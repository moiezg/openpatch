#ifndef OP_PROTOCOL_H
#define OP_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Wire protocol, identical to openpatch/pump/protocol.py.
 * Request : seq | cmd | len | payload | hmac8 | crc16
 * Response: seq | cmd|0x80 | status | len | payload | hmac8 | crc16 */

#define OP_TAG_LEN 8
#define OP_MAX_PAYLOAD 255
#define OP_MAX_FRAME (4 + OP_MAX_PAYLOAD + OP_TAG_LEN + 2)
#define OP_MAX_KEY 64

enum {
    OP_CMD_STATUS = 0x01,
    OP_CMD_SET_TEMP = 0x02,
    OP_CMD_CANCEL_TEMP = 0x03,
    OP_CMD_BOLUS = 0x04,
    OP_CMD_HISTORY = 0x05,
    OP_CMD_SUSPEND = 0x06,
    OP_CMD_RESUME = 0x07,
    OP_CMD_SET_TIME = 0x08,
    OP_CMD_CLEAR_ALARMS = 0x09,
    OP_CMD_CANCEL_BOLUS = 0x0A,
    OP_CMD_REWIND = 0x0B,
    OP_CMD_SET_RESERVOIR = 0x0C,
    OP_CMD_PRIME = 0x0D,
};

enum {
    OP_ST_OK = 0x00,
    OP_ST_CRC = 0x01,
    OP_ST_AUTH = 0x02,
    OP_ST_LIMIT = 0x03,
    OP_ST_STATE = 0x04,
    OP_ST_UNKNOWN = 0x05,
    OP_ST_ALARM = 0x06,
    OP_ST_FORMAT = 0x07,
};

typedef struct {
    uint8_t seq;
    uint8_t cmd;
    uint8_t len;
    const uint8_t *payload;
} op_request_t;

uint16_t op_crc16(const uint8_t *data, size_t len);
void op_sha256(const uint8_t *data, size_t len, uint8_t out[32]);
void op_hmac_tag(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t len, uint8_t out[OP_TAG_LEN]);

/* status < 0 encodes a request (no status byte). Returns frame length, 0 if it does not fit. */
size_t op_encode(const uint8_t *key, size_t key_len, uint8_t seq, uint8_t cmd, int status,
                 const uint8_t *payload, size_t payload_len, uint8_t *out, size_t out_cap);

/* Validates a request frame; returns OP_ST_OK or the error status (same order of checks as Python). */
int op_decode_request(const uint8_t *key, size_t key_len, const uint8_t *frame, size_t len, op_request_t *req);

#ifdef __cplusplus
}
#endif

#endif
