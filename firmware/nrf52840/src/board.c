#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "app.h"
#include "op_hal.h"

LOG_MODULE_REGISTER(board, LOG_LEVEL_INF);

#define USER DT_PATH(zephyr_user)

static const struct gpio_dt_spec step = GPIO_DT_SPEC_GET(USER, step_gpios);
static const struct gpio_dt_spec dir = GPIO_DT_SPEC_GET(USER, dir_gpios);
static const struct gpio_dt_spec sleep_n = GPIO_DT_SPEC_GET(USER, sleep_gpios);
static const struct gpio_dt_spec hx_dout = GPIO_DT_SPEC_GET(USER, hx_dout_gpios);
static const struct gpio_dt_spec hx_sck = GPIO_DT_SPEC_GET(USER, hx_sck_gpios);
static const struct gpio_dt_spec heartbeat = GPIO_DT_SPEC_GET(USER, heartbeat_gpios);
static const struct gpio_dt_spec alarm_req = GPIO_DT_SPEC_GET(USER, alarm_req_gpios);
static const struct gpio_dt_spec motor_off = GPIO_DT_SPEC_GET(USER, motor_off_gpios);
static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(USER, button_gpios);
static const struct gpio_dt_spec limit = GPIO_DT_SPEC_GET(USER, limit_gpios);
static const struct gpio_dt_spec door = GPIO_DT_SPEC_GET(USER, door_gpios);
static const struct gpio_dt_spec vbat_en = GPIO_DT_SPEC_GET(USER, vbat_en_gpios);
static const struct gpio_dt_spec led_r = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_g = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_b = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);
static const struct adc_dt_spec vbat = ADC_DT_SPEC_GET(USER);

#define STEP_HALF_US 400            /* ~1.25 kHz under load */
#define REWIND_HALF_US 150          /* unloaded retraction is faster */
#define DRIVER_WAKE_US 1000
#define MAX_REWIND_STEPS 200000u    /* > full 40 mm travel at 0.25 um/step */
#define LIMIT_BACKOFF_STEPS 400

static struct app_cal cal = {.steps_per_pulse_x100 = 1400, .force_counts_per_unit = 100};
static uint32_t step_carry;
static atomic_t rewind_requested;
static atomic_t rewinding;
static atomic_t battery_mv_cached = ATOMIC_INIT(4000);

int board_init(void)
{
    const struct gpio_dt_spec *outs[] = {&step, &dir, &sleep_n, &hx_sck, &heartbeat, &alarm_req,
                                         &led_r, &led_g, &led_b};
    const struct gpio_dt_spec *ins[] = {&hx_dout, &motor_off, &button, &limit, &door};
    for (size_t i = 0; i < ARRAY_SIZE(outs); i++) {
        if (!gpio_is_ready_dt(outs[i]) || gpio_pin_configure_dt(outs[i], GPIO_OUTPUT_INACTIVE) < 0) {
            LOG_ERR("output %u not ready", (unsigned)i);
            return -ENODEV;
        }
    }
    for (size_t i = 0; i < ARRAY_SIZE(ins); i++) {
        if (!gpio_is_ready_dt(ins[i]) || gpio_pin_configure_dt(ins[i], GPIO_INPUT) < 0) {
            LOG_ERR("input %u not ready", (unsigned)i);
            return -ENODEV;
        }
    }
    /* Seeed: keep P0.14 low at all times; driving it high exposes AIN7 to the raw battery voltage */
    if (!gpio_is_ready_dt(&vbat_en) || gpio_pin_configure_dt(&vbat_en, GPIO_OUTPUT_ACTIVE) < 0) {
        LOG_ERR("battery divider enable not ready");
    }
    if (!adc_is_ready_dt(&vbat) || adc_channel_setup_dt(&vbat) < 0) {
        LOG_ERR("battery ADC not ready");
    }
    return 0;
}

void board_apply_cal(const struct app_cal *c)
{
    cal = *c;
    if (cal.steps_per_pulse_x100 == 0) {
        cal.steps_per_pulse_x100 = 1400;
    }
    if (cal.force_counts_per_unit == 0) {
        cal.force_counts_per_unit = 100;
    }
}

/* ---------- motor ---------- */

static void step_once(uint32_t half_us)
{
    gpio_pin_set_dt(&step, 1);
    k_busy_wait(half_us);
    gpio_pin_set_dt(&step, 0);
    k_busy_wait(half_us);
}

static void driver_enable(bool on)
{
    gpio_pin_set_dt(&sleep_n, on ? 0 : 1);   /* nSLEEP is active-low in the devicetree */
    if (on) {
        k_busy_wait(DRIVER_WAKE_US);
    }
}

op_hal_result_t op_hal_motor_pulse(op_fw_t *fw, uint8_t kind)
{
    ARG_UNUSED(fw);
    ARG_UNUSED(kind);
    if (gpio_pin_get_dt(&motor_off)) {
        return OP_HAL_NO_POWER;
    }
    if (gpio_pin_get_dt(&limit)) {
        return OP_HAL_END_OF_TRAVEL;
    }
    uint32_t total = step_carry + cal.steps_per_pulse_x100;
    uint32_t steps = total / 100;
    step_carry = total % 100;

    gpio_pin_set_dt(&dir, 1);
    driver_enable(true);
    for (uint32_t i = 0; i < steps; i++) {
        if (gpio_pin_get_dt(&limit)) {
            driver_enable(false);
            return OP_HAL_END_OF_TRAVEL;
        }
        if (gpio_pin_get_dt(&motor_off)) {
            driver_enable(false);
            return OP_HAL_NO_POWER;
        }
        step_once(STEP_HALF_US);
    }
    driver_enable(false);
    return OP_HAL_OK;
}

