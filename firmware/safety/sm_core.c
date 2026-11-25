#include "sm_core.h"

#include <string.h>

static uint16_t sat_add16(uint16_t a, uint16_t b)
{
    uint32_t s = (uint32_t)a + b;
    return s > 0xFFFF ? 0xFFFF : (uint16_t)s;
}

static void clear_windows(sm_t *s)
{
    memset(s->sec_steps, 0, sizeof s->sec_steps);
    memset(s->min_steps, 0, sizeof s->min_steps);
    s->cur_sec_steps = 0;
    s->cur_min_steps = 0;
    s->sum_minute = 0;
    s->sum_hour = 0;
}

void sm_init(sm_t *s, const sm_config_t *cfg)
{
    memset(s, 0, sizeof *s);
    s->cfg = *cfg;
    s->ms_since_hb = 0xFFFF;
}

static void roll_second(sm_t *s)
{
    s->sum_minute -= s->sec_steps[s->sec_idx];
    s->sec_steps[s->sec_idx] = s->cur_sec_steps > 0xFFFF ? 0xFFFF : (uint16_t)s->cur_sec_steps;
    s->sum_minute += s->sec_steps[s->sec_idx];
    s->sec_idx = (uint8_t)((s->sec_idx + 1) % 60);
    s->cur_sec_steps = 0;

    if (++s->sec_in_min == 60) {
        s->sec_in_min = 0;
        s->sum_hour -= s->min_steps[s->min_idx];
        s->min_steps[s->min_idx] = s->cur_min_steps;
        s->sum_hour += s->cur_min_steps;
        s->min_idx = (uint8_t)((s->min_idx + 1) % 60);
        s->cur_min_steps = 0;
    }
}

sm_out_t sm_tick(sm_t *s, uint16_t elapsed_ms, uint16_t new_steps, uint8_t hb_edges, uint16_t vbat_mv,
                 uint8_t button_down, uint8_t alarm_request)
{
    const sm_config_t *c = &s->cfg;
    sm_out_t out = {0, SM_BUZZER_OFF, 0};

    s->uptime_ms = s->uptime_ms + elapsed_ms < s->uptime_ms ? 0xFFFFFFFFu : s->uptime_ms + elapsed_ms;

    /* step accounting: rolling 60 s (per-second buckets) and 60 min (per-minute buckets) */
    s->cur_sec_steps += new_steps;
    s->cur_min_steps += new_steps;
    s->ms_in_sec = (uint16_t)(s->ms_in_sec + elapsed_ms);
    while (s->ms_in_sec >= 1000) {
        s->ms_in_sec = (uint16_t)(s->ms_in_sec - 1000);
        roll_second(s);
    }
    uint32_t last_minute = s->sum_minute + s->cur_sec_steps;
    uint32_t last_hour = s->sum_hour + s->cur_min_steps;
    if (last_minute > c->max_steps_per_min) {
        s->latched |= SM_FAULT_RATE;
    }
    if (last_hour > c->max_steps_per_hour) {
        s->latched |= SM_FAULT_HOURLY;
    }

    /* latched faults clear only by a deliberate long press; the step history is forgotten */
    s->button_ms = button_down ? sat_add16(s->button_ms, elapsed_ms) : 0;
    if (s->latched && s->button_ms >= c->clear_hold_ms) {
        s->latched = 0;
        s->button_ms = 0;
        clear_windows(s);
    }

    /* heartbeat */
    s->ms_since_hb = hb_edges ? 0 : sat_add16(s->ms_since_hb, elapsed_ms);
    uint8_t hb_ok = s->ms_since_hb < c->hb_timeout_ms;
    s->hb_ok_ms = hb_ok ? sat_add16(s->hb_ok_ms, elapsed_ms) : 0;
    uint8_t armed = s->hb_ok_ms >= c->rearm_ms;
    if (armed) {
        s->ever_armed = 1;
    }
    uint8_t hb_fault = !hb_ok && (s->ever_armed || s->uptime_ms >= c->boot_grace_ms);

    /* battery with hysteresis */
    if (vbat_mv < c->vbat_cutoff_mv) {
        s->batt_cut = 1;
    } else if (s->batt_cut && vbat_mv >= (uint16_t)(c->vbat_cutoff_mv + c->vbat_hyst_mv)) {
        s->batt_cut = 0;
    }
    uint8_t batt_warn = vbat_mv < c->vbat_warn_mv;

    out.faults = (uint8_t)(s->latched | (armed ? 0 : SM_FAULT_WAITING) | (hb_fault ? SM_FAULT_HEARTBEAT : 0) |
                           (s->batt_cut ? SM_FAULT_BATTERY : 0) | (batt_warn ? SM_FAULT_BATTERY_LOW : 0));
    out.motor_enable = (uint8_t)(armed && !s->latched && !s->batt_cut);

    if (s->latched || hb_fault || s->batt_cut) {
        out.buzzer = SM_BUZZER_CONTINUOUS;
    } else if (alarm_request) {
        out.buzzer = SM_BUZZER_BEEP;
    } else if (batt_warn) {
        out.buzzer = SM_BUZZER_CHIRP;
    }
    return out;
}
