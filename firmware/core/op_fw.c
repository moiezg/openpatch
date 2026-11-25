#include "op_fw.h"

#include <string.h>

#include "op_hal.h"

/* ---------- helpers ---------- */

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

static uint32_t get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static op_record_t *hist_at(op_fw_t *fw, uint16_t i)
{
    return &fw->hist[(fw->hist_head + i) % OP_HISTORY_CAP];
}

static const op_record_t *hist_at_c(const op_fw_t *fw, uint16_t i)
{
    return &fw->hist[(fw->hist_head + i) % OP_HISTORY_CAP];
}

static op_record_t *hist_last(op_fw_t *fw)
{
    return fw->hist_count ? hist_at(fw, (uint16_t)(fw->hist_count - 1)) : NULL;
}

static void hist_append(op_fw_t *fw, uint32_t minute, uint8_t kind, uint16_t cu)
{
    if (fw->hist_count == OP_HISTORY_CAP) {
        fw->hist_head = (uint16_t)((fw->hist_head + 1) % OP_HISTORY_CAP);
        fw->hist_count--;
    }
    op_record_t *r = hist_at(fw, fw->hist_count);
    r->minute = minute;
    r->kind = kind;
    r->cu = cu;
    fw->hist_count++;
}

static uint16_t hist_lower_bound(const op_fw_t *fw, uint32_t since)
{
    uint16_t lo = 0, hi = fw->hist_count;
    while (lo < hi) {
        uint16_t mid = (uint16_t)((lo + hi) / 2);
        if (hist_at_c(fw, mid)->minute < since) {
            lo = (uint16_t)(mid + 1);
        } else {
            hi = mid;
        }
    }
    return lo;
}

static uint16_t basal_at(const op_fw_t *fw, uint32_t minute)
{
    uint32_t m = minute % 1440u;
    uint16_t rate = fw->basal_cuph[0];
    for (uint8_t i = 0; i < fw->basal_n; i++) {
        if (fw->basal_start[i] > m) {
            break;
        }
        rate = fw->basal_cuph[i];
    }
    return rate;
}

static bool temp_active_at(const op_fw_t *fw, uint32_t minute)
{
    return fw->temp_active && minute < fw->temp_end;
}

/* ---------- configuration ---------- */

bool op_fw_configure(op_fw_t *fw, const uint8_t *key, size_t key_len, const uint16_t *basal_start,
                     const uint16_t *basal_cuph, size_t basal_n, const op_limits_t *lim)
{
    if (key_len > OP_MAX_KEY || basal_n == 0 || basal_n > OP_MAX_BASAL_ENTRIES || basal_start[0] != 0 ||
        lim->pulse_cu == 0) {
        return false;
    }
    for (size_t i = 1; i < basal_n; i++) {
        if (basal_start[i] <= basal_start[i - 1] || basal_start[i] >= 1440) {
            return false;
        }
    }
    memcpy(fw->key, key, key_len);
    fw->key_len = (uint8_t)key_len;
    memcpy(fw->basal_start, basal_start, basal_n * sizeof(uint16_t));
    memcpy(fw->basal_cuph, basal_cuph, basal_n * sizeof(uint16_t));
    fw->basal_n = (uint8_t)basal_n;
    fw->lim = *lim;
    fw->last_valid = false;
    return true;
}

bool op_fw_init(op_fw_t *fw, void *hal, const uint8_t *key, size_t key_len, const uint16_t *basal_start,
                const uint16_t *basal_cuph, size_t basal_n, const op_limits_t *lim, uint16_t reservoir_cu,
                uint32_t start_minute)
{
    memset(fw, 0, sizeof *fw);
    fw->hal = hal;
    if (!op_fw_configure(fw, key, key_len, basal_start, basal_cuph, basal_n, lim)) {
        return false;
    }
    fw->minute = start_minute;
    fw->reservoir_cu = reservoir_cu;
    fw->state = reservoir_cu > 0 ? OP_STATE_RUNNING : OP_STATE_NO_RESERVOIR;
    return true;
}

/* ---------- delivery ---------- */

uint16_t op_fw_window_cu(const op_fw_t *fw)
{
    uint32_t total = 0;
    for (uint16_t i = fw->hist_count; i > 0; i--) {
        const op_record_t *r = hist_at_c(fw, (uint16_t)(i - 1));
        if ((int64_t)r->minute <= (int64_t)fw->minute - 60) {
            break;
        }
        total += r->cu;
    }
    return total > 0xFFFF ? 0xFFFF : (uint16_t)total;
}

