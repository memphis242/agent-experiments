/*
 * Black-box test of the CMDVAL library (spec S3.1, S3.2) through its pinned public API only.
 *
 * Types and prototypes are declared here from the specification (no library header is included).
 * Expected digests are published test vectors (FIPS 180-4 examples, RFC 4231) or were computed
 * independently with Python's hashlib/hmac/binascii. Frames are built by this file's own CRC-16 and
 * HMAC-SHA-256, so a defect shared by the library's frame code and its validation cannot hide.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"
#include "cfe.h"

/* ------------------------------------------------------------------------- the pinned API (S3.2) */

typedef struct
{
    uint8  HmacKey[32];
    uint16 AllowedSources[8];
    uint32 MaxAgeSeconds;
    uint8  EnableMask;
    uint8  Spare[3];
} cv_config_t;

typedef struct
{
    uint16       SourceId;
    uint32       TimeSeconds;
    uint32       Sequence;
    const uint8 *Command;
    size_t       CommandLen;
} cv_info_t;

int32  CMDVAL_LibInit(void);
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len);
void   CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]);
void   CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32]);
int    CMDVAL_CheckSource(uint16 SourceId, const cv_config_t *Config);
int    CMDVAL_CheckAge(uint32 FrameSeconds, uint32 NowSeconds, uint32 MaxAgeSeconds);
int    CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const cv_config_t *Config, uint32 NowSeconds,
                            cv_info_t *Info);

#define ST_OK     0
#define ST_FORMAT 1
#define ST_CRC    2
#define ST_HMAC   3
#define ST_SOURCE 4
#define ST_AGE    5

#define CHK_CRC    0x01
#define CHK_HMAC   0x02
#define CHK_SOURCE 0x04
#define CHK_AGE    0x08
#define CHK_ALL    0x0F

static const char *const STATUS_NAME[] = {"OK", "FORMAT", "CRC", "HMAC", "SOURCE", "AGE"};

static const char *sname(int s)
{
    return (s >= 0 && s <= 5) ? STATUS_NAME[s] : "an undefined status";
}

/* ------------------------------------------------------------------------- independent helpers */

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return c - 'A' + 10;
}

static void unhex(const char *hex, uint8 *out)
{
    size_t i, n = strlen(hex) / 2;
    for (i = 0; i < n; i++) out[i] = (uint8)((hexval(hex[2 * i]) << 4) | hexval(hex[2 * i + 1]));
}

static void tohex(const uint8 *d, size_t n, char *out)
{
    size_t i;
    for (i = 0; i < n; i++) snprintf(out + 2 * i, 3, "%02x", d[i]);
}

static bool digest_is(const uint8 *got, size_t n, const char *hex)
{
    uint8 want[64];
    unhex(hex, want);
    return memcmp(got, want, n) == 0;
}

static uint32 be32(const uint8 *p) { return ((uint32)p[0] << 24) | ((uint32)p[1] << 16) | ((uint32)p[2] << 8) | p[3]; }
static void wbe16(uint8 *p, uint16 v) { p[0] = (uint8)(v >> 8); p[1] = (uint8)v; }
static void wbe32(uint8 *p, uint32 v) { p[0] = (uint8)(v >> 24); p[1] = (uint8)(v >> 16); p[2] = (uint8)(v >> 8); p[3] = (uint8)v; }

static uint16 ref_crc16(const uint8 *d, size_t n)
{
    uint16 crc = 0xFFFF;
    size_t i;
    int b;
    for (i = 0; i < n; i++)
    {
        crc ^= (uint16)(d[i] << 8);
        for (b = 0; b < 8; b++) crc = (crc & 0x8000) ? (uint16)((crc << 1) ^ 0x1021) : (uint16)(crc << 1);
    }
    return crc;
}

static const uint32 K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void ref_block(uint32 h[8], const uint8 *p)
{
    uint32 w[64], a, b, c, d, e, f, g, hh, t1, t2;
    int i;
    for (i = 0; i < 16; i++) w[i] = be32(p + 4 * i);
    for (i = 16; i < 64; i++)
        w[i] = w[i - 16] + (ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3)) + w[i - 7] +
               (ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10));
    a = h[0]; b = h[1]; c = h[2]; d = h[3]; e = h[4]; f = h[5]; g = h[6]; hh = h[7];
    for (i = 0; i < 64; i++)
    {
        t1 = hh + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i];
        t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

