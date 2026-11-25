#include "op_protocol.h"

#include <string.h>

uint16_t op_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)(data[i] << 8);
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

/* ---------- SHA-256 (FIPS 180-4) ---------- */

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

typedef struct {
    uint32_t h[8];
    uint8_t block[64];
    size_t fill;
    uint64_t total;
} sha256_ctx;

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(sha256_ctx *c, const uint8_t *p)
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) |
               (uint32_t)p[4 * i + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
    uint32_t e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & cc) ^ (b & cc));
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = cc;
        cc = b;
        b = a;
        a = t1 + t2;
    }
    c->h[0] += a;
    c->h[1] += b;
    c->h[2] += cc;
    c->h[3] += d;
    c->h[4] += e;
    c->h[5] += f;
    c->h[6] += g;
    c->h[7] += h;
}

static void sha256_init(sha256_ctx *c)
{
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c->h, iv, sizeof iv);
    c->fill = 0;
    c->total = 0;
}

static void sha256_update(sha256_ctx *c, const uint8_t *data, size_t len)
{
    c->total += len;
    while (len > 0) {
        size_t take = 64 - c->fill;
        if (take > len) {
            take = len;
        }
        memcpy(c->block + c->fill, data, take);
        c->fill += take;
        data += take;
        len -= take;
        if (c->fill == 64) {
            sha256_block(c, c->block);
            c->fill = 0;
        }
    }
}

static void sha256_final(sha256_ctx *c, uint8_t out[32])
{
    uint64_t bits = c->total * 8;
    uint8_t pad = 0x80;
    sha256_update(c, &pad, 1);
    uint8_t zero = 0;
    while (c->fill != 56) {
        sha256_update(c, &zero, 1);
    }
    uint8_t len_be[8];
    for (int i = 0; i < 8; i++) {
        len_be[i] = (uint8_t)(bits >> (56 - 8 * i));
    }
    sha256_update(c, len_be, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (uint8_t)(c->h[i] >> 24);
        out[4 * i + 1] = (uint8_t)(c->h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(c->h[i] >> 8);
        out[4 * i + 3] = (uint8_t)c->h[i];
    }
}

void op_sha256(const uint8_t *data, size_t len, uint8_t out[32])
{
    sha256_ctx c;
    sha256_init(&c);
    sha256_update(&c, data, len);
    sha256_final(&c, out);
}

void op_hmac_tag(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t len, uint8_t out[OP_TAG_LEN])
{
    uint8_t k[64] = {0};
    if (key_len > 64) {
        op_sha256(key, key_len, k);
    } else if (key_len > 0) {
        memcpy(k, key, key_len);
    }
    uint8_t pad[64];
    uint8_t inner[32];
    sha256_ctx c;

    for (int i = 0; i < 64; i++) {
        pad[i] = k[i] ^ 0x36;
    }
    sha256_init(&c);
    sha256_update(&c, pad, 64);
    sha256_update(&c, msg, len);
    sha256_final(&c, inner);

    for (int i = 0; i < 64; i++) {
        pad[i] = k[i] ^ 0x5c;
    }
    uint8_t full[32];
    sha256_init(&c);
    sha256_update(&c, pad, 64);
    sha256_update(&c, inner, 32);
    sha256_final(&c, full);
    memcpy(out, full, OP_TAG_LEN);
}

/* ---------- framing ---------- */

size_t op_encode(const uint8_t *key, size_t key_len, uint8_t seq, uint8_t cmd, int status,
                 const uint8_t *payload, size_t payload_len, uint8_t *out, size_t out_cap)
{
    size_t head = status < 0 ? 3 : 4;
    size_t total = head + payload_len + OP_TAG_LEN + 2;
    if (payload_len > OP_MAX_PAYLOAD || total > out_cap) {
        return 0;
    }
    size_t i = 0;
    out[i++] = seq;
    out[i++] = cmd;
    if (status >= 0) {
        out[i++] = (uint8_t)status;
    }
    out[i++] = (uint8_t)payload_len;
    if (payload_len) {
        memcpy(out + i, payload, payload_len);
    }
    i += payload_len;
    op_hmac_tag(key, key_len, out, i, out + i);
    i += OP_TAG_LEN;
    uint16_t crc = op_crc16(out, i);
    out[i++] = (uint8_t)(crc >> 8);
    out[i++] = (uint8_t)crc;
    return i;
}

static int ct_equal(const uint8_t *a, const uint8_t *b, size_t n)
{
    uint8_t diff = 0;
    for (size_t i = 0; i < n; i++) {
        diff |= (uint8_t)(a[i] ^ b[i]);
    }
    return diff == 0;
}

int op_decode_request(const uint8_t *key, size_t key_len, const uint8_t *frame, size_t len, op_request_t *req)
{
    const size_t head = 3;
    if (len < head + OP_TAG_LEN + 2) {
        return OP_ST_FORMAT;
    }
    uint16_t crc = (uint16_t)((frame[len - 2] << 8) | frame[len - 1]);
    if (op_crc16(frame, len - 2) != crc) {
        return OP_ST_CRC;
    }
    size_t plen = frame[head - 1];
    if (len != head + plen + OP_TAG_LEN + 2) {
        return OP_ST_FORMAT;
    }
    uint8_t tag[OP_TAG_LEN];
    op_hmac_tag(key, key_len, frame, head + plen, tag);
    if (!ct_equal(tag, frame + head + plen, OP_TAG_LEN)) {
        return OP_ST_AUTH;
    }
    req->seq = frame[0];
    req->cmd = frame[1];
    req->len = (uint8_t)plen;
    req->payload = frame + head;
    return OP_ST_OK;
}