uint16_t op_fw_current_rate_cuph(const op_fw_t *fw, uint32_t minute)
{
    if (fw->state != OP_STATE_RUNNING || (fw->alarms & OP_STOP_MASK)) {
        return 0;
    }
    if (temp_active_at(fw, minute)) {
        return fw->temp_rate_cuph;
    }
    return basal_at(fw, minute);
}

void op_fw_raise(op_fw_t *fw, uint16_t alarm)
{
    fw->alarms |= alarm;
    if (alarm & OP_STOP_MASK) {
        fw->bolus_remaining_cu = 0;
        fw->prime_remaining_cu = 0;
        fw->temp_active = false;
        fw->basal_accum = 0;      /* never deliver a backlog after the alarm is cleared */
    }
}

static void record_pulse(op_fw_t *fw, uint8_t kind)
{
    op_record_t *last = hist_last(fw);
    if (last && last->minute == fw->minute && last->kind == kind) {
        last->cu = (uint16_t)(last->cu + fw->lim.pulse_cu);
    } else {
        hist_append(fw, fw->minute, kind, fw->lim.pulse_cu);
    }
}

/* returns true if a pulse was delivered by the motor */
static bool pulse(op_fw_t *fw, uint8_t kind)
{
    const uint16_t p = fw->lim.pulse_cu;
    if (fw->reservoir_cu < p) {
        op_fw_raise(fw, OP_ALARM_RESERVOIR_EMPTY);
        return false;
    }
    if ((uint32_t)op_fw_window_cu(fw) + p > fw->lim.hourly_cap_cu) {
        op_fw_raise(fw, OP_ALARM_HOURLY_CAP);
        return false;
    }
    op_hal_result_t r = op_hal_motor_pulse(fw, kind);
    if (r != OP_HAL_OK) {
        op_fw_raise(fw, r == OP_HAL_END_OF_TRAVEL ? OP_ALARM_RESERVOIR_EMPTY : OP_ALARM_MOTOR);
        return false;
    }
    fw->reservoir_cu = (uint16_t)(fw->reservoir_cu - p);
    record_pulse(fw, kind);
    return true;
}

uint16_t op_fw_tick(op_fw_t *fw, uint32_t minute)
{
    const uint16_t p = fw->lim.pulse_cu;
    const uint32_t due = (uint32_t)p * 60u;
    uint16_t alarms_before = fw->alarms;
    uint32_t pushed = 0;

    fw->minute = minute;
    if (fw->temp_active && minute >= fw->temp_end) {
        fw->temp_active = false;
    }

    if (!(fw->alarms & OP_STOP_MASK)) {
        if (fw->state == OP_STATE_RUNNING) {
            fw->basal_accum += fw->temp_active ? fw->temp_rate_cuph : basal_at(fw, minute);
            while (fw->basal_accum >= due) {
                if (!pulse(fw, OP_KIND_BASAL)) {
                    break;
                }
                fw->basal_accum -= due;
                pushed += p;
            }
            uint16_t budget = fw->bolus_remaining_cu < fw->lim.bolus_rate_cu_per_min ? fw->bolus_remaining_cu
                                                                                       : fw->lim.bolus_rate_cu_per_min;
            while (budget >= p && !(fw->alarms & OP_STOP_MASK)) {
                if (!pulse(fw, OP_KIND_BOLUS)) {
                    break;
                }
                budget = (uint16_t)(budget - p);
                fw->bolus_remaining_cu = (uint16_t)(fw->bolus_remaining_cu - p);
                pushed += p;
            }
        } else if (fw->state == OP_STATE_PRIMING) {
            uint16_t budget = fw->prime_remaining_cu < fw->lim.bolus_rate_cu_per_min ? fw->prime_remaining_cu
                                                                                       : fw->lim.bolus_rate_cu_per_min;
            while (budget >= p && !(fw->alarms & OP_STOP_MASK)) {
                if (!pulse(fw, OP_KIND_PRIME)) {
                    break;
                }
                budget = (uint16_t)(budget - p);
                fw->prime_remaining_cu = (uint16_t)(fw->prime_remaining_cu - p);
                fw->primed_cu = (uint16_t)(fw->primed_cu + p);
                pushed += p;
            }
        }
    }

    if (pushed && op_hal_line_force(fw) >= fw->lim.occlusion_threshold) {
        op_fw_raise(fw, OP_ALARM_OCCLUSION);
    }

    bool low_res = (fw->state == OP_STATE_RUNNING || fw->state == OP_STATE_SUSPENDED) &&
                   fw->reservoir_cu < OP_RESERVOIR_LOW_CU;
    fw->alarms = low_res ? (uint16_t)(fw->alarms | OP_WARN_RESERVOIR_LOW)
                         : (uint16_t)(fw->alarms & ~OP_WARN_RESERVOIR_LOW);
    bool low_batt = op_hal_battery_pct(fw) < OP_BATTERY_LOW_PCT;
    fw->alarms = low_batt ? (uint16_t)(fw->alarms | OP_WARN_BATTERY_LOW)
                          : (uint16_t)(fw->alarms & ~OP_WARN_BATTERY_LOW);

    int64_t cutoff = (int64_t)minute - (int64_t)OP_HISTORY_KEEP_MIN;
    while (fw->hist_count && (int64_t)hist_at(fw, 0)->minute < cutoff) {
        fw->hist_head = (uint16_t)((fw->hist_head + 1) % OP_HISTORY_CAP);
        fw->hist_count--;
    }

    if (pushed) {
        for (uint16_t i = fw->hist_count; i > 0; i--) {
            op_record_t *r = hist_at(fw, (uint16_t)(i - 1));
            if (r->minute != minute) {
                break;
            }
            op_hal_history_changed(fw, r);
        }
    }
    if (pushed || fw->alarms != alarms_before) {
        op_hal_state_changed(fw);
    }
    return (uint16_t)pushed;
}