/* SHA-256 of two concatenated buffers. */
static void ref_sha2(const uint8 *d1, size_t n1, const uint8 *d2, size_t n2, uint8 out[32])
{
    uint32 h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint8 blk[64];
    size_t total = n1 + n2, i, fill = 0;
    uint64 bits = (uint64)total * 8;
    for (i = 0; i < total; i++)
    {
        blk[fill++] = i < n1 ? d1[i] : d2[i - n1];
        if (fill == 64) { ref_block(h, blk); fill = 0; }
    }
    blk[fill++] = 0x80;
    if (fill > 56) { while (fill < 64) blk[fill++] = 0; ref_block(h, blk); fill = 0; }
    while (fill < 56) blk[fill++] = 0;
    for (i = 0; i < 8; i++) blk[56 + i] = (uint8)(bits >> (56 - 8 * i));
    ref_block(h, blk);
    for (i = 0; i < 8; i++) wbe32(out + 4 * i, h[i]);
}

static void ref_hmac(const uint8 *key, size_t klen, const uint8 *data, size_t n, uint8 mac[32])
{
    uint8 k[64], ipad[64], opad[64], inner[32];
    size_t i;
    memset(k, 0, sizeof(k));
    if (klen > 64) ref_sha2(key, klen, NULL, 0, k);
    else memcpy(k, key, klen);
    for (i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5C; }
    ref_sha2(ipad, 64, data, n, inner);
    ref_sha2(opad, 64, inner, 32, mac);
}

/* ------------------------------------------------------------------------- frames */

#define FR_MAX 1200
#define NOW    1000000u

static uint8 KEY[32];
static uint8 OTHER_KEY[32];
static uint8 fr[FR_MAX];
static uint8 fr2[FR_MAX];

/* Header and embedded command of a frame with CommandLength `len` (the embedded packet is `len` bytes). */
static void fr_compose(uint8 *f, uint16 len, uint16 src, uint32 secs, uint32 seq)
{
    size_t i;
    uint8 x = 0xFF;
    memset(f, 0, FR_MAX);
    wbe16(f, 0x4356);
    f[2] = 1;
    f[3] = 0;
    wbe16(f + 4, src);
    wbe16(f + 6, len);
    wbe32(f + 8, secs);
    wbe32(f + 12, seq);
    /* the embedded CCSDS command: SAMPLE_APP command MID, length field len - 7, function code 0 */
    f[16] = 0x18;
    f[17] = 0x82;
    f[18] = 0xC0;
    f[19] = 0x00;
    wbe16(f + 20, (uint16)(len - 7));
    for (i = 24; i < 16u + len; i++) f[i] = (uint8)(i * 13 + 5);
    f[22] = 0;
    f[23] = 0;
    for (i = 16; i < 16u + len; i++) x ^= f[i];
    f[23] = x;
}

/* MAC over bytes 0..15+L and CRC over bytes 0..47+L, with the given key. */
static void fr_seal_key(uint8 *f, uint16 len, const uint8 *key)
{
    ref_hmac(key, 32, f, 16u + len, f + 16 + len);
    wbe16(f + 48 + len, ref_crc16(f, 48u + len));
}

static void fr_seal(uint8 *f, uint16 len) { fr_seal_key(f, len, KEY); }

static void fr_recrc(uint8 *f, uint16 len) { wbe16(f + 48 + len, ref_crc16(f, 48u + len)); }

/* A valid frame of command length `len`; returns the frame length 50 + len. */
static size_t fr_good(uint8 *f, uint16 len, uint16 src, uint32 secs, uint32 seq)
{
    fr_compose(f, len, src, secs, seq);
    fr_seal(f, len);
    return 50u + len;
}

static cv_config_t cfg_default(uint8 mask)
{
    cv_config_t c;
    memset(&c, 0, sizeof(c));
    memcpy(c.HmacKey, KEY, 32);
    c.AllowedSources[0] = 1;
    c.AllowedSources[1] = 2;
    c.MaxAgeSeconds = 30;
    c.EnableMask = mask;
    return c;
}

static int validate(const uint8 *f, size_t n, uint8 mask)
{
    cv_config_t c = cfg_default(mask);
    return CMDVAL_ValidateFrame(f, n, &c, NOW, NULL);
}

static void expect_status(int got, int want, const char *what)
{
    UtAssert_True(got == want, "%s: CMDVAL_ValidateFrame returned %s (%d), expected %s", what, sname(got), got, sname(want));
}

