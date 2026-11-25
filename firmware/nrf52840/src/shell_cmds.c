#include <stdlib.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/shell/shell.h>

#include "app.h"

/* USB provisioning and bench shell. Every command that changes configuration requires the pump to be
 * out of RUNNING state: keys and limits are never changed while insulin is being delivered. */

static bool require_idle(const struct shell *sh)
{
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    bool idle = g_fw.state != OP_STATE_RUNNING;
    k_mutex_unlock(&g_fw_lock);
    if (!idle) {
        shell_error(sh, "suspend delivery first (hold the button 5 s, or send SUSPEND)");
    }
    return idle;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    static const char *states[] = {"running", "suspended", "no_reservoir", "priming"};
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    shell_print(sh, "state %s  minute %u  reservoir %u.%02u U  alarms 0x%04x  last-hour %u cU",
                states[g_fw.state & 3], g_fw.minute, g_fw.reservoir_cu / 100, g_fw.reservoir_cu % 100, g_fw.alarms,
                op_fw_window_cu(&g_fw));
    k_mutex_unlock(&g_fw_lock);
    shell_print(sh, "provisioned %s  key %s  basal entries %u  calibrated %s  battery %u mV  ble %s",
                app_provisioned() ? "yes" : "NO", g_cfg.key_len >= 16 ? "set" : "MISSING", g_cfg.basal_n,
                g_cfg.cal.calibrated ? "yes" : "NO", board_battery_mv(), ble_connected() ? "connected" : "idle");
    shell_print(sh, "limits: max basal %u cU/h, max bolus %u cU, hourly cap %u cU, occlusion %u",
                g_cfg.lim.max_basal_cuph, g_cfg.lim.max_bolus_cu, g_cfg.lim.hourly_cap_cu,
                g_cfg.lim.occlusion_threshold);
    shell_print(sh, "cal: %u steps/pulse x100, force zero %d, %d counts/unit, passkey %06u",
                g_cfg.cal.steps_per_pulse_x100, g_cfg.cal.force_zero, g_cfg.cal.force_counts_per_unit,
                g_cfg.cal.passkey);
    return 0;
}

static int cmd_key(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    if (!require_idle(sh)) {
        return -EPERM;
    }
    size_t len = strlen(argv[1]);
    if (len < 32 || len > 2 * OP_MAX_KEY || len % 2) {
        shell_error(sh, "key must be 32..128 hex characters (16..64 bytes)");
        return -EINVAL;
    }
    for (size_t i = 0; i < len / 2; i++) {
        int hi = hexval(argv[1][2 * i]), lo = hexval(argv[1][2 * i + 1]);
        if (hi < 0 || lo < 0) {
            shell_error(sh, "not hex");
            return -EINVAL;
        }
        g_cfg.key[i] = (uint8_t)(hi << 4 | lo);
    }
    g_cfg.key_len = (uint8_t)(len / 2);
    storage_save_key();
    app_reconfigure();
    shell_print(sh, "key stored (%u bytes). Put the same hex in the controller config.", g_cfg.key_len);
    return 0;
}

static int cmd_limits(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    if (!require_idle(sh)) {
        return -EPERM;
    }
    long basal = strtol(argv[1], NULL, 10), bolus = strtol(argv[2], NULL, 10), cap = strtol(argv[3], NULL, 10);
    if (basal < 5 || basal > 1000 || bolus < 5 || bolus > 2500 || cap < bolus || cap > 3000) {
        shell_error(sh, "usage: op limits <max_basal_cU/h 5..1000> <max_bolus_cU 5..2500> <hourly_cap_cU>=bolus>");
        return -EINVAL;
    }
    g_cfg.lim.max_basal_cuph = (uint16_t)basal;
    g_cfg.lim.max_bolus_cu = (uint16_t)bolus;
    g_cfg.lim.hourly_cap_cu = (uint16_t)cap;
    storage_save_limits();
    app_reconfigure();
    shell_print(sh, "limits stored. Make the safety MCU ceilings match (see firmware/README.md).");
    return 0;
}

