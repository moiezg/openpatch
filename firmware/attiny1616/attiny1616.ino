// OpenPatch safety MCU (ATtiny1616, megaTinyCore, 10 MHz internal, millis disabled).
// All decisions live in sm_core.c, tested on the host (tests/test_safety_mcu.py). This file only
// connects that logic to pins, a 10 ms timer and the watchdog.
//
//  PA1  STEP_IN     <- DRV8834 STEP line (same wire the nRF52840 drives)
//  PA2  HB_IN       <- nRF52840 heartbeat (toggles every 250 ms while healthy)
//  PA3  BUTTON      <- shared push button to GND (hold 5 s to clear a latched runaway fault)
//  PA4  VBAT        <- battery through a 1:2 divider (ADC, 2.5 V reference)
//  PA5  MOTOR_EN    -> load switch enable for motor power (the only path to motor power)
//  PA6  ALARM_REQ   <- nRF52840 asks for the buzzer (pump alarms)
//  PA7  MOTOR_OFF   -> nRF52840, high when motor power is cut
//  PB0  BUZZER      -> active piezo buzzer via an NPN transistor
//  PA0  UPDI        programming

#include <avr/io.h>
#include <avr/interrupt.h>
#include <util/atomic.h>

#include "sm_core.h"

#if F_CPU > 13000000UL
#error "Select clock=10internal (the 10 ms timer is computed for <= 13 MHz)"
#endif

// ---------- configuration: keep in step with the pump's provisioned limits ----------
#define STEPS_PER_PULSE_X100 1400UL       // same value as `op cal steps` on the nRF52840
#define BOLUS_PULSES_PER_MIN 30UL         // 1.5 U/min, the firmware's fixed bolus rate
#define HOURLY_CEILING_PULSES 260UL       // 13 U: set just above the pump's own hourly cap
#define STEPS_FOR(pulses) (((pulses) * STEPS_PER_PULSE_X100 + 99UL) / 100UL)

static const sm_config_t CFG = {
    2UL * STEPS_FOR(BOLUS_PULSES_PER_MIN + 2UL),   // per-minute ceiling: 2x the largest burst (see sm_core.h)
    STEPS_FOR(HOURLY_CEILING_PULSES),
    2000,    // heartbeat timeout ms
    3000,    // continuous heartbeat needed to (re)arm, ms
    5000,    // button hold to clear a latched fault, ms
    10000,   // boot grace before a missing heartbeat alarms, ms
    3500,    // battery warning mV
    3300,    // battery cutoff mV
    150,     // battery hysteresis mV
};

#define TICK_MS 10

static volatile uint16_t step_count;
static volatile uint8_t hb_edges;
static volatile uint8_t tick_flag;
static sm_t sm;
static uint16_t pattern_ms;

static void on_step(void) { step_count++; }

static void on_heartbeat(void)
{
    if (hb_edges < 255) {
        hb_edges++;
    }
}

ISR(TCB0_INT_vect)
{
    TCB0.INTFLAGS = TCB_CAPT_bm;
    tick_flag = 1;
}

static void set_motor(uint8_t enable)
{
    digitalWrite(PIN_PA5, enable ? HIGH : LOW);
    digitalWrite(PIN_PA7, enable ? LOW : HIGH);
}

static void drive_buzzer(uint8_t mode)
{
    pattern_ms = (uint16_t)((pattern_ms + TICK_MS) % 30000);
    uint8_t on = 0;
    switch (mode) {
    case SM_BUZZER_CONTINUOUS: on = 1; break;
    case SM_BUZZER_BEEP: on = (pattern_ms % 1000) < 200; break;
    case SM_BUZZER_CHIRP: on = pattern_ms < 60; break;
    default: on = 0; break;
    }
    digitalWrite(PIN_PB0, on ? HIGH : LOW);
}

void setup()
{
    pinMode(PIN_PA5, OUTPUT);
    pinMode(PIN_PA7, OUTPUT);
    set_motor(0);                       // motor power stays off until the main MCU proves it is alive
    pinMode(PIN_PB0, OUTPUT);
    digitalWrite(PIN_PB0, LOW);
    pinMode(PIN_PA1, INPUT);
    pinMode(PIN_PA2, INPUT);
    pinMode(PIN_PA3, INPUT_PULLUP);
    pinMode(PIN_PA6, INPUT);
    analogReference(INTERNAL2V5);

    sm_init(&sm, &CFG);
    attachInterrupt(digitalPinToInterrupt(PIN_PA1), on_step, RISING);
    attachInterrupt(digitalPinToInterrupt(PIN_PA2), on_heartbeat, CHANGE);

    // 10 ms periodic interrupt on TCB0 (CLK_PER / 2)
    TCB0.CCMP = (uint16_t)(F_CPU / 2UL / (1000UL / TICK_MS) - 1UL);
    TCB0.CTRLB = TCB_CNTMODE_INT_gc;
    TCB0.INTCTRL = TCB_CAPT_bm;
    TCB0.CTRLA = TCB_CLKSEL_CLKDIV2_gc | TCB_ENABLE_bm;

    _PROTECTED_WRITE(WDT.CTRLA, WDT_PERIOD_256CLK_gc);   // ~0.26 s: a hung safety MCU resets with motor off
}

void loop()
{
    if (!tick_flag) {
        return;
    }
    tick_flag = 0;
    __asm__ __volatile__("wdr");

    uint16_t steps;
    uint8_t hb;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE)
    {
        steps = step_count;
        step_count = 0;
        hb = hb_edges;
        hb_edges = 0;
    }
    uint16_t vbat_mv = (uint16_t)((uint32_t)analogRead(PIN_PA4) * 2500UL * 2UL / 1023UL);
    uint8_t button = digitalRead(PIN_PA3) == LOW;
    uint8_t alarm_req = digitalRead(PIN_PA6) == HIGH;

    sm_out_t out = sm_tick(&sm, TICK_MS, steps, hb, vbat_mv, button, alarm_req);
    set_motor(out.motor_enable);
    drive_buzzer(out.buzzer);
}