static void expect_mask(int got, int want, const char *what, uint8 mask)
{
    UtAssert_True(got == want, "%s with EnableMask 0x%02X: CMDVAL_ValidateFrame returned %s (%d), expected %s", what, mask,
                  sname(got), got, sname(want));
}

/* ------------------------------------------------------------------------- set-up */

static void Setup(void)
{
    UT_ResetState(0);
    memcpy(KEY, "cFS-cmdval-test-key-0123456789ab", 32);
    memcpy(OTHER_KEY, "another-key-of-32-bytes-12345678", 32);
}

static void Teardown(void) {}

/* ========================================================================= cases */

static void Test_LibInit(void)
{
    int32 rc = CMDVAL_LibInit();
    UtAssert_True(rc == CFE_SUCCESS, "CMDVAL_LibInit returns CFE_SUCCESS (%ld)", (long)rc);
}

static void Test_Crc16(void)
{
    uint8 all[256];
    uint16 v;
    int i;
    for (i = 0; i < 256; i++) all[i] = (uint8)i;
    v = CMDVAL_Crc16((const uint8 *)"123456789", 9);
    UtAssert_True(v == 0x29B1, "CRC-16/CCITT-FALSE of \"123456789\" is 0x%04X, check value 0x29B1", v);
    v = CMDVAL_Crc16((const uint8 *)"", 0);
    UtAssert_True(v == 0xFFFF, "CRC of zero bytes is the initial value 0xFFFF, got 0x%04X", v);
    v = CMDVAL_Crc16((const uint8 *)"A", 1);
    UtAssert_True(v == 0xB915, "CRC of \"A\" is 0x%04X, expected 0xB915", v);
    v = CMDVAL_Crc16(all, 256);
    UtAssert_True(v == 0x3FBD, "CRC of the bytes 0..255 is 0x%04X, expected 0x3FBD", v);
}

