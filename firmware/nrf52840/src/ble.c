#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>

#include "app.h"

LOG_MODULE_REGISTER(ble, LOG_LEVEL_INF);

/* GATT layout shared with openpatch/pump/ble.py */
static struct bt_uuid_128 svc_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x6f700001, 0x0b5a, 0x4c5e, 0x9d3e, 0x0e0a7c1d0001));
static struct bt_uuid_128 cmd_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x6f700002, 0x0b5a, 0x4c5e, 0x9d3e, 0x0e0a7c1d0001));
static struct bt_uuid_128 rsp_uuid =
    BT_UUID_INIT_128(BT_UUID_128_ENCODE(0x6f700003, 0x0b5a, 0x4c5e, 0x9d3e, 0x0e0a7c1d0001));

static struct bt_conn *current;
static int64_t pairing_until;

/* Commands run on their own work queue: never on the Bluetooth host's threads */
K_THREAD_STACK_DEFINE(cmd_stack, 3072);
static struct k_work_q cmd_q;
static struct k_work cmd_work;
static uint8_t req_buf[OP_MAX_FRAME];
static size_t req_len;
static struct bt_conn *req_conn;
static atomic_t req_busy;

static ssize_t on_cmd_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                            uint16_t offset, uint8_t flags);

BT_GATT_SERVICE_DEFINE(op_svc,
    BT_GATT_PRIMARY_SERVICE(&svc_uuid),
    BT_GATT_CHARACTERISTIC(&cmd_uuid.uuid, BT_GATT_CHRC_WRITE, BT_GATT_PERM_WRITE_AUTHEN, NULL, on_cmd_write, NULL),
    BT_GATT_CHARACTERISTIC(&rsp_uuid.uuid, BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
    BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE_AUTHEN),
);

static void cmd_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    static uint8_t rsp[OP_MAX_FRAME];
    size_t n = app_handle_frame(req_buf, req_len, rsp, sizeof rsp);
    if (n && req_conn) {
        int err = bt_gatt_notify(req_conn, &op_svc.attrs[4], rsp, (uint16_t)n);
        if (err) {
            LOG_WRN("notify failed (%d)", err);
        }
    }
    if (req_conn) {
        bt_conn_unref(req_conn);
        req_conn = NULL;
    }
    atomic_set(&req_busy, 0);
}

static ssize_t on_cmd_write(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf, uint16_t len,
                            uint16_t offset, uint8_t flags)
{
    ARG_UNUSED(attr);
    ARG_UNUSED(flags);
    if (offset != 0) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
    }
    if (len > sizeof req_buf) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    if (!atomic_cas(&req_busy, 0, 1)) {
        return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);   /* one command at a time; the controller retries */
    }
    memcpy(req_buf, buf, len);
    req_len = len;
    req_conn = bt_conn_ref(conn);
    k_work_submit_to_queue(&cmd_q, &cmd_work);
    return len;
}

/* ---------- connection and pairing ---------- */

static const struct bt_data ad[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_128_ENCODE(0x6f700001, 0x0b5a, 0x4c5e, 0x9d3e, 0x0e0a7c1d0001)),
};
static const struct bt_data sd[] = {
    BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME, sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static void advertise(void)
{
    int err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_1, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
    if (err && err != -EALREADY) {
        LOG_ERR("advertising failed (%d)", err);
    }
}

static void on_connected(struct bt_conn *conn, uint8_t err)
{
    if (err) {
        return;
    }
    current = bt_conn_ref(conn);
    bt_conn_set_security(conn, BT_SECURITY_L4);
    LOG_INF("connected");
}

static void on_disconnected(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(conn);
    if (current) {
        bt_conn_unref(current);
        current = NULL;
    }
    LOG_INF("disconnected (0x%02x)", reason);
}

static void on_recycled(void)
{
    advertise();
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected = on_connected,
    .disconnected = on_disconnected,
    .recycled = on_recycled,
};

static unsigned int bond_count;

static void count_bond(const struct bt_bond_info *info, void *data)
{
    ARG_UNUSED(info);
    ARG_UNUSED(data);
    bond_count++;
}

static enum bt_security_err pairing_accept(struct bt_conn *conn, const struct bt_conn_pairing_feat *const feat)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(feat);
    bond_count = 0;
    bt_foreach_bond(BT_ID_DEFAULT, count_bond, NULL);
    if (bond_count == 0 || k_uptime_get() < pairing_until) {
        return BT_SECURITY_ERR_SUCCESS;
    }
    LOG_WRN("pairing refused: open a window with 'op pair' on the USB shell");
    return BT_SECURITY_ERR_PAIR_NOT_ALLOWED;
}

static void passkey_display(struct bt_conn *conn, unsigned int passkey)
{
    ARG_UNUSED(conn);
    LOG_INF("pairing passkey: %06u", passkey);
}

static void auth_cancel(struct bt_conn *conn)
{
    ARG_UNUSED(conn);
}

static struct bt_conn_auth_cb auth_cb = {
    .pairing_accept = pairing_accept,
    .passkey_display = passkey_display,
    .cancel = auth_cancel,
};

static void pairing_complete(struct bt_conn *conn, bool bonded)
{
    ARG_UNUSED(conn);
    LOG_INF("paired (bonded=%d)", bonded);
}

static void pairing_failed(struct bt_conn *conn, enum bt_security_err reason)
{
    ARG_UNUSED(conn);
    LOG_WRN("pairing failed (%d)", reason);
}

static struct bt_conn_auth_info_cb auth_info_cb = {
    .pairing_complete = pairing_complete,
    .pairing_failed = pairing_failed,
};

int ble_start(uint32_t passkey)
{
    k_work_queue_init(&cmd_q);
    k_work_queue_start(&cmd_q, cmd_stack, K_THREAD_STACK_SIZEOF(cmd_stack), 5, NULL);
    k_work_init(&cmd_work, cmd_work_handler);

    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (%d)", err);
        return err;
    }
    settings_load();   /* bonds */
    bt_passkey_set(passkey % 1000000);
    bt_conn_auth_cb_register(&auth_cb);
    bt_conn_auth_info_cb_register(&auth_info_cb);
    advertise();
    return 0;
}

void ble_open_pairing_window(uint32_t seconds)
{
    pairing_until = k_uptime_get() + (int64_t)seconds * 1000;
}

int ble_unpair_all(void)
{
    return bt_unpair(BT_ID_DEFAULT, BT_ADDR_LE_ANY);
}

bool ble_connected(void)
{
    return current != NULL;
}