static int cmd_basal(const struct shell *sh, size_t argc, char **argv)
{
    if (!require_idle(sh)) {
        return -EPERM;
    }
    if (argc - 1 > OP_MAX_BASAL_ENTRIES) {
        shell_error(sh, "at most %d entries", OP_MAX_BASAL_ENTRIES);
        return -EINVAL;
    }
    uint16_t start[OP_MAX_BASAL_ENTRIES], rate[OP_MAX_BASAL_ENTRIES];
    for (size_t i = 1; i < argc; i++) {
        char *colon = strchr(argv[i], ':');
        if (!colon) {
            shell_error(sh, "usage: op basal <minute>:<cU/h> ...   e.g. op basal 0:80 240:100 480:80");
            return -EINVAL;
        }
        long m = strtol(argv[i], NULL, 10), r = strtol(colon + 1, NULL, 10);
        if (m < 0 || m >= 1440 || r < 0 || r > g_cfg.lim.max_basal_cuph || (i == 1 && m != 0) ||
            (i > 1 && m <= start[i - 2])) {
            shell_error(sh, "entry %u invalid (first must start at 0, ascending, rate <= max basal)", (unsigned)i);
            return -EINVAL;
        }
        start[i - 1] = (uint16_t)m;
        rate[i - 1] = (uint16_t)r;
    }
    g_cfg.basal_n = (uint8_t)(argc - 1);
    memcpy(g_cfg.basal_start, start, sizeof(uint16_t) * g_cfg.basal_n);
    memcpy(g_cfg.basal_cuph, rate, sizeof(uint16_t) * g_cfg.basal_n);
    storage_save_basal();
    app_reconfigure();
    shell_print(sh, "basal schedule stored (%u entries)", g_cfg.basal_n);
    return 0;
}

static int cmd_cal_steps(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    long v = strtol(argv[1], NULL, 10);
    if (v < 100 || v > 100000) {
        shell_error(sh, "steps per pulse x100 must be 100..100000");
        return -EINVAL;
    }
    g_cfg.cal.steps_per_pulse_x100 = (uint32_t)v;
    storage_save_cal();
    board_apply_cal(&g_cfg.cal);
    return 0;
}

static int cmd_cal_zero(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    int64_t sum = 0;
    for (int i = 0; i < 16; i++) {
        int32_t raw = board_read_force_raw();
        if (raw == INT32_MIN) {
            shell_error(sh, "load cell not responding");
            return -EIO;
        }
        sum += raw;
    }
    g_cfg.cal.force_zero = (int32_t)(sum / 16);
    storage_save_cal();
    board_apply_cal(&g_cfg.cal);
    shell_print(sh, "force zero = %d", g_cfg.cal.force_zero);
    return 0;
}

static int cmd_cal_force(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    long counts = strtol(argv[1], NULL, 10), threshold = strtol(argv[2], NULL, 10);
    if (counts <= 0 || threshold <= 0 || threshold >= 0xFFFE) {
        shell_error(sh, "usage: op cal force <counts_per_unit> <occlusion_threshold_units>");
        return -EINVAL;
    }
    g_cfg.cal.force_counts_per_unit = (int32_t)counts;
    g_cfg.lim.occlusion_threshold = (uint16_t)threshold;
    storage_save_cal();
    storage_save_limits();
    board_apply_cal(&g_cfg.cal);
    app_reconfigure();
    return 0;
}

static int cmd_cal_raw(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    shell_print(sh, "raw %d", board_read_force_raw());
    return 0;
}

static int cmd_cal_done(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    if (g_cfg.lim.occlusion_threshold == 0xFFFE) {
        shell_error(sh, "set the occlusion threshold first (op cal force)");
        return -EINVAL;
    }
    if (g_cfg.cal.passkey == 0) {
        g_cfg.cal.passkey = 100000 + sys_rand32_get() % 900000;
    }
    g_cfg.cal.calibrated = 1;
    storage_save_cal();
    shell_print(sh, "calibration marked complete. BLE passkey: %06u. Reboot to start Bluetooth.", g_cfg.cal.passkey);
    return 0;
}

