/* Reference implementation of the pinned CMDVAL API (scratch only, not part of the pack). */
#include "cfe.h"
#include "cmdval_lib.h"

static const uint32 K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
#define R(x, n) (((x) >> (n)) | ((x) << (32 - (n))))
typedef struct { uint32 h[8]; uint8 buf[64]; size_t fill; uint64 total; } Ctx;
static uint32 be32(const uint8 *p) { return ((uint32)p[0] << 24) | ((uint32)p[1] << 16) | ((uint32)p[2] << 8) | p[3]; }
static void blk(uint32 *h, const uint8 *p)
{
    uint32 w[64], a, b, c, d, e, f, g, hh, t1, t2; int i;
    for (i = 0; i < 16; i++) w[i] = be32(p + 4 * i);
    for (i = 16; i < 64; i++) w[i] = w[i-16] + (R(w[i-15],7) ^ R(w[i-15],18) ^ (w[i-15] >> 3)) + w[i-7] + (R(w[i-2],17) ^ R(w[i-2],19) ^ (w[i-2] >> 10));
    a=h[0];b=h[1];c=h[2];d=h[3];e=h[4];f=h[5];g=h[6];hh=h[7];
    for (i = 0; i < 64; i++) { t1 = hh + (R(e,6)^R(e,11)^R(e,25)) + ((e&f)^(~e&g)) + K[i] + w[i]; t2 = (R(a,2)^R(a,13)^R(a,22)) + ((a&b)^(a&c)^(b&c)); hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2; }
    h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;
}
static void s_init(Ctx *c) { static const uint32 iv[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}; memcpy(c->h, iv, sizeof(iv)); c->fill = 0; c->total = 0; }
static void s_upd(Ctx *c, const uint8 *d, size_t n) { size_t i; for (i = 0; i < n; i++) { c->buf[c->fill++] = d[i]; if (c->fill == 64) { blk(c->h, c->buf); c->fill = 0; } } c->total += n; }
static void s_fin(Ctx *c, uint8 out[32])
{
    uint64 bits = c->total * 8; int i; uint8 pad = 0x80, z = 0;
    s_upd(c, &pad, 1); while (c->fill != 56) s_upd(c, &z, 1);
    for (i = 0; i < 8; i++) c->buf[56 + i] = (uint8)(bits >> (56 - 8 * i));
    blk(c->h, c->buf);
    for (i = 0; i < 8; i++) { out[4*i] = (uint8)(c->h[i] >> 24); out[4*i+1] = (uint8)(c->h[i] >> 16); out[4*i+2] = (uint8)(c->h[i] >> 8); out[4*i+3] = (uint8)c->h[i]; }
}
int32 CMDVAL_LibInit(void) { return CFE_SUCCESS; }
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len)
{ uint16 c = 0xFFFF; size_t i; int b; for (i = 0; i < Len; i++) { c ^= (uint16)(Data[i] << 8); for (b = 0; b < 8; b++) c = (c & 0x8000) ? (uint16)((c << 1) ^ 0x1021) : (uint16)(c << 1); } return c; }
void CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]) { Ctx c; s_init(&c); s_upd(&c, Data, Len); s_fin(&c, Digest); }
void CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32])
{
    uint8 k[64], p[64], inner[32]; size_t i; Ctx c;
    memset(k, 0, 64); if (KeyLen > 64) CMDVAL_Sha256(Key, KeyLen, k); else memcpy(k, Key, KeyLen);
    for (i = 0; i < 64; i++) { p[i] = k[i] ^ 0x36; }
    s_init(&c); s_upd(&c, p, 64); s_upd(&c, Data, Len); s_fin(&c, inner);
    for (i = 0; i < 64; i++) { p[i] = k[i] ^ 0x5C; }
    s_init(&c); s_upd(&c, p, 64); s_upd(&c, inner, 32); s_fin(&c, Mac);
}
CMDVAL_Status_t CMDVAL_CheckSource(uint16 SourceId, const CMDVAL_Config_t *Config)
{ int i; if (SourceId == 0) return CMDVAL_ERR_SOURCE; for (i = 0; i < 8; i++) if (Config->AllowedSources[i] != 0 && Config->AllowedSources[i] == SourceId) return CMDVAL_OK; return CMDVAL_ERR_SOURCE; }
CMDVAL_Status_t CMDVAL_CheckAge(uint32 F, uint32 N, uint32 M) { int64 d = (int64)N - (int64)F; if (d < 0) d = -d; return d <= (int64)M ? CMDVAL_OK : CMDVAL_ERR_AGE; }
CMDVAL_Status_t CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const CMDVAL_Config_t *Config, uint32 NowSeconds, CMDVAL_FrameInfo_t *Info)
{
    uint16 L; uint8 mac[32], diff = 0; size_t i;
    if (FrameLen < 58) return CMDVAL_ERR_FORMAT;
    L = (uint16)((Frame[6] << 8) | Frame[7]);
    if (((Frame[0] << 8) | Frame[1]) != 0x4356 || Frame[2] != 1 || Frame[3] != 0 || L < 8 || L > 1024 || FrameLen != 50u + L) return CMDVAL_ERR_FORMAT;
    if ((size_t)((Frame[20] << 8) | Frame[21]) + 7 != L || (Frame[16] & 0x10) == 0) return CMDVAL_ERR_FORMAT;
    if ((Config->EnableMask & 1) && CMDVAL_Crc16(Frame, 48u + L) != (uint16)((Frame[48 + L] << 8) | Frame[49 + L])) return CMDVAL_ERR_CRC;
    if (Config->EnableMask & 2) { CMDVAL_HmacSha256(Config->HmacKey, 32, Frame, 16u + L, mac); for (i = 0; i < 32; i++) diff |= (uint8)(mac[i] ^ Frame[16 + L + i]); if (diff) return CMDVAL_ERR_HMAC; }
    if ((Config->EnableMask & 4) && CMDVAL_CheckSource((uint16)((Frame[4] << 8) | Frame[5]), Config) != CMDVAL_OK) return CMDVAL_ERR_SOURCE;
    if ((Config->EnableMask & 8) && CMDVAL_CheckAge(be32(Frame + 8), NowSeconds, Config->MaxAgeSeconds) != CMDVAL_OK) return CMDVAL_ERR_AGE;
    if (Info != NULL) { Info->SourceId = (uint16)((Frame[4] << 8) | Frame[5]); Info->TimeSeconds = be32(Frame + 8); Info->Sequence = be32(Frame + 12); Info->Command = Frame + 16; Info->CommandLen = L; }
    return CMDVAL_OK;
}
