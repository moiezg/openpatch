#ifndef OP_FW_H
#define OP_FW_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "op_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Pump firmware core: behaviour identical to openpatch/pump/firmware.py (the executable spec).
 * No OS or hardware calls here; everything physical goes through op_hal.h. */

#define OP_ALARM_OCCLUSION (1u << 0)
#define OP_ALARM_RESERVOIR_EMPTY (1u << 1)
#define OP_ALARM_HOURLY_CAP (1u << 2)
#define OP_ALARM_MOTOR (1u << 3)
#define OP_WARN_RESERVOIR_LOW (1u << 8)
#define OP_WARN_BATTERY_LOW (1u << 9)
#define OP_WARN_RESTARTED (1u << 10)
#define OP_STOP_MASK (OP_ALARM_OCCLUSION | OP_ALARM_RESERVOIR_EMPTY | OP_ALARM_HOURLY_CAP | OP_ALARM_MOTOR)

enum { OP_STATE_RUNNING = 0, OP_STATE_SUSPENDED = 1, OP_STATE_NO_RESERVOIR = 2, OP_STATE_PRIMING = 3 };
enum { OP_KIND_BASAL = 0, OP_KIND_BOLUS = 1, OP_KIND_PRIME = 2 };

#define OP_MAX_BASAL_ENTRIES 48
#define OP_HISTORY_CAP 6144          /* 48 h of per-minute basal + bolus records */
#define OP_HISTORY_KEEP_MIN (2u * 1440u)
#define OP_HISTORY_PAGE 20
#define OP_RESERVOIR_LOW_CU 2000
#define OP_RESERVOIR_MIN_CU 1000
#define OP_RESERVOIR_MAX_CU 30000
#define OP_PRIME_MAX_CU 300
#define OP_BATTERY_LOW_PCT 15

typedef struct {
    uint16_t max_basal_cuph;
    uint16_t max_bolus_cu;
    uint16_t hourly_cap_cu;
    uint16_t max_temp_duration_min;
    uint16_t pulse_cu;
    uint16_t bolus_rate_cu_per_min;
    uint16_t occlusion_threshold;   /* in the same calibrated units op_hal_line_force() returns */
} op_limits_t;

typedef struct {
    uint32_t minute;
    uint16_t cu;
    uint8_t kind;
} op_record_t;

/* Fields that survive a reboot (ports persist this on op_hal_state_changed) */
typedef struct {
    uint32_t minute;
    uint16_t reservoir_cu;
    uint16_t primed_cu;
    uint16_t alarms;
    uint8_t state;
    uint8_t valid;
} op_snapshot_t;

typedef struct op_fw {
    uint8_t key[OP_MAX_KEY];
    uint8_t key_len;
    uint16_t basal_start[OP_MAX_BASAL_ENTRIES];
    uint16_t basal_cuph[OP_MAX_BASAL_ENTRIES];
    uint8_t basal_n;
    op_limits_t lim;

    uint32_t minute;
    uint16_t reservoir_cu;
    uint8_t state;
    bool temp_active;
    uint16_t temp_rate_cuph;
    uint32_t temp_end;
    uint16_t bolus_remaining_cu;
    uint16_t prime_remaining_cu;
    uint16_t primed_cu;
    uint32_t basal_accum;
    uint16_t alarms;

    op_record_t hist[OP_HISTORY_CAP];
    uint16_t hist_head;
    uint16_t hist_count;

    bool last_valid;
    uint8_t last_seq;
    uint16_t last_req_len;
    uint16_t last_resp_len;
    uint8_t last_req[OP_MAX_FRAME];
    uint8_t last_resp[OP_MAX_FRAME];

    void *hal;                      /* port-specific context passed back to op_hal_* */
} op_fw_t;

/* Returns false if the configuration is invalid (key too long, empty/unsorted schedule). */
bool op_fw_configure(op_fw_t *fw, const uint8_t *key, size_t key_len, const uint16_t *basal_start,
                     const uint16_t *basal_cuph, size_t basal_n, const op_limits_t *lim);
bool op_fw_init(op_fw_t *fw, void *hal, const uint8_t *key, size_t key_len, const uint16_t *basal_start,
                const uint16_t *basal_cuph, size_t basal_n, const op_limits_t *lim, uint16_t reservoir_cu,
                uint32_t start_minute);

size_t op_fw_handle(op_fw_t *fw, const uint8_t *frame, size_t len, uint8_t *out, size_t out_cap);

/* Run as if a valid frame with this command arrived (button, door switch, shell). Returns status. */
int op_fw_local_command(op_fw_t *fw, uint8_t cmd, const uint8_t *payload, uint8_t len);

/* One minute of delivery at `minute`; returns centi-units the motor pushed. */
uint16_t op_fw_tick(op_fw_t *fw, uint32_t minute);

void op_fw_raise(op_fw_t *fw, uint16_t alarm);
uint16_t op_fw_current_rate_cuph(const op_fw_t *fw, uint32_t minute);
uint16_t op_fw_window_cu(const op_fw_t *fw);

/* Reboot handling */
op_snapshot_t op_fw_snapshot(const op_fw_t *fw);
void op_fw_restore(op_fw_t *fw, const op_snapshot_t *snap);
void op_fw_history_restore(op_fw_t *fw, const op_record_t *rec);
void op_fw_power_cycle(op_fw_t *fw);

#ifdef __cplusplus
}
#endif

#endif