static int cmd_bench(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    /* only in PRIMING: reachable solely after a rewind + reservoir change, never with a set on the body */
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    bool priming = g_fw.state == OP_STATE_PRIMING;
    k_mutex_unlock(&g_fw_lock);
    if (!priming) {
        shell_error(sh, "bench pulses only in PRIMING state (rewind, then set reservoir with saline)");
        return -EPERM;
    }
    long n = strtol(argv[1], NULL, 10);
    if (n <= 0 || n > 2000) {
        shell_error(sh, "usage: op bench <pulses 1..2000>  (saline only, set disconnected from any body)");
        return -EINVAL;
    }
    int r = board_bench_pulses((uint32_t)n);
    if (r == 0) {
        shell_print(sh, "delivered %ld pulses: weigh the output (1 pulse = 0.5 mg of water)", n);
    } else {
        shell_error(sh, "stopped early (hal error %d)", r);
    }
    return r;
}

static int cmd_time(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    uint32_t minute = (uint32_t)strtoul(argv[1], NULL, 10);
    uint8_t p[4] = {(uint8_t)(minute >> 24), (uint8_t)(minute >> 16), (uint8_t)(minute >> 8), (uint8_t)minute};
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    int st = op_fw_local_command(&g_fw, OP_CMD_SET_TIME, p, 4);
    k_mutex_unlock(&g_fw_lock);
    if (st == OP_ST_OK) {
        shell_print(sh, "time set");
    } else {
        shell_error(sh, "rejected (status %d)", st);
    }
    return 0;
}

static int cmd_pair(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    ble_open_pairing_window(120);
    shell_print(sh, "pairing allowed for 120 s. Passkey: %06u", g_cfg.cal.passkey);
    return 0;
}

static int cmd_unpair(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc);
    ARG_UNUSED(argv);
    shell_print(sh, "unpair: %d", ble_unpair_all());
    return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_cal,
    SHELL_CMD_ARG(steps, NULL, "<steps_per_pulse_x100>", cmd_cal_steps, 2, 0),
    SHELL_CMD_ARG(zero, NULL, "tare the load cell (no load on the plunger)", cmd_cal_zero, 1, 0),
    SHELL_CMD_ARG(force, NULL, "<counts_per_unit> <occlusion_threshold>", cmd_cal_force, 3, 0),
    SHELL_CMD_ARG(raw, NULL, "print a raw load-cell reading", cmd_cal_raw, 1, 0),
    SHELL_CMD_ARG(done, NULL, "mark calibration complete, generate passkey", cmd_cal_done, 1, 0),
    SHELL_SUBCMD_SET_END);

SHELL_STATIC_SUBCMD_SET_CREATE(sub_op,
    SHELL_CMD_ARG(status, NULL, "pump, provisioning and calibration status", cmd_status, 1, 0),
    SHELL_CMD_ARG(key, NULL, "<hex> set the 16..64 byte command key", cmd_key, 2, 0),
    SHELL_CMD_ARG(limits, NULL, "<max_basal_cU/h> <max_bolus_cU> <hourly_cap_cU>", cmd_limits, 4, 0),
    SHELL_CMD_ARG(basal, NULL, "<min>:<cU/h> ... scheduled basal", cmd_basal, 2, OP_MAX_BASAL_ENTRIES - 1),
    SHELL_CMD(cal, &sub_cal, "calibration", NULL),
    SHELL_CMD_ARG(bench, NULL, "<pulses> gravimetric test (saline only)", cmd_bench, 2, 0),
    SHELL_CMD_ARG(time, NULL, "<epoch_minutes>", cmd_time, 2, 0),
    SHELL_CMD_ARG(pair, NULL, "allow BLE pairing for 120 s", cmd_pair, 1, 0),
    SHELL_CMD_ARG(unpair, NULL, "forget all bonded controllers", cmd_unpair, 1, 0),
    SHELL_SUBCMD_SET_END);

SHELL_CMD_REGISTER(op, &sub_op, "OpenPatch pump", NULL);