static const struct { const char *msg; const char *digest; } SHA_VEC[] = {
    {"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    {"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
    {"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
     "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"},
    {"abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu",
     "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1"},
};

/* Digests of n bytes (i * 7 + 3) & 0xFF, at the padding boundaries. */
static const struct { size_t n; const char *digest; } SHA_LEN[] = {
    {55, "e7313d333c272e639f790978283f9eb392e843d0f29b7016828bb1daa4aac70b"},
    {56, "4324d65f3c103567f5589c710bc08f8523f929a9272e3af36fc968e52abc6c27"},
    {63, "81c80242132f230c3bd41b3e63bbcff16107339549214a99614ff26664625055"},
    {64, "39e3d7b6b5d075d37d053ad89b24b41bef4f3c29760c84447cab3f3be1882241"},
    {65, "aacca6ff74fdbb296d165a45cecfa04e5127bc008770fbbdd48006f2d2fae95e"},
    {119, "9ce7368e4daf32341631b492e80359dc9f594b48453cd0dd5bf0b19279cc177e"},
    {120, "7836b787757e95e58b3ca5aec90b1b004e8deba1e50e9675af9cabf1a13a04b5"},
};

static uint8 million[1000000];

static void Test_Sha256(void)
{
    uint8 d[32], buf[128];
    char hex[65];
    size_t i, k;
    for (i = 0; i < sizeof(SHA_VEC) / sizeof(SHA_VEC[0]); i++)
    {
        memset(d, 0, sizeof(d));
        CMDVAL_Sha256((const uint8 *)SHA_VEC[i].msg, strlen(SHA_VEC[i].msg), d);
        tohex(d, 32, hex);
        UtAssert_True(digest_is(d, 32, SHA_VEC[i].digest), "SHA-256 of the %u-byte FIPS 180-4 message is %s, expected %s",
                      (unsigned)strlen(SHA_VEC[i].msg), hex, SHA_VEC[i].digest);
    }
    for (i = 0; i < sizeof(SHA_LEN) / sizeof(SHA_LEN[0]); i++)
    {
        for (k = 0; k < SHA_LEN[i].n; k++) buf[k] = (uint8)((k * 7 + 3) & 0xFF);
        memset(d, 0, sizeof(d));
        CMDVAL_Sha256(buf, SHA_LEN[i].n, d);
        tohex(d, 32, hex);
        UtAssert_True(digest_is(d, 32, SHA_LEN[i].digest), "SHA-256 of a %u-byte message (padding boundary) is %s, expected %s",
                      (unsigned)SHA_LEN[i].n, hex, SHA_LEN[i].digest);
    }
    memset(million, 'a', sizeof(million));
    memset(d, 0, sizeof(d));
    CMDVAL_Sha256(million, sizeof(million), d);
    tohex(d, 32, hex);
    UtAssert_True(digest_is(d, 32, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"),
                  "SHA-256 of one million 'a' (FIPS 180-4) is %s", hex);
}

static void hmac_case(int n, const uint8 *key, size_t klen, const uint8 *data, size_t dlen, const char *want, size_t cmp)
{
    uint8 mac[32];
    char hex[65];
    memset(mac, 0, sizeof(mac));
    CMDVAL_HmacSha256(key, klen, data, dlen, mac);
    tohex(mac, 32, hex);
    UtAssert_True(digest_is(mac, cmp, want), "HMAC-SHA-256 RFC 4231 test case %d (key %u bytes, data %u bytes) is %s, expected %s%s",
                  n, (unsigned)klen, (unsigned)dlen, hex, want, cmp < 32 ? " (first 16 bytes)" : "");
}

static void Test_HmacRfc4231(void)
{
    uint8 key[131], data[50];
    size_t i;
    memset(key, 0x0b, 20);
    hmac_case(1, key, 20, (const uint8 *)"Hi There", 8, "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7", 32);
    hmac_case(2, (const uint8 *)"Jefe", 4, (const uint8 *)"what do ya want for nothing?", 28,
              "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", 32);
    memset(key, 0xaa, 20);
    memset(data, 0xdd, 50);
    hmac_case(3, key, 20, data, 50, "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe", 32);
    for (i = 0; i < 25; i++) key[i] = (uint8)(i + 1);
    memset(data, 0xcd, 50);
    hmac_case(4, key, 25, data, 50, "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b", 32);
    memset(key, 0x0c, 20);
    hmac_case(5, key, 20, (const uint8 *)"Test With Truncation", 20, "a3b6167473100ee06e0c796c2955552b", 16);
}

static void Test_HmacLongKeys(void)
{
    static const char *d7 = "This is a test using a larger than block-size key and a larger than block-size data. "
                            "The key needs to be hashed before being used by the HMAC algorithm.";
    uint8 key[131], mac[32];
    char hex[65];
    size_t i;
    memset(key, 0xaa, sizeof(key));
    hmac_case(6, key, 131, (const uint8 *)"Test Using Larger Than Block-Size Key - Hash Key First", 54,
              "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54", 32);
    hmac_case(7, key, 131, (const uint8 *)d7, strlen(d7), "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2", 32);
    /* keys of exactly one block (64 bytes, used as is) and one byte more (hashed first) */
    for (i = 0; i < 65; i++) key[i] = (uint8)i;
    CMDVAL_HmacSha256(key, 64, (const uint8 *)"key boundary", 12, mac);
    tohex(mac, 32, hex);
    UtAssert_True(digest_is(mac, 32, "dc02ca5976aead998b94cba2568c69d718256d4ca34ef29918019a4cf6a0802d"),
                  "HMAC-SHA-256 with a 64-byte key (not hashed) is %s", hex);
    CMDVAL_HmacSha256(key, 65, (const uint8 *)"key boundary", 12, mac);
    tohex(mac, 32, hex);
    UtAssert_True(digest_is(mac, 32, "5195e5e4c48c579ac405d5bc483eb6768217643d6e73e76a452defba2cf2eae7"),
                  "HMAC-SHA-256 with a 65-byte key (hashed first) is %s", hex);
    CMDVAL_HmacSha256(KEY, 32, (const uint8 *)"", 0, mac);
    tohex(mac, 32, hex);
    UtAssert_True(digest_is(mac, 32, "b0f0f0d472947db7973c356bb7e20711dcaaa2bc94a9ddc678779b658bd11324"),
                  "HMAC-SHA-256 with the default 32-byte table key over zero bytes is %s", hex);
}

static void age_is(uint32 frame, uint32 now, uint32 max, int want)
{
    int got = CMDVAL_CheckAge(frame, now, max);
    UtAssert_True(got == want, "CMDVAL_CheckAge(FrameSeconds %lu, NowSeconds %lu, MaxAgeSeconds %lu) returned %s, expected %s",
                  (unsigned long)frame, (unsigned long)now, (unsigned long)max, sname(got), sname(want));
}

static void Test_CheckAge(void)
{
    age_is(1000, 1030, 30, ST_OK);       /* exactly MaxAge in the past */
    age_is(1000, 1031, 30, ST_AGE);      /* one second more */
    age_is(1030, 1000, 30, ST_OK);       /* exactly MaxAge in the future */
    age_is(1031, 1000, 30, ST_AGE);
    age_is(1000, 1000, 30, ST_OK);
    age_is(1000, 1000, 0, ST_OK);        /* MaxAge 0: only the same second */
    age_is(1000, 1001, 0, ST_AGE);
    age_is(1001, 1000, 0, ST_AGE);
    age_is(0, 30, 30, ST_OK);
    age_is(30, 0, 30, ST_OK);
    age_is(31, 0, 30, ST_AGE);
    age_is(0, 31, 30, ST_AGE);
    age_is(0xFFFFFFFFu, 0, 30, ST_AGE);  /* no wrap-around: the difference is 4294967295 */
    age_is(0, 0xFFFFFFFFu, 30, ST_AGE);
    age_is(0xFFFFFFE1u, 0xFFFFFFFFu, 30, ST_OK);
    age_is(0xFFFFFFE0u, 0xFFFFFFFFu, 30, ST_AGE);
    age_is(0xFFFFFFFFu, 0xFFFFFFE1u, 30, ST_OK);
    age_is(0xFFFFFFFFu, 0xFFFFFFE0u, 30, ST_AGE);
    age_is(0, 0xFFFFFFFFu, 0xFFFFFFFFu, ST_OK);
    age_is(0xFFFFFFFFu, 0, 0xFFFFFFFFu, ST_OK);
    age_is(0x80000000u, 0x7FFFFFFFu, 1, ST_OK);
    age_is(0x7FFFFFFFu, 0x80000001u, 1, ST_AGE);
}

static void source_is(uint16 src, const uint16 list[8], int want, const char *what)
{
    cv_config_t c = cfg_default(CHK_ALL);
    int got;
    memcpy(c.AllowedSources, list, sizeof(c.AllowedSources));
    got = CMDVAL_CheckSource(src, &c);
    UtAssert_True(got == want, "CMDVAL_CheckSource(%u) with %s returned %s, expected %s", src, what, sname(got), sname(want));
}

static void Test_CheckSource(void)
{
    static const uint16 deflt[8] = {1, 2, 0, 0, 0, 0, 0, 0};
    static const uint16 zero[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    static const uint16 last[8] = {0, 0, 0, 0, 0, 0, 0, 9};
    static const uint16 full[8] = {11, 12, 13, 0xFFFF, 15, 16, 17, 18};
    source_is(1, deflt, ST_OK, "the default list {1, 2}");
    source_is(2, deflt, ST_OK, "the default list {1, 2}");
    source_is(3, deflt, ST_SOURCE, "the default list {1, 2}");
    source_is(0, deflt, ST_SOURCE, "the default list {1, 2} (0 is never a source, even with unused 0 slots)");
    source_is(0, zero, ST_SOURCE, "an all-zero list");
    source_is(1, zero, ST_SOURCE, "an all-zero list");
    source_is(9, last, ST_OK, "9 only in the last slot");
    source_is(0, last, ST_SOURCE, "9 only in the last slot");
    source_is(1, last, ST_SOURCE, "9 only in the last slot");
    source_is(0xFFFF, full, ST_OK, "0xFFFF in the fourth slot");
    source_is(18, full, ST_OK, "eight used slots");
    source_is(19, full, ST_SOURCE, "eight used slots");
}

static void Test_ValidFrame(void)
{
    cv_config_t c = cfg_default(CHK_ALL);
    cv_info_t info;
    size_t n;
    int got;

    n = fr_good(fr, 8, 1, NOW, 0xDEADBEEFu);
    memset(&info, 0xA5, sizeof(info));
    got = CMDVAL_ValidateFrame(fr, n, &c, NOW, &info);
    expect_status(got, ST_OK, "a valid 58-byte frame (L 8) from source 1");
    if (got == ST_OK)
    {
        UtAssert_True(info.SourceId == 1, "Info.SourceId %u, expected 1", info.SourceId);
        UtAssert_True(info.TimeSeconds == NOW, "Info.TimeSeconds %lu, expected %lu", (unsigned long)info.TimeSeconds,
                      (unsigned long)NOW);
        UtAssert_True(info.Sequence == 0xDEADBEEFu, "Info.Sequence 0x%08lX, expected 0xDEADBEEF", (unsigned long)info.Sequence);
        UtAssert_True(info.Command == fr + 16, "Info.Command points into the frame at offset 16");
        UtAssert_True(info.CommandLen == 8, "Info.CommandLen %lu, expected 8", (unsigned long)info.CommandLen);
    }

    n = fr_good(fr, 100, 2, NOW - 30, 7);
    memset(&info, 0, sizeof(info));
    got = CMDVAL_ValidateFrame(fr, n, &c, NOW, &info);
    expect_status(got, ST_OK, "a valid frame of L 100 from source 2, 30 s old");
    if (got == ST_OK)
    {
        UtAssert_True(info.SourceId == 2 && info.TimeSeconds == NOW - 30 && info.Sequence == 7,
                      "Info reports source %u, seconds %lu, sequence %lu", info.SourceId, (unsigned long)info.TimeSeconds,
                      (unsigned long)info.Sequence);
        UtAssert_True(info.Command == fr + 16 && info.CommandLen == 100,
                      "Info.Command and Info.CommandLen (%lu) describe the embedded packet", (unsigned long)info.CommandLen);
    }

    n = fr_good(fr, 8, 1, NOW + 30, 1);
    expect_status(CMDVAL_ValidateFrame(fr, n, &c, NOW, NULL), ST_OK, "a valid frame 30 s in the future with Info NULL");
}

static void Test_FrameSizeLimits(void)
{
    cv_config_t c = cfg_default(CHK_ALL);
    cv_info_t info;
    size_t n;
    int got;
    n = fr_good(fr, 1024, 1, NOW, 3);
    memset(&info, 0, sizeof(info));
    got = CMDVAL_ValidateFrame(fr, n, &c, NOW, &info);
    expect_status(got, ST_OK, "a 1074-byte frame with the largest command (L 1024)");
    if (got == ST_OK)
        UtAssert_True(info.CommandLen == 1024 && info.Command == fr + 16, "Info.CommandLen %lu for L 1024",
                      (unsigned long)info.CommandLen);
    n = fr_good(fr, 8, 1, NOW, 3);
    expect_status(validate(fr, n, CHK_ALL), ST_OK, "the smallest frame (58 bytes, L 8)");
}

static void Test_FormatRejections(void)
{
    static const uint8 masks[2] = {CHK_ALL, 0};
    size_t n;
    int m;
    for (m = 0; m < 2; m++)
    {
        uint8 mask = masks[m];
        /* FrameLen below 58 */
        n = fr_good(fr, 8, 1, NOW, 1);
        expect_mask(validate(fr, 57, mask), ST_FORMAT, "a frame truncated to 57 bytes", mask);
        expect_mask(validate(fr, 16, mask), ST_FORMAT, "a 16-byte frame", mask);
        expect_mask(validate(fr, 0, mask), ST_FORMAT, "a 0-byte frame", mask);
        /* magic, version, flags (MAC and CRC recomputed so only the header field is wrong) */
        fr_compose(fr, 8, 1, NOW, 1); wbe16(fr, 0x4357); fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Magic 0x4357", mask);
        fr_compose(fr, 8, 1, NOW, 1); wbe16(fr, 0x5643); fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Magic with its bytes swapped (0x5643)", mask);
        fr_compose(fr, 8, 1, NOW, 1); fr[2] = 2; fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Version 2", mask);
        fr_compose(fr, 8, 1, NOW, 1); fr[2] = 0; fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Version 0", mask);
        fr_compose(fr, 8, 1, NOW, 1); fr[3] = 1; fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Flags 1", mask);
        fr_compose(fr, 8, 1, NOW, 1); fr[3] = 0x80; fr_seal(fr, 8);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "Flags 0x80", mask);
        /* CommandLength below 8 (the embedded packet keeps 8 bytes) */
        fr_compose(fr, 8, 1, NOW, 1); wbe16(fr + 6, 7); fr_seal(fr, 8);
        expect_mask(validate(fr, 58, mask), ST_FORMAT, "CommandLength 7 in a 58-byte frame", mask);
        fr_compose(fr, 8, 1, NOW, 1); wbe16(fr + 6, 0); fr_seal(fr, 8);
        expect_mask(validate(fr, 58, mask), ST_FORMAT, "CommandLength 0 in a 58-byte frame", mask);
        /* CommandLength above 1024, everything else consistent */
        n = fr_good(fr, 1025, 1, NOW, 1);
        expect_mask(validate(fr, n, mask), ST_FORMAT, "CommandLength 1025 in a 1075-byte frame", mask);
        /* FrameLen other than 50 + L */
        n = fr_good(fr, 8, 1, NOW, 1);
        expect_mask(validate(fr, n + 1, mask), ST_FORMAT, "a 59-byte datagram whose CommandLength is 8", mask);
        n = fr_good(fr, 20, 1, NOW, 1);
        expect_mask(validate(fr, n - 4, mask), ST_FORMAT, "a 66-byte datagram whose CommandLength is 20", mask);
        /* embedded CCSDS length field inconsistent with L */
        fr_compose(fr, 10, 1, NOW, 1); wbe16(fr + 20, 8 - 7); fr_seal(fr, 10);
        expect_mask(validate(fr, 60, mask), ST_FORMAT, "CommandLength 10 with an embedded CCSDS length of 8 bytes", mask);
        fr_compose(fr, 10, 1, NOW, 1); wbe16(fr + 20, 12 - 7); fr_seal(fr, 10);
        expect_mask(validate(fr, 60, mask), ST_FORMAT, "CommandLength 10 with an embedded CCSDS length of 12 bytes", mask);
        /* embedded packet with the type bit clear (telemetry) */
        fr_compose(fr, 8, 1, NOW, 1); fr[16] = 0x08; fr_seal(fr, 8);
        expect_mask(validate(fr, 58, mask), ST_FORMAT, "an embedded telemetry packet (type bit clear)", mask);
    }
}

static void Test_SingleReasons(void)
{
    cv_config_t c;
    size_t n;

    n = fr_good(fr, 8, 1, NOW, 1);
    fr[n - 1] ^= 0x01;
    expect_status(validate(fr, n, CHK_ALL), ST_CRC, "a frame with one CRC bit flipped");

    n = fr_good(fr, 8, 1, NOW, 1);
    fr[16 + 8 + 5] ^= 0x01;
    fr_recrc(fr, 8);
    expect_status(validate(fr, n, CHK_ALL), ST_HMAC, "a frame with one MAC bit flipped and the CRC recomputed");

    fr_compose(fr, 8, 1, NOW, 1);
    fr_seal_key(fr, 8, OTHER_KEY);
    expect_status(validate(fr, 58, CHK_ALL), ST_HMAC, "a frame authenticated with another key");

    fr_compose(fr, 12, 1, NOW, 1);
    fr_seal(fr, 12);
    fr[26] ^= 0x40; /* embedded command changed after the MAC was computed */
    fr_recrc(fr, 12);
    expect_status(validate(fr, 62, CHK_ALL), ST_HMAC, "a frame whose embedded command was altered after authentication");

    n = fr_good(fr, 8, 7, NOW, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_SOURCE, "a valid frame from unlisted source 7");
    n = fr_good(fr, 8, 0, NOW, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_SOURCE, "a valid frame from source 0");

    n = fr_good(fr, 8, 1, NOW - 31, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_AGE, "a frame 31 s old with MaxAgeSeconds 30");
    n = fr_good(fr, 8, 1, NOW + 31, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_AGE, "a frame 31 s in the future with MaxAgeSeconds 30");
    n = fr_good(fr, 8, 1, NOW - 30, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_OK, "a frame exactly 30 s old");
    n = fr_good(fr, 8, 1, NOW + 30, 1);
    expect_status(validate(fr, n, CHK_ALL), ST_OK, "a frame exactly 30 s in the future");

    /* the configuration given is the one used: key, sources and window */
    n = fr_good(fr, 8, 5, NOW - 100, 1);
    c = cfg_default(CHK_ALL);
    c.AllowedSources[4] = 5;
    c.MaxAgeSeconds = 100;
    expect_status(CMDVAL_ValidateFrame(fr, n, &c, NOW, NULL), ST_OK,
                  "source 5 listed in slot 5 and a 100 s window for a frame 100 s old");
    fr_compose(fr, 8, 1, NOW, 1);
    fr_seal_key(fr, 8, OTHER_KEY);
    c = cfg_default(CHK_ALL);
    memcpy(c.HmacKey, OTHER_KEY, 32);
    expect_status(CMDVAL_ValidateFrame(fr, 58, &c, NOW, NULL), ST_OK, "a frame authenticated with the key of the configuration");
}

/* All 16 combinations of defects (CRC, MAC, source, age) against all 16 enable masks. */
static void Test_OrderMatrix(void)
{
    int defects, mask, bad = 0, total = 0;
    size_t n;
    char first[3][160];
    memset(first, 0, sizeof(first));
    for (defects = 0; defects < 16; defects++)
    {
        for (mask = 0; mask < 16; mask++)
        {
            int want = ST_OK, got;
            fr_compose(fr2, 16, (defects & CHK_SOURCE) ? 7 : 1, (defects & CHK_AGE) ? NOW - 100 : NOW, (uint32)defects);
            fr_seal(fr2, 16);
            if (defects & CHK_HMAC)
            {
                fr2[16 + 16 + 9] ^= 0x20;
                fr_recrc(fr2, 16);
            }
            if (defects & CHK_CRC) fr2[16 + 16 + 32 + 1] ^= 0x01;
            if ((defects & CHK_CRC) && (mask & CHK_CRC)) want = ST_CRC;
            else if ((defects & CHK_HMAC) && (mask & CHK_HMAC)) want = ST_HMAC;
            else if ((defects & CHK_SOURCE) && (mask & CHK_SOURCE)) want = ST_SOURCE;
            else if ((defects & CHK_AGE) && (mask & CHK_AGE)) want = ST_AGE;
            got = validate(fr2, 66, (uint8)mask);
            total++;
            if (got != want)
            {
                if (bad < 3)
                    snprintf(first[bad], sizeof(first[bad]), "defects%s%s%s%s mask 0x%02X: %s instead of %s",
                             (defects & CHK_CRC) ? " CRC" : "", (defects & CHK_HMAC) ? " MAC" : "",
                             (defects & CHK_SOURCE) ? " source" : "", (defects & CHK_AGE) ? " age" : "", mask, sname(got),
                             sname(want));
                bad++;
            }
        }
    }
    UtAssert_True(bad == 0,
                  "CMDVAL_ValidateFrame returns the first failure in the order CRC, HMAC, SOURCE, AGE among the enabled "
                  "checks in %d of %d combinations; %d wrong, e.g. %s | %s | %s",
                  total - bad, total, bad, first[0], first[1], first[2]);

    /* the two cases the specification names */
    n = fr_good(fr, 8, 1, NOW, 1);
    fr[16 + 8 + 2] ^= 0x01; /* MAC wrong, and the CRC no longer matches either */
    expect_status(validate(fr, n, CHK_ALL), ST_CRC, "a frame with a bad CRC and a bad MAC");
    expect_status(validate(fr, n, CHK_HMAC | CHK_SOURCE | CHK_AGE), ST_HMAC,
                  "a frame with a bad CRC and a bad MAC while the CRC check is disabled");
    expect_status(validate(fr, n, 0), ST_OK, "a frame with a bad CRC and a bad MAC while every optional check is disabled");
    n = fr_good(fr, 8, 1, NOW, 1);
    fr[n - 2] ^= 0xFF;
    expect_status(validate(fr, n, CHK_HMAC | CHK_SOURCE | CHK_AGE), ST_OK,
                  "a frame with only a bad CRC while the CRC check is disabled");
    /* FORMAT comes before everything else */
    fr_compose(fr, 8, 7, NOW - 100, 1);
    fr[3] = 1;
    fr_seal(fr, 8);
    fr[57] ^= 0x01;
    expect_status(validate(fr, 58, CHK_ALL), ST_FORMAT,
                  "a frame with bad Flags, a bad CRC, an unlisted source and an old timestamp");
}

void UtTest_Setup(void)
{
    UtTest_Add(Test_LibInit, Setup, Teardown, "Library initialization");
    UtTest_Add(Test_Crc16, Setup, Teardown, "CRC-16/CCITT-FALSE");
    UtTest_Add(Test_Sha256, Setup, Teardown, "SHA-256 vectors");
    UtTest_Add(Test_HmacRfc4231, Setup, Teardown, "HMAC-SHA-256 RFC 4231 cases 1 to 5");
    UtTest_Add(Test_HmacLongKeys, Setup, Teardown, "HMAC-SHA-256 keys longer than 64 bytes");
    UtTest_Add(Test_CheckAge, Setup, Teardown, "Age window");
    UtTest_Add(Test_CheckSource, Setup, Teardown, "Source allow-list");
    UtTest_Add(Test_ValidFrame, Setup, Teardown, "Valid frame and frame information");
    UtTest_Add(Test_FrameSizeLimits, Setup, Teardown, "Frame size limits");
    UtTest_Add(Test_FormatRejections, Setup, Teardown, "FORMAT rejections");
    UtTest_Add(Test_SingleReasons, Setup, Teardown, "CRC, HMAC, SOURCE and AGE rejections");
    UtTest_Add(Test_OrderMatrix, Setup, Teardown, "Check order and enable mask");
}