/* ---------- commands ---------- */

static int expected_len(uint8_t cmd)
{
    switch (cmd) {
    case OP_CMD_STATUS:
    case OP_CMD_CLEAR_ALARMS:
    case OP_CMD_SUSPEND:
    case OP_CMD_RESUME:
    case OP_CMD_CANCEL_BOLUS:
    case OP_CMD_CANCEL_TEMP:
    case OP_CMD_REWIND:
        return 0;
    case OP_CMD_BOLUS:
    case OP_CMD_SET_RESERVOIR:
    case OP_CMD_PRIME:
        return 2;
    case OP_CMD_SET_TIME:
    case OP_CMD_SET_TEMP:
        return 4;
    case OP_CMD_HISTORY:
        return 5;
    default:
        return -1;
    }
}

static uint8_t status_payload(op_fw_t *fw, uint8_t *out)
{
    bool temp = temp_active_at(fw, fw->minute);
    uint8_t flags = (uint8_t)((fw->state == OP_STATE_SUSPENDED ? 0x01 : 0) | (temp ? 0x02 : 0));
    put_u32(out, fw->minute);
    put_u16(out + 4, fw->reservoir_cu);
    out[6] = op_hal_battery_pct(fw);
    out[7] = flags;
    put_u16(out + 8, temp ? fw->temp_rate_cuph : 0);
    put_u16(out + 10, temp ? (uint16_t)(fw->temp_end - fw->minute) : 0);
    put_u16(out + 12, fw->alarms);
    put_u16(out + 14, op_fw_window_cu(fw));
    put_u16(out + 16, fw->bolus_remaining_cu);
    out[18] = fw->state;
    return 19;
}

