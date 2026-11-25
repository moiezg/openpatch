#ifndef APP_H
#define APP_H

#include <stdbool.h>
#include <stdint.h>
#include <zephyr/kernel.h>

#include "op_fw.h"

struct app_cal {
    uint32_t steps_per_pulse_x100;   /* motor steps per 0.05 U, x100 (from gravimetric calibration) */
    int32_t force_zero;              /* HX711 raw reading with no load */
    int32_t force_counts_per_unit;   /* HX711 counts per force unit used by occlusion_threshold */
    uint32_t passkey;                /* 6-digit BLE pairing passkey */
    uint8_t calibrated;
};

struct app_config {
    uint8_t key[OP_MAX_KEY];
    uint8_t key_len;
    op_limits_t lim;
    uint8_t basal_n;
    uint16_t basal_start[OP_MAX_BASAL_ENTRIES];
    uint16_t basal_cuph[OP_MAX_BASAL_ENTRIES];
    struct app_cal cal;
    op_snapshot_t snap;
};

extern op_fw_t g_fw;
extern struct app_config g_cfg;
extern struct k_mutex g_fw_lock;

bool app_provisioned(void);
size_t app_handle_frame(const uint8_t *in, size_t len, uint8_t *out, size_t cap);
void app_keepalive(void);            /* long motor operations call this to keep watchdog + heartbeat alive */
void app_reconfigure(void);          /* re-apply key/basal/limits after a shell change */
uint32_t app_clock_minute(void);
void app_clock_set(uint32_t minute);

/* board.c */
int board_init(void);
void board_apply_cal(const struct app_cal *cal);
bool board_rewind_pending(void);
bool board_run_rewind(void);
int32_t board_read_force_raw(void);
uint16_t board_battery_mv(void);
void board_update_battery(void);
void board_set_heartbeat(bool level);
void board_set_alarm_request(bool on);
bool board_motor_power_off(void);
bool board_button_pressed(void);
bool board_door_open(void);
void board_leds(bool red, bool green, bool blue);
int board_bench_pulses(uint32_t n);

/* ble.c */
int ble_start(uint32_t passkey);
void ble_open_pairing_window(uint32_t seconds);
int ble_unpair_all(void);
bool ble_connected(void);

/* storage.c */
int storage_init(void);
int storage_load_config(struct app_config *cfg);
int storage_save_key(void);
int storage_save_limits(void);
int storage_save_basal(void);
int storage_save_cal(void);
void storage_save_snapshot(const op_snapshot_t *snap);
int storage_history_init(void);
void storage_history_append(const op_record_t *rec);
int storage_history_replay(op_fw_t *fw);

#endif
