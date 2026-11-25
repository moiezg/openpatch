/* Host build of the firmware core: a simulated plant behind op_hal.h, exported as a shared library
 * so the Python test-suite can drive the real C code (openpatch/pump/cfirmware.py). */

#include <stdlib.h>
#include <string.h>

#include "op_fw.h"
#include "op_hal.h"
#include "sm_core.h"

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

#define BATTERY_FULL_MICRO 1000000u
#define BATTERY_MICRO_PER_PULSE 4u

typedef struct {
    op_fw_t fw;
    uint32_t battery_micro;
    bool occluded;
    uint32_t undelivered_cu;
    bool motor_fault;
    uint32_t body_cu;
} host_t;

/* ---------- simulated plant (mirrors PumpFirmware._hal_* in Python) ---------- */

op_hal_result_t op_hal_motor_pulse(op_fw_t *fw, uint8_t kind)
{
    host_t *h = (host_t *)fw->hal;
    if (h->motor_fault) {
        return OP_HAL_FAULT;
    }
    h->battery_micro = h->battery_micro > BATTERY_MICRO_PER_PULSE ? h->battery_micro - BATTERY_MICRO_PER_PULSE : 0;
    if (h->occluded) {
        h->undelivered_cu += fw->lim.pulse_cu;
    } else if (kind != OP_KIND_PRIME) {
        h->body_cu += fw->lim.pulse_cu;
    }
    return OP_HAL_OK;
}

bool op_hal_motor_rewind(op_fw_t *fw)
{
    return !((host_t *)fw->hal)->motor_fault;
}

bool op_hal_busy(op_fw_t *fw)
{
    (void)fw;
    return false;
}

uint16_t op_hal_line_force(op_fw_t *fw)
{
    uint32_t u = ((host_t *)fw->hal)->undelivered_cu;
    return u > 0xFFFE ? 0xFFFE : (uint16_t)u;
}

uint8_t op_hal_battery_pct(op_fw_t *fw)
{
    return (uint8_t)(((host_t *)fw->hal)->battery_micro / 10000u);
}

void op_hal_history_changed(op_fw_t *fw, const op_record_t *rec)
{
    (void)fw;
    (void)rec;
}

void op_hal_state_changed(op_fw_t *fw)
{
    (void)fw;
}

void op_hal_time_set(op_fw_t *fw, uint32_t minute)
{
    (void)fw;
    (void)minute;
}

/* ---------- exported API ---------- */

EXPORT void *oph_new(const uint8_t *key, int key_len, const uint16_t *starts, const uint16_t *rates, int n,
                     const uint16_t *limits7, uint16_t reservoir_cu, uint32_t start_minute)
{
    host_t *h = calloc(1, sizeof *h);
    if (!h) {
        return NULL;
    }
    op_limits_t lim = {limits7[0], limits7[1], limits7[2], limits7[3], limits7[4], limits7[5], limits7[6]};
    if (!op_fw_init(&h->fw, h, key, (size_t)key_len, starts, rates, (size_t)n, &lim, reservoir_cu, start_minute)) {
        free(h);
        return NULL;
    }
    h->battery_micro = BATTERY_FULL_MICRO;
    return h;
}

EXPORT void oph_free(void *p)
{
    free(p);
}

EXPORT int oph_handle(void *p, const uint8_t *frame, int len, uint8_t *out, int cap)
{
    return (int)op_fw_handle(&((host_t *)p)->fw, frame, (size_t)len, out, (size_t)cap);
}

EXPORT int oph_tick(void *p, uint32_t minute)
{
    host_t *h = p;
    h->body_cu = 0;
    op_fw_tick(&h->fw, minute);
    return (int)h->body_cu;
}

EXPORT int oph_local_command(void *p, uint8_t cmd, const uint8_t *payload, int len)
{
    return op_fw_local_command(&((host_t *)p)->fw, cmd, payload, (uint8_t)len);
}

EXPORT void oph_inject_occlusion(void *p)
{
    ((host_t *)p)->occluded = true;
}

EXPORT void oph_replace_site(void *p)
{
    host_t *h = p;
    h->occluded = false;
    h->undelivered_cu = 0;
}

EXPORT void oph_inject_motor_fault(void *p, int on)
{
    ((host_t *)p)->motor_fault = on != 0;
}

EXPORT void oph_power_cycle(void *p)
{
    host_t *h = p;
    /* emulate a real reboot: persisted snapshot + history survive, RAM state does not */
    op_snapshot_t snap = op_fw_snapshot(&h->fw);
    op_fw_restore(&h->fw, &snap);
    op_fw_power_cycle(&h->fw);
}

EXPORT int oph_current_rate_cuph(void *p, uint32_t minute)
{
    return op_fw_current_rate_cuph(&((host_t *)p)->fw, minute);
}

enum { F_ALARMS, F_BOLUS_REMAINING, F_RESERVOIR, F_STATE, F_MINUTE, F_BATTERY, F_PRIMED, F_HIST_COUNT };

EXPORT uint32_t oph_get(void *p, int field)
{
    const op_fw_t *fw = &((host_t *)p)->fw;
    switch (field) {
    case F_ALARMS: return fw->alarms;
    case F_BOLUS_REMAINING: return fw->bolus_remaining_cu;
    case F_RESERVOIR: return fw->reservoir_cu;
    case F_STATE: return fw->state;
    case F_MINUTE: return fw->minute;
    case F_BATTERY: return ((host_t *)p)->battery_micro;
    case F_PRIMED: return fw->primed_cu;
    case F_HIST_COUNT: return fw->hist_count;
    default: return 0;
    }
}

EXPORT int oph_history(void *p, uint32_t *minutes, uint16_t *cus, uint8_t *kinds, int cap)
{
    const op_fw_t *fw = &((host_t *)p)->fw;
    int n = fw->hist_count < cap ? fw->hist_count : cap;
    for (int i = 0; i < n; i++) {
        const op_record_t *r = &fw->hist[(fw->hist_head + i) % OP_HISTORY_CAP];
        minutes[i] = r->minute;
        cus[i] = r->cu;
        kinds[i] = r->kind;
    }
    return n;
}

EXPORT void oph_sha256(const uint8_t *data, int len, uint8_t *out)
{
    op_sha256(data, (size_t)len, out);
}

/* ---------- safety MCU core ---------- */

EXPORT int smh_size(void)
{
    return (int)sizeof(sm_t);
}

EXPORT void smh_init(void *s, uint32_t max_steps_per_min, uint32_t max_steps_per_hour, uint16_t hb_timeout_ms,
                     uint16_t rearm_ms, uint16_t clear_hold_ms, uint16_t boot_grace_ms, uint16_t vbat_warn_mv,
                     uint16_t vbat_cutoff_mv, uint16_t vbat_hyst_mv)
{
    sm_config_t cfg = {max_steps_per_min, max_steps_per_hour, hb_timeout_ms, rearm_ms, clear_hold_ms,
                       boot_grace_ms, vbat_warn_mv, vbat_cutoff_mv, vbat_hyst_mv};
    sm_init((sm_t *)s, &cfg);
}

EXPORT void smh_tick(void *s, uint16_t elapsed_ms, uint16_t new_steps, uint8_t hb_edges, uint16_t vbat_mv,
                     uint8_t button_down, uint8_t alarm_request, uint8_t *out3)
{
    sm_out_t o = sm_tick((sm_t *)s, elapsed_ms, new_steps, hb_edges, vbat_mv, button_down, alarm_request);
    out3[0] = o.motor_enable;
    out3[1] = o.buzzer;
    out3[2] = o.faults;
}