static int execute(op_fw_t *fw, uint8_t cmd, const uint8_t *p, uint8_t len, uint8_t *out, uint8_t *out_len)
{
    *out_len = 0;
    int want = expected_len(cmd);
    if (want < 0) {
        return OP_ST_UNKNOWN;
    }
    if (len != want) {
        return OP_ST_FORMAT;
    }
    const bool stopped = (fw->alarms & OP_STOP_MASK) != 0;
    const uint16_t pc = fw->lim.pulse_cu;

    switch (cmd) {
    case OP_CMD_STATUS:
        *out_len = status_payload(fw, out);
        return OP_ST_OK;

    case OP_CMD_HISTORY: {
        uint32_t since = get_u32(p);
        uint8_t max_n = p[4] < OP_HISTORY_PAGE ? p[4] : OP_HISTORY_PAGE;
        uint16_t i = hist_lower_bound(fw, since);
        uint8_t n = 0;
        while (n < max_n && i < fw->hist_count) {
            const op_record_t *r = hist_at_c(fw, i++);
            uint8_t *dst = out + 1 + 7 * n;
            put_u32(dst, r->minute);
            put_u16(dst + 4, r->cu);
            dst[6] = r->kind;
            n++;
        }
        out[0] = n;
        *out_len = (uint8_t)(1 + 7 * n);
        return OP_ST_OK;
    }

    case OP_CMD_SET_TIME: {
        uint32_t minute = get_u32(p);
        op_record_t *last = hist_last(fw);
        if (last && minute < last->minute) {
            return OP_ST_STATE;
        }
        fw->minute = minute;
        op_hal_time_set(fw, minute);
        return OP_ST_OK;
    }

    case OP_CMD_CLEAR_ALARMS:
        if ((fw->alarms & OP_ALARM_OCCLUSION) && op_hal_line_force(fw) >= fw->lim.occlusion_threshold) {
            return OP_ST_STATE;   /* line still pressurised: replace the site first */
        }
        fw->alarms = (uint16_t)(fw->alarms & ~(OP_STOP_MASK | OP_WARN_RESTARTED));
        return OP_ST_OK;

    case OP_CMD_SUSPEND:
        if (fw->state == OP_STATE_RUNNING) {
            fw->state = OP_STATE_SUSPENDED;
        }
        fw->temp_active = false;
        fw->bolus_remaining_cu = 0;
        fw->prime_remaining_cu = 0;
        return OP_ST_OK;

    case OP_CMD_RESUME:
        if (fw->state == OP_STATE_SUSPENDED) {
            fw->state = OP_STATE_RUNNING;
        } else if (fw->state == OP_STATE_PRIMING) {
            if (fw->primed_cu == 0 || fw->prime_remaining_cu > 0) {
                return OP_ST_STATE;
            }
            fw->state = OP_STATE_RUNNING;
        } else if (fw->state == OP_STATE_NO_RESERVOIR) {
            return OP_ST_STATE;
        }
        return OP_ST_OK;

    case OP_CMD_CANCEL_BOLUS:
        fw->bolus_remaining_cu = 0;
        return OP_ST_OK;

    case OP_CMD_CANCEL_TEMP:
        fw->temp_active = false;
        return OP_ST_OK;

    case OP_CMD_REWIND:
        if (fw->state == OP_STATE_RUNNING) {
            return OP_ST_STATE;
        }
        if (!op_hal_motor_rewind(fw)) {
            op_fw_raise(fw, OP_ALARM_MOTOR);
            return OP_ST_STATE;
        }
        fw->state = OP_STATE_NO_RESERVOIR;
        fw->reservoir_cu = 0;
        fw->primed_cu = 0;
        fw->prime_remaining_cu = 0;
        fw->basal_accum = 0;
        fw->alarms = (uint16_t)(fw->alarms & ~OP_ALARM_RESERVOIR_EMPTY);
        return OP_ST_OK;

    case OP_CMD_SET_RESERVOIR: {
        uint16_t cu = get_u16(p);
        if (fw->state != OP_STATE_NO_RESERVOIR || op_hal_busy(fw)) {
            return OP_ST_STATE;
        }
        if (cu < OP_RESERVOIR_MIN_CU || cu > OP_RESERVOIR_MAX_CU) {
            return OP_ST_LIMIT;
        }
        fw->reservoir_cu = cu;
        fw->primed_cu = 0;
        fw->state = OP_STATE_PRIMING;
        return OP_ST_OK;
    }

    case OP_CMD_PRIME: {
        uint16_t cu = get_u16(p);
        if (stopped) {
            return OP_ST_ALARM;
        }
        if (fw->state != OP_STATE_PRIMING || fw->prime_remaining_cu > 0 || op_hal_busy(fw)) {
            return OP_ST_STATE;
        }
        if (cu == 0 || cu > OP_PRIME_MAX_CU || cu % pc) {
            return OP_ST_LIMIT;
        }
        fw->prime_remaining_cu = cu;
        return OP_ST_OK;
    }

    case OP_CMD_SET_TEMP: {
        uint16_t rate = get_u16(p);
        uint16_t duration = get_u16(p + 2);
        if (stopped) {
            return OP_ST_ALARM;
        }
        if (fw->state != OP_STATE_RUNNING) {
            return OP_ST_STATE;
        }
        if (rate > fw->lim.max_basal_cuph || duration < 5 || duration > fw->lim.max_temp_duration_min) {
            return OP_ST_LIMIT;
        }
        fw->temp_active = true;
        fw->temp_rate_cuph = rate;
        fw->temp_end = fw->minute + duration;
        return OP_ST_OK;
    }

    case OP_CMD_BOLUS: {
        uint16_t cu = get_u16(p);
        if (stopped) {
            return OP_ST_ALARM;
        }
        if (fw->state != OP_STATE_RUNNING) {
            return OP_ST_STATE;
        }
        if (cu == 0 || cu > fw->lim.max_bolus_cu || cu % pc) {
            return OP_ST_LIMIT;
        }
        if (fw->bolus_remaining_cu > 0 || cu > fw->reservoir_cu) {
            return OP_ST_STATE;
        }
        fw->bolus_remaining_cu = cu;
        return OP_ST_OK;
    }
    default:
        return OP_ST_UNKNOWN;
    }
}

