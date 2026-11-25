#include <zephyr/drivers/watchdog.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "app.h"
#include "op_hal.h"

LOG_MODULE_REGISTER(openpatch, LOG_LEVEL_INF);

op_fw_t g_fw;
struct app_config g_cfg;
K_MUTEX_DEFINE(g_fw_lock);
K_SEM_DEFINE(init_done, 0, 1);

static const struct device *const wdt = DEVICE_DT_GET(DT_NODELABEL(wdt0));
static int wdt_channel = -1;
static atomic_t alive_ms;

/* ---------- clock: epoch minutes, set by the controller (CMD_SET_TIME) ---------- */

static uint32_t base_minute;
static int64_t base_uptime_ms;

uint32_t app_clock_minute(void)
{
    return base_minute + (uint32_t)((k_uptime_get() - base_uptime_ms) / 60000);
}

void app_clock_set(uint32_t minute)
{
    base_minute = minute;
    base_uptime_ms = k_uptime_get();
}

/* ---------- glue ---------- */

bool app_provisioned(void)
{
    return g_cfg.key_len >= 16 && g_cfg.basal_n > 0 && g_cfg.cal.calibrated;
}

size_t app_handle_frame(const uint8_t *in, size_t len, uint8_t *out, size_t cap)
{
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    size_t n = op_fw_handle(&g_fw, in, len, out, cap);
    k_mutex_unlock(&g_fw_lock);
    return n;
}

void app_keepalive(void)
{
    atomic_set(&alive_ms, (atomic_val_t)k_uptime_get_32());
    if (wdt_channel >= 0) {
        wdt_feed(wdt, wdt_channel);
    }
}

void app_reconfigure(void)
{
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    if (!op_fw_configure(&g_fw, g_cfg.key, g_cfg.key_len, g_cfg.basal_start, g_cfg.basal_cuph, g_cfg.basal_n,
                         &g_cfg.lim)) {
        LOG_ERR("configuration rejected by firmware core");
    }
    k_mutex_unlock(&g_fw_lock);
}

/* ---------- persistence and time hooks from the core ---------- */

void op_hal_history_changed(op_fw_t *fw, const op_record_t *rec)
{
    ARG_UNUSED(fw);
    storage_history_append(rec);
}

void op_hal_state_changed(op_fw_t *fw)
{
    op_snapshot_t s = op_fw_snapshot(fw);
    storage_save_snapshot(&s);
}

void op_hal_time_set(op_fw_t *fw, uint32_t minute)
{
    ARG_UNUSED(fw);
    app_clock_set(minute);
}

/* ---------- UI thread: heartbeat, button, door, LEDs, alarm request ---------- */

static int64_t silenced_until;

static void ui_thread(void *a, void *b, void *c)
{
    ARG_UNUSED(a);
    ARG_UNUSED(b);
    ARG_UNUSED(c);
    bool hb = false;
    uint32_t tick = 0;
    int64_t press_start = -1;
    bool held_handled = false;

    k_sem_take(&init_done, K_FOREVER);
    for (;;) {
        k_msleep(50);
        tick++;
        uint32_t now = k_uptime_get_32();

        /* heartbeat only while the delivery loop is demonstrably alive */
        if (tick % 5 == 0) {
            bool healthy = (now - (uint32_t)atomic_get(&alive_ms)) < 3000;
            if (healthy) {
                hb = !hb;
                board_set_heartbeat(hb);
            }
        }

        /* button: short press silences warnings for 30 min, 5 s hold suspends delivery */
        if (board_button_pressed()) {
            if (press_start < 0) {
                press_start = now;
                held_handled = false;
            } else if (!held_handled && now - press_start >= 5000) {
                k_mutex_lock(&g_fw_lock, K_FOREVER);
                op_fw_local_command(&g_fw, OP_CMD_SUSPEND, NULL, 0);
                k_mutex_unlock(&g_fw_lock);
                LOG_WRN("suspended by button");
                held_handled = true;
            }
        } else if (press_start >= 0) {
            if (!held_handled && now - press_start < 1000) {
                silenced_until = k_uptime_get() + 30LL * 60 * 1000;
            }
            press_start = -1;
        }

        k_mutex_lock(&g_fw_lock, K_FOREVER);
        if (board_door_open() && g_fw.state == OP_STATE_RUNNING) {
            op_fw_local_command(&g_fw, OP_CMD_SUSPEND, NULL, 0);
            LOG_WRN("reservoir door opened: suspended");
        }
        uint16_t alarms = g_fw.alarms;
        uint8_t state = g_fw.state;
        k_mutex_unlock(&g_fw_lock);

        bool stop = (alarms & OP_STOP_MASK) != 0;
        bool warn = (alarms & (OP_WARN_RESERVOIR_LOW | OP_WARN_BATTERY_LOW | OP_WARN_RESTARTED)) != 0;
        board_set_alarm_request(stop || (warn && k_uptime_get() > silenced_until));

        bool blink = (tick / 10) % 2;
        board_leds(stop || (warn && blink), state == OP_STATE_RUNNING && !stop && blink,
                   !app_provisioned() || state == OP_STATE_NO_RESERVOIR || ble_connected());
    }
}

