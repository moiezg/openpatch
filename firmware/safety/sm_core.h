#ifndef SM_CORE_H
#define SM_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Safety MCU logic (ATtiny1616), independent of the main MCU and its firmware.
 * It owns the motor power switch and the buzzer. It counts every STEP pulse the motor driver
 * receives and cuts motor power when:
 *   - more steps arrive in a rolling minute or hour than the configured ceilings (latched until
 *     the user holds the button for clear_hold_ms), or
 *   - the main MCU's heartbeat stops (re-arms after heartbeats resume for rearm_ms), or
 *   - the battery is below the cutoff voltage (with hysteresis). */

/* Windows are built from 1 s / 1 min buckets, so "the last minute" can span up to 61 s and contain two
 * consecutive per-minute delivery bursts. Configure max_steps_per_min as at least 2x the largest
 * legitimate burst (bolus rate x steps per pulse); a runaway motor is still caught in under a second. */
typedef struct {
    uint32_t max_steps_per_min;
    uint32_t max_steps_per_hour;
    uint16_t hb_timeout_ms;
    uint16_t rearm_ms;
    uint16_t clear_hold_ms;
    uint16_t boot_grace_ms;     /* silent wait for the first heartbeat after power-up */
    uint16_t vbat_warn_mv;
    uint16_t vbat_cutoff_mv;
    uint16_t vbat_hyst_mv;
} sm_config_t;

enum {
    SM_FAULT_WAITING = 1u << 0,    /* not yet armed (boot / re-arm), silent */
    SM_FAULT_HEARTBEAT = 1u << 1,
    SM_FAULT_RATE = 1u << 2,       /* latched */
    SM_FAULT_HOURLY = 1u << 3,     /* latched */
    SM_FAULT_BATTERY = 1u << 4,
    SM_FAULT_BATTERY_LOW = 1u << 5 /* warning only */
};

enum { SM_BUZZER_OFF = 0, SM_BUZZER_CONTINUOUS = 1, SM_BUZZER_BEEP = 2, SM_BUZZER_CHIRP = 3 };

typedef struct {
    uint8_t motor_enable;
    uint8_t buzzer;
    uint8_t faults;
} sm_out_t;

typedef struct {
    sm_config_t cfg;
    uint16_t sec_steps[60];
    uint32_t min_steps[60];
    uint8_t sec_idx;
    uint8_t min_idx;
    uint32_t cur_sec_steps;
    uint32_t cur_min_steps;
    uint32_t sum_minute;       /* completed seconds in the last 60 s */
    uint32_t sum_hour;         /* completed minutes in the last 60 min */
    uint16_t ms_in_sec;
    uint8_t sec_in_min;
    uint32_t uptime_ms;
    uint16_t ms_since_hb;
    uint16_t hb_ok_ms;
    uint8_t ever_armed;
    uint8_t latched;
    uint16_t button_ms;
    uint8_t batt_cut;
} sm_t;

void sm_init(sm_t *s, const sm_config_t *cfg);

/* Call every 1..100 ms with the steps and heartbeat edges counted by ISRs since the last call. */
sm_out_t sm_tick(sm_t *s, uint16_t elapsed_ms, uint16_t new_steps, uint8_t hb_edges, uint16_t vbat_mv,
                 uint8_t button_down, uint8_t alarm_request);

#ifdef __cplusplus
}
#endif

#endif