static bool mutating(uint8_t cmd)
{
    return cmd != OP_CMD_STATUS && cmd != OP_CMD_HISTORY;
}

int op_fw_local_command(op_fw_t *fw, uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    uint8_t out[OP_MAX_PAYLOAD];
    uint8_t out_len;
    int st = execute(fw, cmd, payload, len, out, &out_len);
    if (st == OP_ST_OK && mutating(cmd)) {
        op_hal_state_changed(fw);
    }
    return st;
}

size_t op_fw_handle(op_fw_t *fw, const uint8_t *frame, size_t len, uint8_t *out, size_t out_cap)
{
    op_request_t req;
    int st = op_decode_request(fw->key, fw->key_len, frame, len, &req);
    if (st != OP_ST_OK) {
        uint8_t seq = len > 0 ? frame[0] : 0;
        uint8_t cmd = len > 1 ? frame[1] : 0;
        return op_encode(fw->key, fw->key_len, seq, (uint8_t)(cmd | 0x80), st, NULL, 0, out, out_cap);
    }
    if (fw->last_valid && req.seq == fw->last_seq && len == fw->last_req_len &&
        memcmp(frame, fw->last_req, len) == 0) {
        if (fw->last_resp_len > out_cap) {
            return 0;
        }
        memcpy(out, fw->last_resp, fw->last_resp_len);
        return fw->last_resp_len;   /* retried frame: answer again, never execute twice */
    }

    uint8_t payload[OP_MAX_PAYLOAD];
    uint8_t plen;
    st = execute(fw, req.cmd, req.payload, req.len, payload, &plen);
    size_t n = op_encode(fw->key, fw->key_len, req.seq, (uint8_t)(req.cmd | 0x80), st, payload, plen, out, out_cap);
    if (n && len <= sizeof fw->last_req && n <= sizeof fw->last_resp) {
        fw->last_valid = true;
        fw->last_seq = req.seq;
        fw->last_req_len = (uint16_t)len;
        memcpy(fw->last_req, frame, len);
        fw->last_resp_len = (uint16_t)n;
        memcpy(fw->last_resp, out, n);
    }
    if (st == OP_ST_OK && mutating(req.cmd)) {
        op_hal_state_changed(fw);
    }
    return n;
}

/* ---------- reboot ---------- */

op_snapshot_t op_fw_snapshot(const op_fw_t *fw)
{
    op_snapshot_t s;
    memset(&s, 0, sizeof s);
    s.minute = fw->minute;
    s.reservoir_cu = fw->reservoir_cu;
    s.primed_cu = fw->primed_cu;
    s.alarms = fw->alarms;
    s.state = fw->state;
    s.valid = 1;
    return s;
}

void op_fw_restore(op_fw_t *fw, const op_snapshot_t *snap)
{
    if (!snap->valid) {
        return;
    }
    fw->minute = snap->minute;
    fw->reservoir_cu = snap->reservoir_cu;
    fw->primed_cu = snap->primed_cu;
    fw->alarms = snap->alarms;
    fw->state = snap->state <= OP_STATE_PRIMING ? snap->state : OP_STATE_SUSPENDED;
}

void op_fw_history_restore(op_fw_t *fw, const op_record_t *rec)
{
    op_record_t *last = hist_last(fw);
    if (last && last->minute == rec->minute && last->kind == rec->kind) {
        last->cu = rec->cu;   /* later log entries carry the updated per-minute total */
        return;
    }
    if (last && rec->minute < last->minute) {
        return;               /* out-of-order entry: keep history sorted */
    }
    hist_append(fw, rec->minute, rec->kind, rec->cu);
}

void op_fw_power_cycle(op_fw_t *fw)
{
    fw->temp_active = false;
    fw->bolus_remaining_cu = 0;
    fw->prime_remaining_cu = 0;
    fw->basal_accum = 0;
    fw->last_valid = false;
    fw->alarms |= OP_WARN_RESTARTED;
}