bool op_hal_motor_rewind(op_fw_t *fw)
{
    ARG_UNUSED(fw);
    if (gpio_pin_get_dt(&motor_off)) {
        return false;
    }
    atomic_set(&rewind_requested, 1);   /* performed by the delivery loop, outside the command path */
    return true;
}

bool op_hal_busy(op_fw_t *fw)
{
    ARG_UNUSED(fw);
    return atomic_get(&rewind_requested) || atomic_get(&rewinding);
}

bool board_rewind_pending(void)
{
    return atomic_get(&rewind_requested) != 0;
}

bool board_run_rewind(void)
{
    atomic_set(&rewinding, 1);
    atomic_set(&rewind_requested, 0);
    bool ok = false;

    /* If already on the limit switch (end of travel), the first moves back off it */
    gpio_pin_set_dt(&dir, 0);
    driver_enable(true);
    uint32_t i = 0;
    for (; i < LIMIT_BACKOFF_STEPS && gpio_pin_get_dt(&limit); i++) {
        step_once(REWIND_HALF_US);
    }
    for (; i < MAX_REWIND_STEPS; i++) {
        if (gpio_pin_get_dt(&motor_off)) {
            break;
        }
        if (gpio_pin_get_dt(&limit)) {
            ok = true;
            break;
        }
        step_once(REWIND_HALF_US);
        if (i % 2000 == 0) {
            app_keepalive();
        }
    }
    if (ok) {
        /* home and end switches share one input: step forward until the home switch releases */
        gpio_pin_set_dt(&dir, 1);
        uint32_t b = 0;
        for (; b < LIMIT_BACKOFF_STEPS && gpio_pin_get_dt(&limit); b++) {
            step_once(REWIND_HALF_US);
        }
        ok = !gpio_pin_get_dt(&limit);
    }
    driver_enable(false);
    step_carry = 0;
    atomic_set(&rewinding, 0);
    if (!ok) {
        LOG_ERR("rewind failed after %u steps", (unsigned)i);
    }
    return ok;
}

int board_bench_pulses(uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        op_hal_result_t r = op_hal_motor_pulse(NULL, OP_KIND_PRIME);
        if (r != OP_HAL_OK) {
            return -(int)r;
        }
        app_keepalive();
    }
    return 0;
}

/* ---------- load cell (HX711, channel A, gain 128) ---------- */

int32_t board_read_force_raw(void)
{
    for (int waited = 0; gpio_pin_get_dt(&hx_dout); waited++) {
        if (waited > 200) {
            return INT32_MIN;          /* no conversion within 200 ms: sensor fault */
        }
        k_msleep(1);
    }
    uint32_t v = 0;
    unsigned int key = irq_lock();
    for (int i = 0; i < 24; i++) {
        gpio_pin_set_dt(&hx_sck, 1);
        k_busy_wait(1);
        v = (v << 1) | (uint32_t)gpio_pin_get_dt(&hx_dout);
        gpio_pin_set_dt(&hx_sck, 0);
        k_busy_wait(1);
    }
    gpio_pin_set_dt(&hx_sck, 1);       /* 25th pulse selects channel A / gain 128 for the next read */
    k_busy_wait(1);
    gpio_pin_set_dt(&hx_sck, 0);
    irq_unlock(key);
    if (v & 0x800000) {
        v |= 0xFF000000;
    }
    return (int32_t)v;
}

uint16_t op_hal_line_force(op_fw_t *fw)
{
    ARG_UNUSED(fw);
    int32_t raw = board_read_force_raw();
    if (raw == INT32_MIN) {
        return 0xFFFF;                 /* unreadable sensor reads as maximum force: fails safe */
    }
    int32_t f = (raw - cal.force_zero) / cal.force_counts_per_unit;
    if (f < 0) {
        return 0;
    }
    return f > 0xFFFE ? 0xFFFE : (uint16_t)f;
}

/* ---------- battery ---------- */

void board_update_battery(void)
{
    int16_t sample = 0;
    struct adc_sequence seq = {.buffer = &sample, .buffer_size = sizeof sample};
    if (adc_sequence_init_dt(&vbat, &seq) == 0 && adc_read_dt(&vbat, &seq) == 0) {
        int32_t mv = sample;
        if (adc_raw_to_millivolts_dt(&vbat, &mv) == 0) {
            atomic_set(&battery_mv_cached, mv * 1510 / 510);   /* XIAO divider: 1 M / 510 k */
        }
    }
}

uint16_t board_battery_mv(void)
{
    return (uint16_t)atomic_get(&battery_mv_cached);
}

uint8_t op_hal_battery_pct(op_fw_t *fw)
{
    ARG_UNUSED(fw);
    int32_t mv = (int32_t)board_battery_mv();
    int32_t pct = (mv - 3300) * 100 / (4150 - 3300);   /* rough LiPo curve under light load */
    return (uint8_t)(pct < 0 ? 0 : pct > 100 ? 100 : pct);
}

/* ---------- misc I/O ---------- */

void board_set_heartbeat(bool level)
{
    gpio_pin_set_dt(&heartbeat, level);
}

void board_set_alarm_request(bool on)
{
    gpio_pin_set_dt(&alarm_req, on);
}

bool board_motor_power_off(void)
{
    return gpio_pin_get_dt(&motor_off) == 1;
}

bool board_button_pressed(void)
{
    return gpio_pin_get_dt(&button) == 1;
}

bool board_door_open(void)
{
    return gpio_pin_get_dt(&door) == 0;
}

void board_leds(bool red, bool green, bool blue)
{
    gpio_pin_set_dt(&led_r, red);
    gpio_pin_set_dt(&led_g, green);
    gpio_pin_set_dt(&led_b, blue);
}