K_THREAD_DEFINE(ui_tid, 2048, ui_thread, NULL, NULL, NULL, 6, 0, 0);

/* ---------- delivery loop ---------- */

static void delivery_step(void)
{
    if (board_rewind_pending()) {
        if (!board_run_rewind()) {
            k_mutex_lock(&g_fw_lock, K_FOREVER);
            op_fw_raise(&g_fw, OP_ALARM_MOTOR);
            k_mutex_unlock(&g_fw_lock);
        }
    }
    uint32_t now = app_clock_minute();
    k_mutex_lock(&g_fw_lock, K_FOREVER);
    if (now > g_fw.minute) {
        uint32_t gap = now - g_fw.minute;
        if (gap <= 3) {
            for (uint32_t m = g_fw.minute + 1; m <= now; m++) {
                op_fw_tick(&g_fw, m);
            }
        } else {
            op_fw_tick(&g_fw, now);   /* never "catch up" hours of basal after a clock jump */
        }
    }
    k_mutex_unlock(&g_fw_lock);
}

static void watchdog_start(void)
{
    if (!device_is_ready(wdt)) {
        LOG_ERR("watchdog not ready");
        return;
    }
    struct wdt_timeout_cfg cfg = {
        .window = {.min = 0, .max = 8000},
        .callback = NULL,
        .flags = WDT_FLAG_RESET_SOC,
    };
    wdt_channel = wdt_install_timeout(wdt, &cfg);
    if (wdt_channel < 0 || wdt_setup(wdt, WDT_OPT_PAUSE_HALTED_BY_DBG) < 0) {
        LOG_ERR("watchdog setup failed");
        wdt_channel = -1;
    }
}

int main(void)
{
    static const uint16_t idle_start[1] = {0};
    static const uint16_t idle_rate[1] = {0};

    board_init();
    storage_init();
    storage_load_config(&g_cfg);
    storage_history_init();

    const uint16_t *starts = g_cfg.basal_n ? g_cfg.basal_start : idle_start;
    const uint16_t *rates = g_cfg.basal_n ? g_cfg.basal_cuph : idle_rate;
    size_t n = g_cfg.basal_n ? g_cfg.basal_n : 1;

    k_mutex_lock(&g_fw_lock, K_FOREVER);
    op_fw_init(&g_fw, NULL, g_cfg.key, g_cfg.key_len, starts, rates, n, &g_cfg.lim, 0, 0);
    if (g_cfg.snap.valid) {
        op_fw_restore(&g_fw, &g_cfg.snap);
        storage_history_replay(&g_fw);
        op_fw_power_cycle(&g_fw);    /* resume scheduled basal, nothing in flight, RESTARTED alarm */
    }
    app_clock_set(g_fw.minute);
    k_mutex_unlock(&g_fw_lock);

    board_apply_cal(&g_cfg.cal);
    board_update_battery();

    if (app_provisioned()) {
        ble_start(g_cfg.cal.passkey);
    } else {
        LOG_WRN("not provisioned: connect USB and use the 'op' shell (op status)");
    }

    watchdog_start();
    app_keepalive();
    k_sem_give(&init_done);

    uint32_t loops = 0;
    for (;;) {
        delivery_step();
        if (++loops % 60 == 0) {
            board_update_battery();
        }
        app_keepalive();
        k_msleep(1000);
    }
    return 0;
}
