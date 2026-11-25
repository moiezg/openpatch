#include <string.h>
#include <zephyr/fs/fcb.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>
#include <zephyr/storage/flash_map.h>

#include "app.h"

LOG_MODULE_REGISTER(storage, LOG_LEVEL_INF);

/* ---------- configuration + state snapshot: Zephyr settings ("op/...") on NVS ---------- */

static struct app_config *loading;

static int op_settings_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
    const char *next;
    struct app_config *c = loading;
    if (!c) {
        return 0;
    }
    if (settings_name_steq(name, "key", &next) && !next) {
        if (len > sizeof c->key) {
            return -EINVAL;
        }
        ssize_t n = read_cb(cb_arg, c->key, len);
        c->key_len = n > 0 ? (uint8_t)n : 0;
        return 0;
    }
    if (settings_name_steq(name, "limits", &next) && !next && len == sizeof c->lim) {
        return read_cb(cb_arg, &c->lim, len) == (ssize_t)len ? 0 : -EIO;
    }
    if (settings_name_steq(name, "basal", &next) && !next) {
        struct {
            uint8_t n;
            uint16_t start[OP_MAX_BASAL_ENTRIES];
            uint16_t cuph[OP_MAX_BASAL_ENTRIES];
        } b;
        if (len != sizeof b || read_cb(cb_arg, &b, len) != (ssize_t)len || b.n > OP_MAX_BASAL_ENTRIES) {
            return -EINVAL;
        }
        c->basal_n = b.n;
        memcpy(c->basal_start, b.start, sizeof b.start);
        memcpy(c->basal_cuph, b.cuph, sizeof b.cuph);
        return 0;
    }
    if (settings_name_steq(name, "cal", &next) && !next && len == sizeof c->cal) {
        return read_cb(cb_arg, &c->cal, len) == (ssize_t)len ? 0 : -EIO;
    }
    if (settings_name_steq(name, "snap", &next) && !next && len == sizeof c->snap) {
        return read_cb(cb_arg, &c->snap, len) == (ssize_t)len ? 0 : -EIO;
    }
    return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(op, "op", NULL, op_settings_set, NULL, NULL);

int storage_init(void)
{
    int err = settings_subsys_init();
    if (err) {
        LOG_ERR("settings init failed (%d)", err);
    }
    return err;
}

int storage_load_config(struct app_config *cfg)
{
    memset(cfg, 0, sizeof *cfg);
    /* conservative defaults until provisioned over USB */
    cfg->lim = (op_limits_t){
        .max_basal_cuph = 200, .max_bolus_cu = 500, .hourly_cap_cu = 800, .max_temp_duration_min = 120,
        .pulse_cu = 5, .bolus_rate_cu_per_min = 150, .occlusion_threshold = 0xFFFE,
    };
    cfg->cal.steps_per_pulse_x100 = 1400;
    cfg->cal.force_counts_per_unit = 100;
    loading = cfg;
    int err = settings_load_subtree("op");
    loading = NULL;
    return err;
}

int storage_save_key(void)
{
    return settings_save_one("op/key", g_cfg.key, g_cfg.key_len);
}

int storage_save_limits(void)
{
    return settings_save_one("op/limits", &g_cfg.lim, sizeof g_cfg.lim);
}

int storage_save_basal(void)
{
    struct {
        uint8_t n;
        uint16_t start[OP_MAX_BASAL_ENTRIES];
        uint16_t cuph[OP_MAX_BASAL_ENTRIES];
    } b;
    memset(&b, 0, sizeof b);
    b.n = g_cfg.basal_n;
    memcpy(b.start, g_cfg.basal_start, sizeof b.start);
    memcpy(b.cuph, g_cfg.basal_cuph, sizeof b.cuph);
    return settings_save_one("op/basal", &b, sizeof b);
}

int storage_save_cal(void)
{
    return settings_save_one("op/cal", &g_cfg.cal, sizeof g_cfg.cal);
}

void storage_save_snapshot(const op_snapshot_t *snap)
{
    static op_snapshot_t last;
    if (memcmp(&last, snap, sizeof last) == 0) {
        return;
    }
    if (settings_save_one("op/snap", snap, sizeof *snap) == 0) {
        last = *snap;
    }
}

/* ---------- delivery history: append-only flash circular buffer ---------- */

#define HIST_AREA FIXED_PARTITION_ID(history_partition)
#define HIST_MAGIC 0x4f504831u   /* "OPH1" */

static struct fcb hist_fcb;
static struct flash_sector hist_sectors[64];
static bool hist_ready;

struct hist_entry {
    uint32_t minute;
    uint16_t cu;
    uint8_t kind;
    uint8_t check;
} __packed;

static uint8_t entry_check(const struct hist_entry *e)
{
    return (uint8_t)(0xA5 ^ e->minute ^ (e->minute >> 8) ^ (e->minute >> 16) ^ (e->minute >> 24) ^ e->cu ^
                     (e->cu >> 8) ^ e->kind);
}

int storage_history_init(void)
{
    uint32_t cnt = ARRAY_SIZE(hist_sectors);
    int err = flash_area_get_sectors(HIST_AREA, &cnt, hist_sectors);
    if (err) {
        LOG_ERR("history sectors (%d)", err);
        return err;
    }
    hist_fcb.f_magic = HIST_MAGIC;
    hist_fcb.f_version = 1;
    hist_fcb.f_sector_cnt = (uint8_t)cnt;
    hist_fcb.f_scratch_cnt = 0;
    hist_fcb.f_sectors = hist_sectors;
    err = fcb_init(HIST_AREA, &hist_fcb);
    if (err) {
        LOG_WRN("history log unreadable (%d): erasing", err);
        const struct flash_area *fa;
        if (flash_area_open(HIST_AREA, &fa) == 0) {
            flash_area_erase(fa, 0, fa->fa_size);
            flash_area_close(fa);
        }
        err = fcb_init(HIST_AREA, &hist_fcb);
    }
    hist_ready = err == 0;
    return err;
}

void storage_history_append(const op_record_t *rec)
{
    if (!hist_ready) {
        return;
    }
    struct hist_entry e = {.minute = rec->minute, .cu = rec->cu, .kind = rec->kind};
    e.check = entry_check(&e);
    struct fcb_entry loc;
    int err = fcb_append(&hist_fcb, sizeof e, &loc);
    if (err == -ENOSPC) {
        fcb_rotate(&hist_fcb);   /* drop the oldest sector (far older than the 48 h the core keeps) */
        err = fcb_append(&hist_fcb, sizeof e, &loc);
    }
    if (err) {
        LOG_ERR("history append (%d)", err);
        return;
    }
    flash_area_write(hist_fcb.fap, FCB_ENTRY_FA_DATA_OFF(loc), &e, sizeof e);
    fcb_append_finish(&hist_fcb, &loc);
}

static int replay_cb(struct fcb_entry_ctx *ctx, void *arg)
{
    struct hist_entry e;
    if (ctx->loc.fe_data_len != sizeof e ||
        flash_area_read(ctx->fap, FCB_ENTRY_FA_DATA_OFF(ctx->loc), &e, sizeof e) != 0 || e.check != entry_check(&e)) {
        return 0;
    }
    op_record_t r = {.minute = e.minute, .cu = e.cu, .kind = e.kind};
    op_fw_history_restore((op_fw_t *)arg, &r);
    return 0;
}

int storage_history_replay(op_fw_t *fw)
{
    if (!hist_ready) {
        return -ENODEV;
    }
    return fcb_walk(&hist_fcb, NULL, replay_cb, fw);
}
