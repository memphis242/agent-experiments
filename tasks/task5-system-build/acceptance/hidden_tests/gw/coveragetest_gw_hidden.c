/*
 * Black-box behaviour test of the GW gateway application (spec S5).
 *
 * GW_AppMain runs on its own thread against the behaviour model of t5_model.h. The test talks to it
 * only as the specification allows: commands, SEND_HK and WAKEUP on GW's message IDs, subscribed
 * packets on the bus, the shared table "CMDI.ValTable" offered by the model, the "ConnTable" image
 * loaded at start-up, datagrams on bound UDP ports, bytes on TCP connections to test listeners and
 * on PTY-like devices. cmdval_lib is replaced by the model's reference implementation, so every frame
 * GW builds or receives is checked with the table key independently of the agent's library.
 * Structures are decoded by offset from the specification.
 */
#define T5_MODEL_CMDVAL
#include "t5_model.h"

extern void GW_AppMain(void);

/* ------------------------------------------------------------------ identifiers pinned by the spec */

#define GW_CMD_MID      0x18D8u
#define GW_SEND_HK_MID  0x18D9u
#define GW_WAKEUP_MID   0x18DAu
#define GW_HK_MID       0x08D8u
#define GW_CONN_MID     0x08D9u
#define SFI_HB_MID      0x18DFu
#define SAMPLE_CMD_MID  0x1882u
#define V_E4            0x18E4u
#define V_E5            0x18E5u
#define V_E6            0x18E6u
#define V_E7            0x18E7u

#define CC_NOOP    0
#define CC_RESET   1
#define CC_ADD     2
#define CC_REMOVE  3
#define CC_ENABLE  4
#define CC_DISABLE 5
#define CC_RELOAD  6
#define CC_STATUS  7

#define EID_INIT        1
#define EID_CC_ERR      2
#define EID_NOOP        3
#define EID_RESET       4
#define EID_MID_ERR     5
#define EID_LEN_ERR     6
#define EID_TBL_VAL     10
#define EID_ADD_INF     11
#define EID_ADD_ERR     12
#define EID_REMOVE_INF  13
#define EID_REMOVE_ERR  14
#define EID_ENABLE_INF  15
#define EID_ENABLE_ERR  16
#define EID_DISABLE_INF 17
#define EID_DISABLE_ERR 18
#define EID_RELOAD_INF  19
#define EID_UP_INF      20
#define EID_DOWN_ERR    21
#define EID_EGRESS_ERR  22
#define EID_INGRESS_ERR 23
#define EID_STATUS_INF  24
#define EID_REJECT      25
#define EID_VALTBL_ERR  26
#define EID_VALTBL_INF  27
#define EID_TIMEOUT_ERR 28

#define IF_UDP        1
#define IF_TCP        2
#define IF_SERIAL     3
#define ST_DISABLED   0
#define ST_UP         1
#define ST_CONNECTING 2
#define ST_DOWN       3

#define CONN_SIZE     152
#define TBL_SIZE      (8 * CONN_SIZE)
#define VAL_SIZE      56
#define HK_SIZE       52
#define CONN_TLM_SIZE 160
#define TLM_HDR       16
#define CMD_HDR       8

static const uint8 KEY0[32] = "cFS-cmdval-test-key-0123456789ab";
static const uint8 KEY2[32] = "second-test-key-for-gw-abcdefghi";
static const uint8 MARKER[4] = {0x1A, 0xCF, 0xFC, 0x1D};

/* ------------------------------------------------------------------ images */

typedef struct { uint8 b[CONN_SIZE]; } conn_t;

static uint8 conntbl[TBL_SIZE];
static uint8 valtbl[VAL_SIZE];

static void valtbl_make(uint8 *t, const uint8 *key, uint32 max_age, uint8 mask)
{
    memset(t, 0, VAL_SIZE);
    memcpy(t, key, 32);
    t5_put16(t + 32, 1);
    t5_put16(t + 34, 2);
    t5_put32(t + 48, max_age);
    t[52] = mask;
}

static void conn_set(conn_t *c, uint8 iface, uint8 enabled, uint16 lport, const char *addr, uint16 rport, const char *dev)
{
    size_t n;
    memset(c, 0, sizeof(*c));
    c->b[0] = iface;
    c->b[1] = enabled;
    t5_put16(c->b + 2, lport);
    if (addr != NULL)
    {
        n = strlen(addr);
        memcpy(c->b + 4, addr, n > 16 ? 16 : n);
    }
    t5_put16(c->b + 20, rport);
    if (dev != NULL)
    {
        n = strlen(dev);
        memcpy(c->b + 24, dev, n > 64 ? 64 : n);
    }
}

static void conn_sub(conn_t *c, int i, uint32 mid) { t5_put32(c->b + 88 + 4 * i, mid); }

static void udp_conn(conn_t *c, uint8 en, uint16 lport, uint16 rport, uint32 sub)
{
    conn_set(c, IF_UDP, en, lport, "127.0.0.1", rport, NULL);
    if (sub != 0) conn_sub(c, 0, sub);
}

static void tbl_put(uint8 *t, int slot, const conn_t *c) { memcpy(t + slot * CONN_SIZE, c->b, CONN_SIZE); }

/* ------------------------------------------------------------------ commands, time */

static bool add_conn(uint8 idx, const conn_t *c)
{
    uint8 p[4 + CONN_SIZE];
    memset(p, 0, sizeof(p));
    p[0] = idx;
    memcpy(p + 4, c->b, CONN_SIZE);
    return t5_send_cmd(GW_CMD_MID, CC_ADD, p, sizeof(p));
}

static bool idx_cmd(int cc, uint8 idx)
{
    uint8 p[4] = {0, 0, 0, 0};
    p[0] = idx;
    return t5_send_cmd(GW_CMD_MID, cc, p, sizeof(p));
}

static bool simple_cmd(int cc) { return t5_send_cmd(GW_CMD_MID, cc, NULL, 0); }

static void tick_time(void)
{
    uint64 s = (uint64)t5_now_sub + 0x1999999Au; /* 0.1 s */
    t5_now_sec += (uint32)(s >> 32);
    t5_now_sub = (uint32)s;
}

static bool wakeup(void)
{
    tick_time();
    return t5_send_cmd(GW_WAKEUP_MID, 0, NULL, 0);
}

static bool wakeups(int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (!wakeup()) return false;
    return true;
}

/* ------------------------------------------------------------------ telemetry readers */

typedef struct
{
    uint8  cc, cec, cfg, up;
    uint32 egr, ingr, egerr, ingerr, loop;
    uint16 subs;
    uint8  shared;
    uint32 frames, fmt, crc, hmac, src, age;
} hk_t;

static bool get_hk(hk_t *h)
{
    int from = t5_ntx;
    const t5_tx_t *t;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!t5_send_cmd(GW_SEND_HK_MID, 0, NULL, 0)) return false;
    t = t5_last_tx(GW_HK_MID, from);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_HK did not transmit GW housekeeping on GW_HK_TLM_MID (0x08D8)");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + HK_SIZE, "GW_HkTlm_t is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + HK_SIZE));
    p = t->bytes + TLM_HDR;
    h->cc = p[0];
    h->cec = p[1];
    h->cfg = p[2];
    h->up = p[3];
    h->egr = t5_get32(p + 4);
    h->ingr = t5_get32(p + 8);
    h->egerr = t5_get32(p + 12);
    h->ingerr = t5_get32(p + 16);
    h->loop = t5_get32(p + 20);
    h->subs = t5_get16(p + 24);
    h->shared = p[26];
    h->frames = t5_get32(p + 28);
    h->fmt = t5_get32(p + 32);
    h->crc = t5_get32(p + 36);
    h->hmac = t5_get32(p + 40);
    h->src = t5_get32(p + 44);
    h->age = t5_get32(p + 48);
    return true;
}

typedef struct { uint8 iface, en, state, subs; uint32 tx, rx, txerr, rxerr; } cs_t;

static bool get_cs(cs_t *cs)
{
    int from = t5_ntx, i;
    const t5_tx_t *t;
    memset(cs, 0, 8 * sizeof(*cs));
    if (!simple_cmd(CC_STATUS)) return false;
    t = t5_last_tx(GW_CONN_MID, from);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_CONN_STATUS did not transmit GW_ConnTlm_t on GW_CONN_TLM_MID (0x08D9)");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + CONN_TLM_SIZE, "GW_ConnTlm_t is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + CONN_TLM_SIZE));
    for (i = 0; i < 8; i++)
    {
        const uint8 *p = t->bytes + TLM_HDR + i * 20;
        cs[i].iface = p[0];
        cs[i].en = p[1];
        cs[i].state = p[2];
        cs[i].subs = p[3];
        cs[i].tx = t5_get32(p + 4);
        cs[i].rx = t5_get32(p + 8);
        cs[i].txerr = t5_get32(p + 12);
        cs[i].rxerr = t5_get32(p + 16);
    }
    return true;
}

/* True when `text` holds `v` as a decimal number of its own. */
static bool has_num(const char *text, unsigned long v)
{
    const char *p = text;
    char *end;
    while (*p)
    {
        if (*p >= '0' && *p <= '9')
        {
            unsigned long x = strtoul(p, &end, 10);
            if (x == v) return true;
            p = end;
        }
        else
            p++;
    }
    return false;
}

/* A GW_FRAME_REJECT_ERR_EID since `from` naming the reason, the connection index and (src >= 0) the source ID. */
static bool reject_ev(int from, const char *reason, unsigned idx, long src)
{
    int i;
    for (i = from; i < t5_nev; i++)
    {
        if (t5_ev[i].id != EID_REJECT || strstr(t5_ev[i].text, reason) == NULL) continue;
        if (!has_num(t5_ev[i].text, idx)) continue;
        if (src >= 0 && !has_num(t5_ev[i].text, (unsigned long)src)) continue;
        return true;
    }
    return false;
}

/* ------------------------------------------------------------------ frames */

static uint32 tseq;

static size_t mkframe(uint8 *out, const uint8 *cmd, size_t clen, const uint8 *key, uint16 src, int32 age_off)
{
    return t5_frame(out, cmd, clen, key, src, (uint32)((int64)t5_now_sec + age_off), tseq++);
}

static size_t noop_frame(uint8 *out, const uint8 *key, uint16 src, int32 age_off)
{
    uint8 cmd[CMD_HDR];
    t5_build_cmd(cmd, SAMPLE_CMD_MID, 0, NULL, 0);
    return mkframe(out, cmd, CMD_HDR, key, src, age_off);
}

/* A frame with one MAC bit flipped and the CRC recomputed. */
static void bad_mac(uint8 *f, size_t n)
{
    f[n - 2 - 32 + 3] ^= 0x01;
    t5_wbe16(f + n - 2, t5_crc16(f, n - 2));
}

static int noops_since(int from) { return t5_tx_count(SAMPLE_CMD_MID, from); }

/* Datagrams GW sent to UDP port `port` since t5_sent index `from` (indices into t5_sent). */
static int udp_out(uint16 port, int from, int *idx, int max)
{
    int i, n = 0;
    for (i = from; i < t5_nsent && n < max; i++)
        if (!t5_sent[i].stream && t5_sent[i].port == port) idx[n++] = i;
    return n;
}

/* Check one unit GW egressed: a valid frame under `key` carrying `pkt`, with the given header fields. */
static bool check_egress(const uint8 *f, size_t n, const uint8 *key, const uint8 *pkt, size_t plen, uint16 src,
                         uint32 seq, uint32 secs, const char *what)
{
    int rc = t5_check_frame(f, n, key);
    bool ok = true;
    UtAssert_True(rc == 0, "%s: the unit is a CMDI frame (magic, Version 1, Flags 0, 50 + L bytes) whose CRC and MAC "
                  "verify under the table key (first problem status %d)", what, rc);
    if (rc != 0) return false;
    ok &= UtAssert_True(t5_be16(f + 4) == src, "%s: SourceId %u, expected the node ID %u", what, t5_be16(f + 4), src);
    ok &= UtAssert_True(t5_be32(f + 12) == seq, "%s: Sequence %u, expected %u (per connection, from 1 after it is opened)",
                        what, (unsigned)t5_be32(f + 12), (unsigned)seq);
    ok &= UtAssert_True(t5_be32(f + 8) == secs, "%s: TimeSeconds %u, expected the onboard seconds %u", what,
                        (unsigned)t5_be32(f + 8), (unsigned)secs);
    ok &= UtAssert_True(t5_be16(f + 6) == plen && memcmp(f + 16, pkt, plen) == 0,
                        "%s: the frame carries the subscribed packet unchanged (L %u, packet %u bytes)", what,
                        t5_be16(f + 6), (unsigned)plen);
    return ok;
}

/* Split stream bytes into frames; `clean` is false when anything but the marker precedes a frame. */
static int stream_frames(const uint8 *s, size_t n, size_t *off, size_t *len, int max, bool *clean)
{
    size_t i = 0;
    int k = 0;
    *clean = true;
    while (i < n && k < max)
    {
        uint16 l;
        if (i + 4 > n || memcmp(s + i, MARKER, 4) != 0)
        {
            *clean = false;
            i++;
            continue;
        }
        if (i + 4 + 16 > n)
        {
            *clean = false;
            break;
        }
        l = t5_be16(s + i + 10);
        if (l < 8 || l > 1024 || i + 4 + 50 + l > n)
        {
            *clean = false;
            break;
        }
        off[k] = i + 4;
        len[k] = 50u + l;
        k++;
        i += 4u + 50u + l;
    }
    return k;
}

/* Write marker + frame (optionally after `pre` bytes of prefix) to a TCP port or a device. */
static size_t framed(uint8 *wr, const uint8 *pre, size_t npre, const uint8 *f, size_t n)
{
    if (npre) memcpy(wr, pre, npre);
    memcpy(wr + npre, MARKER, 4);
    memcpy(wr + npre + 4, f, n);
    return npre + 4 + n;
}

/* ------------------------------------------------------------------ local model refinements */

/* Device paths under /dev name host devices: opening one that does not exist fails whatever the flags. */
static void H_OpenDev(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *vpath = UT_Hook_GetArgValueByName(c, "path", const char *);
    char path[NAME_MAX_TEST];
    int i;
    t5_translate(vpath, path, sizeof(path));
    if (strncmp(path, "/dev/", 5) == 0)
    {
        i = mf_find(path);
        if (i < 0 || !mf[i].dev)
        {
            t5_ret(key, OS_FS_ERR_PATH_INVALID);
            return;
        }
    }
    H_OS_OpenCreate(ud, key, c);
}

/* A one-shot I/O error on the next read of a stream socket that has data. */
static bool fail_stream_read;

static void H_ReadMaybeFail(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t id = UT_Hook_GetArgValueByName(c, "filedes", osal_id_t);
    int s = t5_sock_index(id);
    if (fail_stream_read && s >= 0 && t5_sock[s].stream && t5_sock[s].rx_len > 0)
    {
        fail_stream_read = false;
        t5_ret(key, OS_ERROR);
        return;
    }
    if (ud != NULL) H_OS_TimedRead(NULL, key, c);
    else H_OS_read(NULL, key, c);
}

/* ES STOP_APP then START_APP: the stopped instance's pipes, subscriptions and own tables go away. */
static bool restart_app(void)
{
    int k;
    t5_app_end();
    memset(t5_pipes, 0, sizeof(t5_pipes));
    memset(t5_subs, 0, sizeof(t5_subs));
    for (k = 0; k < T5_TBL_MAX; k++)
        if (t5_tbl[k].used && t5_tbl[k].owned) t5_tbl[k].used = false;
    t5_exit_called = false; /* the stopped instance's exit is expected */
    return t5_app_start();
}

/* ------------------------------------------------------------------ fixture */

static void Setup(void)
{
    t5_setup();
    memset(conntbl, 0, sizeof(conntbl));
    valtbl_make(valtbl, KEY0, 30, 0x0F);
    t5_app_entry = GW_AppMain;
    tseq = 500;
    fail_stream_read = false;
    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate), H_OpenDev, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_read), H_ReadMaybeFail, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TimedRead), H_ReadMaybeFail, (void *)1);
}

static void Teardown(void) { t5_teardown(); }

static bool boot(bool share_available)
{
    t5_tbl_set_file("ConnTable", conntbl, TBL_SIZE);
    t5_tbl_offer("CMDI.ValTable", valtbl, VAL_SIZE, share_available);
    if (!t5_app_start()) return false;
    UtAssert_True(!t5_exit_called, "GW_AppMain keeps running after initialization (exit status %u)",
                  (unsigned)t5_exit_status);
    return !t5_exit_called;
}

/* Expect an ADD_CONN to be rejected: CONN_ADD_ERR, a command error, nothing configured. */
static bool add_rejected(uint8 idx, const conn_t *c, const char *what)
{
    hk_t a, b;
    cs_t cs[8];
    int ev0;
    bool ok;
    if (!get_hk(&a)) return false;
    ev0 = t5_nev;
    add_conn(idx, c);
    if (!get_hk(&b)) return false;
    ok = UtAssert_True(t5_count_ev(EID_ADD_ERR, ev0) == 1 && b.cec == (uint8)(a.cec + 1) && b.cc == a.cc,
                       "ADD_CONN %s is rejected with GW_CONN_ADD_ERR_EID (12) and counted as a command error", what);
    ok &= UtAssert_True(b.cfg == a.cfg, "a rejected ADD_CONN (%s) leaves ConnectionsConfigured %u (now %u)", what, a.cfg,
                        b.cfg);
    if (idx < 8 && a.cfg == 0 && get_cs(cs))
        ok &= UtAssert_True(cs[idx].iface == 0, "a rejected ADD_CONN (%s) leaves slot %u unused", what, idx);
    return ok;
}

static bool wait_state(int slot, uint8 want, int max_wakeups)
{
    cs_t cs[8];
    int i;
    for (i = 0; i < max_wakeups; i++)
    {
        wakeup();
        if (get_cs(cs) && cs[slot].state == want) return true;
    }
    return false;
}

/* One egress and one ingress over a SERIAL connection on device `dev` subscribed to `sub`. */
static bool serial_traffic(int dev, uint32 sub, uint32 seq, const char *what)
{
    uint8 pkt[64], f[256], wr[512], buf[4096];
    size_t pn, n, sn, off[4], len[4];
    int cnt, from;
    bool clean, ok = true;
    t5_dev_clear_out(dev);
    pn = t5_build_verif(pkt, sub, 40, 7, 4);
    t5_deliver(pkt, pn);
    sn = t5_stream_out(0, dev, buf, sizeof(buf));
    cnt = stream_frames(buf, sn, off, len, 4, &clean);
    ok &= UtAssert_True(cnt == 1 && clean, "%s: egress on SERIAL is the marker followed by one frame (%d frames, %s)", what,
                        cnt, clean ? "nothing else" : "other bytes present");
    if (cnt == 1) ok &= check_egress(buf + off[0], len[0], KEY0, pkt, pn, 1, seq, t5_now_sec, what);
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_dev_write(dev, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    ok &= UtAssert_True(noops_since(from) == 1, "%s: a marker-framed frame on SERIAL is published (%d)", what,
                        noops_since(from));
    return ok;
}

/* ================================================================== tests */

static void Test_Startup(void)
{
    hk_t h;
    cs_t cs[8];
    int k, i, used = 0;
    if (!boot(true)) return;
    UtAssert_True(t5_count_ev(EID_INIT, 0) == 1, "GW_INIT_INF_EID (1) is sent once at start-up (%d)",
                  t5_count_ev(EID_INIT, 0));
    UtAssert_True(t5_last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION type");
    k = t5_tbl_find("ConnTable");
    UtAssert_True(k >= 0 && t5_tbl[k].owned, "GW registers its table as ConnTable");
    if (k >= 0)
    {
        UtAssert_True(t5_tbl[k].size == TBL_SIZE, "ConnTable is %u bytes, expected %u (8 GW_ConnDef_t of 152 bytes)",
                      (unsigned)t5_tbl[k].size, TBL_SIZE);
        UtAssert_True(t5_tbl[k].validate != NULL, "ConnTable has a validation function");
    }
    UtAssert_True(t5_subscribed(GW_CMD_MID) && t5_subscribed(GW_SEND_HK_MID) && t5_subscribed(GW_WAKEUP_MID),
                  "GW subscribes to GW_CMD_MID, GW_SEND_HK_MID and GW_WAKEUP_MID");
    UtAssert_True(t5_n_share_calls >= 1, "GW shares CMDI.ValTable with CFE_TBL_Share at initialization");
    if (!get_hk(&h)) return;
    UtAssert_True(h.cc == 0 && h.cec == 0 && h.egr == 0 && h.ingr == 0 && h.frames == 0,
                  "housekeeping counters start at zero");
    UtAssert_True(h.cfg == 0 && h.up == 0 && h.subs == 0, "the default table has every slot unused "
                  "(ConnectionsConfigured %u, ConnectionsUp %u, Subscriptions %u)", h.cfg, h.up, h.subs);
    UtAssert_True(h.shared == 1, "ValTableShared is 1 once CMDI.ValTable is shared (%u)", h.shared);
    if (get_cs(cs))
    {
        for (i = 0; i < 8; i++) used += cs[i].iface != 0;
        UtAssert_True(used == 0, "the connection status lists no used slot after a default start (%d)", used);
        UtAssert_True(t5_count_ev(EID_STATUS_INF, 0) == 1, "SEND_CONN_STATUS sends GW_CONN_STATUS_INF_EID (24)");
    }
    simple_cmd(CC_NOOP);
    UtAssert_True(t5_count_ev(EID_NOOP, 0) == 1, "NOOP sends GW_NOOP_INF_EID (3)");
    if (get_hk(&h)) UtAssert_True(h.cc == 2, "NOOP and SEND_CONN_STATUS each increment CommandCounter (%u of 2)", h.cc);
}

static void Test_TableShare(void)
{
    hk_t h;
    int ev0, calls, ga;
    if (!boot(false)) return;
    UtAssert_True(t5_count_ev(EID_VALTBL_ERR, 0) == 1, "a failed share at initialization sends one GW_VALTBL_ERR_EID (26) "
                  "(%d)", t5_count_ev(EID_VALTBL_ERR, 0));
    if (get_hk(&h)) UtAssert_True(h.shared == 0, "ValTableShared is 0 while CMDI.ValTable cannot be shared (%u)", h.shared);
    calls = t5_n_share_calls;
    wakeups(5);
    UtAssert_True(t5_n_share_calls - calls >= 5, "GW retries the share on every WAKEUP until it succeeds (%d tries in 5 "
                  "wakeups)", t5_n_share_calls - calls);
    UtAssert_True(t5_count_ev(EID_VALTBL_ERR, 0) == 1, "the failure streak sends GW_VALTBL_ERR_EID only once (%d)",
                  t5_count_ev(EID_VALTBL_ERR, 0));
    t5_tbl_offer("CMDI.ValTable", valtbl, VAL_SIZE, true);
    wakeup();
    UtAssert_True(t5_count_ev(EID_VALTBL_INF, 0) == 1, "a successful share sends GW_VALTBL_INF_EID (27)");
    if (get_hk(&h)) UtAssert_True(h.shared == 1, "ValTableShared is 1 after the share succeeds (%u)", h.shared);
    ga = t5_n_getaddress;
    wakeups(4);
    UtAssert_True(t5_n_getaddress - ga >= 4, "GW calls CFE_TBL_GetAddress on the shared handle on every WAKEUP (%d calls "
                  "in 4 wakeups)", t5_n_getaddress - ga);
    /* The owner goes away (CMDI restarted): the old handle no longer gives an address. */
    ev0 = t5_nev;
    calls = t5_n_share_calls;
    t5_tbl_shared_unregister("CMDI.ValTable");
    wakeups(4);
    if (get_hk(&h)) UtAssert_True(h.shared == 0, "ValTableShared is 0 once CFE_TBL_GetAddress fails (%u)", h.shared);
    UtAssert_True(t5_count_ev(EID_VALTBL_ERR, ev0) == 1, "the new failure streak sends one GW_VALTBL_ERR_EID (%d)",
                  t5_count_ev(EID_VALTBL_ERR, ev0));
    UtAssert_True(t5_n_share_calls > calls, "GW calls CFE_TBL_Share again after losing the shared table");
    t5_tbl_offer("CMDI.ValTable", valtbl, VAL_SIZE, true);
    calls = t5_n_share_calls;
    wakeups(2);
    UtAssert_True(t5_n_share_calls > calls, "GW shares the re-registered table again");
    UtAssert_True(t5_count_ev(EID_VALTBL_INF, ev0) == 1, "sharing again sends GW_VALTBL_INF_EID (%d)",
                  t5_count_ev(EID_VALTBL_INF, ev0));
    if (get_hk(&h)) UtAssert_True(h.shared == 1, "ValTableShared returns to 1 (%u)", h.shared);
}

static void Test_UnsharedHoldsTraffic(void)
{
    conn_t c;
    hk_t h0, h;
    uint8 f[128], pkt[64];
    size_t n, pn;
    int s, from_tx, from_sent, idx[4];
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, V_E4);
    add_conn(5, &c);
    t5_tbl_shared_unregister("CMDI.ValTable");
    wakeups(2);
    if (!get_hk(&h0)) return;
    UtAssert_True(h0.shared == 0, "ValTableShared is 0 without the shared table (%u)", h0.shared);
    n = noop_frame(f, KEY0, 1, 0);
    UtAssert_True(t5_udp_inject(6005, f, n), "GW keeps its UDP connection bound to LocalPort 6005");
    from_tx = t5_ntx;
    wakeups(3);
    s = t5_sock_by_port(6005);
    UtAssert_True(s >= 0 && t5_sock[s].dq_count == 1, "while ValTableShared is 0 GW reads nothing from its connections "
                  "(the datagram waits in the socket)");
    UtAssert_True(noops_since(from_tx) == 0, "nothing is published while ValTableShared is 0");
    from_sent = t5_nsent;
    pn = t5_build_verif(pkt, V_E4, 40, 1, 4);
    t5_deliver(pkt, pn);
    UtAssert_True(udp_out(7005, from_sent, idx, 4) == 0, "nothing is egressed while ValTableShared is 0");
    if (get_hk(&h)) UtAssert_True(h.egerr == h0.egerr + 1 && h.frames == h0.frames, "a skipped write counts as an egress "
                                  "error (EgressErrors %u -> %u) and FramesReceived stays %u", (unsigned)h0.egerr,
                                  (unsigned)h.egerr, (unsigned)h0.frames);
    t5_tbl_offer("CMDI.ValTable", valtbl, VAL_SIZE, true);
    wakeups(3);
    UtAssert_True(noops_since(from_tx) == 1, "the waiting frame is read and published once the table is shared again (%d)",
                  noops_since(from_tx));
}

static void Test_KeyChange(void)
{
    conn_t c;
    hk_t h0, h;
    uint8 f[128], pkt[64];
    size_t n, pn;
    int from, from_sent, idx[4], cnt;
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, V_E4);
    add_conn(5, &c);
    wakeup();
    if (!get_hk(&h0)) return;
    valtbl_make(valtbl, KEY2, 30, 0x0F);
    t5_tbl_shared_update("CMDI.ValTable", valtbl, VAL_SIZE);
    wakeup();
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    UtAssert_True(memcmp(cmdval_last_key, KEY2, 32) == 0, "after a table update of CMDI.ValTable GW validates with the new "
                  "key from its next WAKEUP");
    UtAssert_True(noops_since(from) == 0, "a frame under the old key is not published after the key change");
    if (get_hk(&h)) UtAssert_True(h.hmac == h0.hmac + 1, "the old-key frame counts under HmacErrors (%u -> %u)",
                                  (unsigned)h0.hmac, (unsigned)h.hmac);
    n = noop_frame(f, KEY2, 1, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    UtAssert_True(noops_since(from) == 1, "a frame under the new key is published (%d)", noops_since(from));
    from_sent = t5_nsent;
    pn = t5_build_verif(pkt, V_E4, 40, 2, 4);
    t5_deliver(pkt, pn);
    cnt = udp_out(7005, from_sent, idx, 4);
    UtAssert_True(cnt == 1, "the subscribed packet is egressed once (%d)", cnt);
    if (cnt == 1)
    {
        UtAssert_True(t5_check_frame(t5_sent[idx[0]].bytes, t5_sent[idx[0]].size, KEY2) == 0,
                      "egress frames carry a MAC under the new key");
        UtAssert_True(t5_check_frame(t5_sent[idx[0]].bytes, t5_sent[idx[0]].size, KEY0) == 3,
                      "egress frames no longer verify under the old key");
    }
}

static void Test_DefinitionValidation(void)
{
    conn_t c;
    hk_t h;
    int ev0;
    char longdev[70];
    if (!boot(true)) return;
    udp_conn(&c, 0, 0, 7000, 0);
    add_rejected(8, &c, "with Index 8");
    conn_set(&c, 0, 0, 0, "127.0.0.1", 7000, NULL);
    add_rejected(0, &c, "with Interface NONE");
    conn_set(&c, 4, 0, 0, "127.0.0.1", 7000, NULL);
    add_rejected(0, &c, "with Interface 4");
    udp_conn(&c, 2, 0, 7000, 0);
    add_rejected(0, &c, "with Enabled 2");
    conn_set(&c, IF_UDP, 0, 0, "256.1.1.1", 7000, NULL);
    add_rejected(0, &c, "with RemoteAddr 256.1.1.1");
    conn_set(&c, IF_UDP, 0, 0, "1.2.3", 7000, NULL);
    add_rejected(0, &c, "with RemoteAddr 1.2.3");
    conn_set(&c, IF_TCP, 0, 0, "localhost", 7000, NULL);
    add_rejected(0, &c, "with a RemoteAddr that is not dotted IPv4");
    conn_set(&c, IF_UDP, 0, 0, "1111111111111111", 7000, NULL);
    add_rejected(0, &c, "with an unterminated RemoteAddr");
    conn_set(&c, IF_UDP, 0, 0, "127.0.0.1", 0, NULL);
    add_rejected(0, &c, "UDP with RemotePort 0");
    conn_set(&c, IF_TCP, 0, 0, "127.0.0.1", 0, NULL);
    add_rejected(0, &c, "TCP with RemotePort 0");
    conn_set(&c, IF_TCP, 0, 5000, "127.0.0.1", 7000, NULL);
    add_rejected(0, &c, "TCP with a nonzero LocalPort");
    conn_set(&c, IF_SERIAL, 0, 5000, NULL, 0, "/dev/pts/3");
    add_rejected(0, &c, "SERIAL with a nonzero LocalPort");
    conn_set(&c, IF_SERIAL, 0, 0, NULL, 0, "dev/pts/3");
    add_rejected(0, &c, "SERIAL with a DevicePath not starting with /");
    memset(longdev, 'a', sizeof(longdev));
    longdev[0] = '/';
    longdev[69] = 0;
    conn_set(&c, IF_SERIAL, 0, 0, NULL, 0, longdev);
    add_rejected(0, &c, "SERIAL with an unterminated DevicePath");
    udp_conn(&c, 0, 0, 7000, V_E4);
    conn_sub(&c, 3, V_E4);
    add_rejected(0, &c, "with a duplicate used MID in Subs");
    /* Valid definitions, disabled so nothing is opened. */
    ev0 = t5_nev;
    udp_conn(&c, 0, 6000, 7000, V_E4);
    add_conn(0, &c);
    conn_set(&c, IF_TCP, 0, 0, "10.1.2.3", 7001, NULL);
    add_conn(1, &c);
    conn_set(&c, IF_SERIAL, 0, 0, NULL, 0, "/dev/pts/3");
    add_conn(2, &c);
    UtAssert_True(t5_count_ev(EID_ADD_INF, ev0) == 3 && t5_count_ev(EID_ADD_ERR, ev0) == 0,
                  "valid UDP, TCP and SERIAL definitions are accepted with GW_CONN_ADD_INF_EID (11) (%d of 3)",
                  t5_count_ev(EID_ADD_INF, ev0));
    if (get_hk(&h)) UtAssert_True(h.cfg == 3, "ConnectionsConfigured counts the used slots (%u of 3)", h.cfg);
    udp_conn(&c, 0, 0, 7002, 0);
    add_rejected(0, &c, "into a used slot");
    udp_conn(&c, 0, 6000, 7003, 0);
    add_rejected(3, &c, "UDP sharing LocalPort 6000 with another UDP connection");
}

static void Test_TableValidation(void)
{
    conn_t c;
    uint8 t[TBL_SIZE];
    int ev0, k;
    if (!boot(true)) return;
    k = t5_tbl_find("ConnTable");
    if (k < 0 || t5_tbl[k].validate == NULL)
    {
        UtAssert_Failed("ConnTable has no validation function");
        return;
    }
    memset(t, 0, sizeof(t));
    UtAssert_True(t5_tbl_validate("ConnTable", t) == CFE_SUCCESS, "a table with every slot unused is valid");
    udp_conn(&c, 1, 6000, 7000, V_E4);
    tbl_put(t, 0, &c);
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/4");
    tbl_put(t, 7, &c);
    UtAssert_True(t5_tbl_validate("ConnTable", t) == CFE_SUCCESS, "a table with valid UDP and SERIAL definitions is valid");
    ev0 = t5_nev;
    conn_set(&c, 4, 0, 0, "127.0.0.1", 7000, NULL);
    tbl_put(t, 2, &c);
    UtAssert_True(t5_tbl_validate("ConnTable", t) != CFE_SUCCESS, "a used slot with Interface 4 makes the table invalid");
    UtAssert_True(t5_count_ev(EID_TBL_VAL, ev0) >= 1, "a rejected table sends GW_TBL_VAL_ERR_EID (10)");
    conn_set(&c, IF_TCP, 0, 9, "127.0.0.1", 7000, NULL);
    tbl_put(t, 2, &c);
    UtAssert_True(t5_tbl_validate("ConnTable", t) != CFE_SUCCESS, "a TCP definition with a nonzero LocalPort makes the "
                  "table invalid");
    udp_conn(&c, 0, 6000, 7001, 0);
    tbl_put(t, 2, &c);
    UtAssert_True(t5_tbl_validate("ConnTable", t) != CFE_SUCCESS, "two UDP definitions sharing LocalPort 6000 make the "
                  "table invalid");
    conn_set(&c, IF_UDP, 0, 0, "300.0.0.1", 7001, NULL);
    tbl_put(t, 2, &c);
    UtAssert_True(t5_tbl_validate("ConnTable", t) != CFE_SUCCESS, "an invalid RemoteAddr makes the table invalid");
}

static void Test_EgressFraming(void)
{
    conn_t c;
    hk_t h;
    cs_t cs[8];
    uint8 p1[64], p2[64], p3[160];
    size_t n1, n2, n3;
    int from, i0[8], i1[8], c0, c1, hm, cr;
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7000, V_E4);
    add_conn(0, &c);
    udp_conn(&c, 1, 0, 7001, V_E4);
    conn_sub(&c, 1, V_E5);
    add_conn(1, &c);
    from = t5_nsent;
    hm = cmdval_n_hmac;
    cr = cmdval_n_crc;
    t5_now_sec = 1234567;
    n1 = t5_build_verif(p1, V_E4, 40, 11, 4);
    n2 = t5_build_verif(p2, V_E5, 36, 12, 4);
    n3 = t5_build_verif(p3, V_E4, 120, 13, 4);
    t5_deliver(p1, n1);
    t5_deliver(p2, n2);
    t5_now_sec = 1234569;
    t5_deliver(p3, n3);
    c0 = udp_out(7000, from, i0, 8);
    c1 = udp_out(7001, from, i1, 8);
    UtAssert_True(c0 == 2 && c1 == 3, "each subscribed packet is written to every enabled connection listing its MID, one "
                  "frame per datagram (port 7000 got %d of 2, port 7001 %d of 3)", c0, c1);
    if (c0 == 2)
    {
        check_egress(t5_sent[i0[0]].bytes, t5_sent[i0[0]].size, KEY0, p1, n1, 1, 1, 1234567, "connection 0, first frame");
        check_egress(t5_sent[i0[1]].bytes, t5_sent[i0[1]].size, KEY0, p3, n3, 1, 2, 1234569, "connection 0, second frame");
        UtAssert_True(strcmp(t5_sent[i0[0]].ip, "127.0.0.1") == 0, "egress goes to RemoteAddr (%s)", t5_sent[i0[0]].ip);
    }
    if (c1 == 3)
    {
        check_egress(t5_sent[i1[0]].bytes, t5_sent[i1[0]].size, KEY0, p1, n1, 1, 1, 1234567, "connection 1, first frame");
        check_egress(t5_sent[i1[1]].bytes, t5_sent[i1[1]].size, KEY0, p2, n2, 1, 2, 1234567, "connection 1, second frame");
        check_egress(t5_sent[i1[2]].bytes, t5_sent[i1[2]].size, KEY0, p3, n3, 1, 3, 1234569, "connection 1, third frame");
    }
    UtAssert_True(cmdval_n_hmac - hm >= 5 && cmdval_n_crc - cr >= 5, "GW builds egress frames with CMDVAL_HmacSha256 and "
                  "CMDVAL_Crc16 (%d and %d calls for 5 frames)", cmdval_n_hmac - hm, cmdval_n_crc - cr);
    if (get_hk(&h)) UtAssert_True(h.egr == 5 && h.egerr == 0, "PacketsEgressed counts every write (%u of 5, EgressErrors %u)",
                                  (unsigned)h.egr, (unsigned)h.egerr);
    if (get_cs(cs))
        UtAssert_True(cs[0].tx == 2 && cs[1].tx == 3 && cs[0].txerr == 0, "TxPackets per connection (%u of 2, %u of 3)",
                      (unsigned)cs[0].tx, (unsigned)cs[1].tx);
}

static void Test_EgressNodeId(void)
{
    conn_t c;
    uint8 p[64];
    size_t n;
    int from, idx[4];
    t5_node_id = 2;
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7000, V_E4);
    add_conn(0, &c);
    from = t5_nsent;
    n = t5_build_verif(p, V_E4, 40, 1, 4);
    t5_deliver(p, n);
    if (UtAssert_True(udp_out(7000, from, idx, 4) == 1, "the packet is egressed on node 2"))
        check_egress(t5_sent[idx[0]].bytes, t5_sent[idx[0]].size, KEY0, p, n, 2, 1, t5_now_sec, "node 2");
}

static void Test_EgressErrors(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    uint8 big[1200], p[64];
    size_t nb, n;
    int ev0, from, idx[8];
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7400, V_E4);
    add_conn(0, &c);
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    from = t5_nsent;
    nb = t5_build_verif(big, V_E4, 1100, 1, 4);
    t5_deliver(big, nb);
    t5_deliver(big, nb);
    UtAssert_True(udp_out(7400, from, idx, 8) == 0, "a packet longer than 1024 bytes is not egressed");
    if (get_hk(&h)) UtAssert_True(h.egerr == a.egerr + 2, "each packet longer than 1024 bytes counts an egress error "
                                  "(EgressErrors %u -> %u)", (unsigned)a.egerr, (unsigned)h.egerr);
    UtAssert_True(t5_count_ev(EID_EGRESS_ERR, ev0) == 1, "GW_EGRESS_ERR_EID (22) is sent once per connection until its "
                  "next successful write (%d)", t5_count_ev(EID_EGRESS_ERR, ev0));
    n = t5_build_verif(p, V_E4, 40, 2, 4);
    t5_deliver(p, n);
    UtAssert_True(udp_out(7400, from, idx, 8) == 1, "a normal packet is egressed after the errors");
    t5_deliver(big, nb);
    UtAssert_True(t5_count_ev(EID_EGRESS_ERR, ev0) == 2, "after a successful write the next egress error sends "
                  "GW_EGRESS_ERR_EID again (%d)", t5_count_ev(EID_EGRESS_ERR, ev0));
    /* A UDP write that fails is an I/O failure: the connection is disabled. */
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    t5_sendto_rc = OS_ERROR;
    t5_deliver(p, n);
    t5_sendto_rc = OS_SUCCESS;
    UtAssert_True(t5_count_ev(EID_DOWN_ERR, ev0) == 1, "an I/O failure on a UDP connection sends GW_CONN_DOWN_ERR_EID (21)");
    if (get_cs(cs)) UtAssert_True(cs[0].en == 0 && cs[0].state == ST_DISABLED, "an I/O failure disables the UDP connection "
                                  "(Enabled %u, State %u)", cs[0].en, cs[0].state);
    if (get_hk(&h)) UtAssert_True(h.egerr >= a.egerr + 1, "the failed write counts as an egress error (%u -> %u)",
                                  (unsigned)a.egerr, (unsigned)h.egerr);
}

static void Test_Subscriptions(void)
{
    conn_t c;
    hk_t h;
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7000, V_E4);
    conn_sub(&c, 1, V_E5);
    add_conn(0, &c);
    udp_conn(&c, 1, 0, 7001, V_E5);
    conn_sub(&c, 5, V_E6);
    add_conn(1, &c);
    udp_conn(&c, 0, 0, 7002, V_E7);
    add_conn(2, &c);
    UtAssert_True(t5_subscribed(V_E4) && t5_subscribed(V_E5) && t5_subscribed(V_E6),
                  "GW subscribes to the union of the enabled connections' Subs");
    UtAssert_True(t5_subscribed(V_E7) == 0, "a disabled connection's Subs are not subscribed");
    UtAssert_True(t5_sub_msglim(V_E4) == 16 && t5_sub_msglim(V_E5) == 16 && t5_sub_msglim(V_E6) == 16,
                  "egress subscriptions use a message limit of 16 (%u, %u, %u)", t5_sub_msglim(V_E4), t5_sub_msglim(V_E5),
                  t5_sub_msglim(V_E6));
    if (get_hk(&h)) UtAssert_True(h.subs == 3, "Subscriptions counts the subscribed MIDs (%u of 3)", h.subs);
    idx_cmd(CC_DISABLE, 1);
    UtAssert_True(t5_subscribed(V_E6) == 0 && t5_subscribed(V_E5) != 0, "disabling a connection unsubscribes the MIDs no "
                  "other enabled connection lists and keeps the others");
    idx_cmd(CC_ENABLE, 2);
    UtAssert_True(t5_subscribed(V_E7) != 0, "enabling a connection subscribes its Subs");
    if (get_hk(&h)) UtAssert_True(h.subs == 3, "Subscriptions follows the union (%u of 3)", h.subs);
    idx_cmd(CC_REMOVE, 0);
    UtAssert_True(t5_subscribed(V_E4) == 0 && t5_subscribed(V_E5) == 0 && t5_subscribed(V_E7) != 0,
                  "removing a connection unsubscribes MIDs no longer needed");
    if (get_hk(&h)) UtAssert_True(h.subs == 1, "Subscriptions follows the union (%u of 1)", h.subs);
}

static void Test_UdpIngress(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    uint8 f[256], cmd[CMD_HDR];
    size_t n;
    int from, ev0, v0;
    const t5_tx_t *t;
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, 0);
    add_conn(5, &c);
    if (!get_hk(&a)) return;
    UtAssert_True(t5_sock_by_port(6005) >= 0, "a UDP connection with LocalPort 6005 binds that port");
    from = t5_ntx;
    v0 = cmdval_n_validate;
    t5_build_cmd(cmd, SAMPLE_CMD_MID, 0, NULL, 0);
    n = mkframe(f, cmd, CMD_HDR, KEY0, 1, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    UtAssert_True(noops_since(from) == 1, "a valid frame's embedded command is published on the bus (%d)", noops_since(from));
    t = t5_last_tx(SAMPLE_CMD_MID, from);
    if (t != NULL) UtAssert_True(t->size == CMD_HDR && memcmp(t->bytes, cmd, CMD_HDR) == 0,
                                 "the published command is the embedded packet unchanged");
    UtAssert_True(cmdval_n_validate > v0, "GW validates inbound frames with CMDVAL_ValidateFrame");
    UtAssert_True(cmdval_last_now == t5_now_sec, "frames are validated against GW's onboard seconds (%u, now %u)",
                  (unsigned)cmdval_last_now, (unsigned)t5_now_sec);
    if (get_hk(&h)) UtAssert_True(h.frames == a.frames + 1 && h.ingr == a.ingr + 1, "FramesReceived and PacketsIngressed "
                                  "count the accepted frame (%u, %u)", (unsigned)(h.frames - a.frames),
                                  (unsigned)(h.ingr - a.ingr));
    if (get_cs(cs)) UtAssert_True(cs[5].rx == 1 && cs[5].rxerr == 0, "RxPackets counts accepted frames (%u)",
                                  (unsigned)cs[5].rx);
    /* Every rejection reason. */
    if (!get_hk(&a)) return;
    from = t5_ntx;
    ev0 = t5_nev;
    t5_udp_inject(6005, cmd, CMD_HDR); /* unframed */
    n = noop_frame(f, KEY0, 1, 0);
    f[n] = 0;
    t5_udp_inject(6005, f, n + 1); /* size is not 50 + L */
    n = noop_frame(f, KEY0, 2, 0);
    f[n - 1] ^= 0x01;
    t5_udp_inject(6005, f, n); /* CRC */
    n = noop_frame(f, KEY0, 2, 0);
    bad_mac(f, n);
    t5_udp_inject(6005, f, n); /* HMAC */
    n = noop_frame(f, KEY0, 7, 0);
    t5_udp_inject(6005, f, n); /* SOURCE */
    n = noop_frame(f, KEY0, 2, -31);
    t5_udp_inject(6005, f, n); /* AGE (past) */
    n = noop_frame(f, KEY0, 2, 31);
    t5_udp_inject(6005, f, n); /* AGE (future) */
    wakeup();
    UtAssert_True(noops_since(from) == 0, "no rejected frame's command is published (%d)", noops_since(from));
    if (get_hk(&h))
    {
        UtAssert_True(h.frames == a.frames + 7, "FramesReceived counts every inbound frame (%u of 7)",
                      (unsigned)(h.frames - a.frames));
        UtAssert_True(h.fmt == a.fmt + 2, "an unframed packet and a datagram whose size is not 50 + L count under "
                      "FormatErrors (%u of 2)", (unsigned)(h.fmt - a.fmt));
        UtAssert_True(h.crc == a.crc + 1, "a bad CRC counts under CrcErrors (%u of 1)", (unsigned)(h.crc - a.crc));
        UtAssert_True(h.hmac == a.hmac + 1, "a bad MAC counts under HmacErrors (%u of 1)", (unsigned)(h.hmac - a.hmac));
        UtAssert_True(h.src == a.src + 1, "an unlisted source counts under SourceErrors (%u of 1)",
                      (unsigned)(h.src - a.src));
        UtAssert_True(h.age == a.age + 2, "timestamps outside the window, past and future, count under AgeErrors (%u of 2)",
                      (unsigned)(h.age - a.age));
        UtAssert_True(h.ingr == a.ingr, "PacketsIngressed does not count rejected frames");
    }
    if (get_cs(cs)) UtAssert_True(cs[5].rxerr == 7, "the connection's RxErrors counts its rejected frames (%u of 7)",
                                  (unsigned)cs[5].rxerr);
    UtAssert_True(t5_count_ev(EID_REJECT, ev0) == 7, "every rejection sends one GW_FRAME_REJECT_ERR_EID (25) (%d of 7)",
                  t5_count_ev(EID_REJECT, ev0));
    UtAssert_True(t5_last_ev_type(EID_REJECT) == CFE_EVS_EventType_ERROR, "event 25 is ERROR type");
    UtAssert_True(reject_ev(ev0, "FORMAT", 5, -1), "the FORMAT rejection event names FORMAT and connection 5");
    UtAssert_True(reject_ev(ev0, "CRC", 5, 2), "the CRC rejection event names CRC, connection 5 and source 2");
    UtAssert_True(reject_ev(ev0, "HMAC", 5, 2), "the HMAC rejection event names HMAC, connection 5 and source 2");
    UtAssert_True(reject_ev(ev0, "SOURCE", 5, 7), "the SOURCE rejection event names SOURCE, connection 5 and source 7");
    UtAssert_True(reject_ev(ev0, "AGE", 5, 2), "the AGE rejection event names AGE, connection 5 and source 2");
}

static void Test_IngressBatch(void)
{
    conn_t c;
    hk_t a, h;
    uint8 f[128];
    size_t n;
    int i, from;
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, 0);
    add_conn(5, &c);
    if (!get_hk(&a)) return;
    from = t5_ntx;
    for (i = 0; i < 20; i++)
    {
        n = noop_frame(f, KEY0, 1, 0);
        t5_udp_inject(6005, f, n);
    }
    wakeup();
    UtAssert_True(noops_since(from) == 16, "one WAKEUP reads up to 16 frames from a connection (%d published of 20 "
                  "waiting)", noops_since(from));
    wakeup();
    UtAssert_True(noops_since(from) == 20, "the next WAKEUP reads the rest (%d of 20)", noops_since(from));
    UtAssert_True(t5_blocking_recvs == 0, "GW reads its UDP socket without blocking (%d reads would have waited)",
                  t5_blocking_recvs);
    if (get_hk(&h)) UtAssert_True(h.frames - a.frames == 20 && h.ingr - a.ingr == 20, "all 20 frames are counted (%u, %u)",
                                  (unsigned)(h.frames - a.frames), (unsigned)(h.ingr - a.ingr));
}

static void Test_LoopRule(void)
{
    conn_t c;
    hk_t a, h;
    uint8 f[128], cmd[64];
    size_t n, cl;
    int from, from_sent, idx[4];
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, V_E4);
    add_conn(5, &c);
    udp_conn(&c, 1, 0, 7006, V_E5);
    add_conn(6, &c);
    if (!get_hk(&a)) return;
    from = t5_ntx;
    from_sent = t5_nsent;
    cl = t5_build_verif(cmd, V_E5, 24, 9, 4);
    n = mkframe(f, cmd, cl, KEY0, 1, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    UtAssert_True(t5_tx_count(V_E5, from) == 0, "an embedded command whose MID is in the Subs of an enabled connection is "
                  "not published");
    UtAssert_True(udp_out(7006, from_sent, idx, 4) == 0 && udp_out(7005, from_sent, idx, 4) == 0,
                  "the dropped command is not egressed again");
    if (get_hk(&h))
    {
        UtAssert_True(h.loop == a.loop + 1, "LoopDrops counts the dropped command (%u of 1)", (unsigned)(h.loop - a.loop));
        UtAssert_True(h.ingr == a.ingr, "a loop drop is not counted under PacketsIngressed");
    }
    idx_cmd(CC_DISABLE, 6);
    from = t5_ntx;
    n = mkframe(f, cmd, cl, KEY0, 1, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    UtAssert_True(t5_tx_count(V_E5, from) == 1, "once no enabled connection lists the MID the command is published");
}

/* ------------------------------------------------------------------ TCP connect cycle (S5.2) */

#define ATTEMPT_WAKES 50 /* GW_TCP_ATTEMPT_SEC at 10 WAKEUPs per second */
#define CYCLE_WAKES   200 /* 4 attempts x GW_TCP_ATTEMPT_SEC */

/* Stream sockets GW holds open. */
static int stream_socks(void)
{
    int k, n = 0;
    for (k = 0; k < T5_SOCK_MAX; k++) n += t5_sock[k].used && t5_sock[k].stream;
    return n;
}

/* Run WAKEUPs until the onboard clock sits on a whole second, so that a cycle begun now has its attempt
 * starts on whole seconds whatever clock resolution GW uses. */
static void align_second(void)
{
    int guard = 0;
    while (t5_now_sub > 0x00100000u && guard++ < 20) wakeup();
}

/* WAKEUPs since the trace began, the wakeup at which each new connect call came, when GW_CONN_TIMEOUT_ERR_EID
 * and GW_CONN_UP_INF_EID first appeared. */
typedef struct
{
    int w, n, last, ev0, down_at, up_at;
    int at[16];
} trace_t;

static void trace_reset(trace_t *tr)
{
    memset(tr, 0, sizeof(*tr));
    tr->last = t5_n_connect;
    tr->ev0 = t5_nev;
    tr->down_at = tr->up_at = -1;
}

static void trace_run(trace_t *tr, int n)
{
    int i;
    for (i = 0; i < n; i++)
    {
        if (!wakeup()) return;
        tr->w++;
        while (tr->last < t5_n_connect)
        {
            if (tr->n < 16) tr->at[tr->n++] = tr->w;
            tr->last++;
        }
        if (tr->down_at < 0 && t5_count_ev(EID_TIMEOUT_ERR, tr->ev0) > 0) tr->down_at = tr->w;
        if (tr->up_at < 0 && t5_count_ev(EID_UP_INF, tr->ev0) > 0) tr->up_at = tr->w;
    }
}

static bool near(int got, int want) { return got >= want - 1 && got <= want + 1; }

/* The event GW_CONN_TIMEOUT_ERR_EID since `from` names the index, RemoteAddr and RemotePort. */
static bool timeout_ev(int from, unsigned idx, const char *addr, unsigned port)
{
    int i;
    for (i = from; i < t5_nev; i++)
        if (t5_ev[i].id == EID_TIMEOUT_ERR && has_num(t5_ev[i].text, idx) && strstr(t5_ev[i].text, addr) != NULL &&
            has_num(t5_ev[i].text, port))
            return true;
    return false;
}

static void Test_TcpNotListening(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    int ev0;
    if (!boot(true)) return;
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7015, NULL);
    add_conn(6, &c);
    UtAssert_True(t5_count_ev(EID_ADD_INF, ev0) == 1 && t5_count_ev(EID_ADD_ERR, ev0) == 0,
                  "ADD_CONN of an enabled TCP connection whose peer does not listen succeeds with GW_CONN_ADD_INF_EID (a "
                  "refusing peer is not an open failure)");
    conn_set(&c, IF_TCP, 0, 0, "127.0.0.1", 7016, NULL);
    add_conn(7, &c);
    idx_cmd(CC_ENABLE, 7);
    UtAssert_True(t5_count_ev(EID_ENABLE_INF, ev0) == 1 && t5_count_ev(EID_ENABLE_ERR, ev0) == 0,
                  "ENABLE_CONN of a TCP connection whose peer does not listen succeeds with GW_CONN_ENABLE_INF_EID");
    if (get_cs(cs))
        UtAssert_True(cs[6].state == ST_CONNECTING && cs[6].en == 1 && cs[7].state == ST_CONNECTING && cs[7].en == 1,
                      "both connections are CONNECTING while their connect cycle runs (States %u, %u)", cs[6].state,
                      cs[7].state);
    if (get_hk(&h))
        UtAssert_True(h.cfg == a.cfg + 2 && h.cec == a.cec && h.up == 0, "both slots are configured, no command error, "
                      "ConnectionsUp 0 (%u, %u, %u)", h.cfg, h.cec, h.up);
    /* Only a socket that cannot be opened is an open failure. */
    t5_sock_open_rc = OS_ERROR;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7017, NULL);
    add_rejected(5, &c, "of a TCP connection whose socket cannot be opened");
    t5_sock_open_rc = OS_SUCCESS;
    UtAssert_True(t5_connect_waiting == 0, "connect attempts never wait (OS_SocketConnect with a timeout of 0; %d waited)",
                  t5_connect_waiting);
}

static void Test_TcpCycleDown(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    trace_t tr;
    uint8 pkt[64];
    size_t pn;
    int ev0, n0, from, hb_from, hk_ok, i;
    if (!boot(true)) return;
    align_second();
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7009, NULL);
    conn_sub(&c, 0, V_E5);
    ev0 = t5_nev;
    n0 = t5_n_connect;
    add_conn(4, &c);
    UtAssert_True(t5_count_ev(EID_ADD_INF, ev0) == 1, "ADD_CONN to a refusing peer succeeds and begins the connect cycle");
    UtAssert_True(t5_n_connect - n0 == 1, "the cycle's first attempt starts at once (%d connect calls)", t5_n_connect - n0);
    UtAssert_True(stream_socks() == 0, "a refused attempt's socket is closed (%d stream sockets open)", stream_socks());
    trace_reset(&tr);
    trace_run(&tr, 25);
    if (get_cs(cs))
    {
        UtAssert_True(cs[4].state == ST_CONNECTING && cs[4].en == 1, "the connection is CONNECTING while the cycle runs "
                      "(State %u)", cs[4].state);
        UtAssert_True(cs[4].tx == 0 && cs[4].rx == 0 && cs[4].txerr == 0 && cs[4].rxerr == 0, "failed attempts are counted "
                      "in none of the connection's counters (%u, %u, %u, %u)", (unsigned)cs[4].tx, (unsigned)cs[4].rx,
                      (unsigned)cs[4].txerr, (unsigned)cs[4].rxerr);
    }
    if (!get_hk(&a)) return;
    UtAssert_True(a.ingerr == 0, "failed attempts are not ingress errors (%u)", (unsigned)a.ingerr);
    ev0 = t5_nev;
    pn = t5_build_verif(pkt, V_E5, 40, 1, 4);
    t5_deliver(pkt, pn);
    t5_deliver(pkt, pn);
    if (get_hk(&h)) UtAssert_True(h.egerr == a.egerr + 2, "each write to a CONNECTING connection is skipped and counts an "
                                  "egress error (%u of 2)", (unsigned)(h.egerr - a.egerr));
    UtAssert_True(t5_count_ev(EID_EGRESS_ERR, ev0) == 1, "GW_EGRESS_ERR_EID is sent once for the skipped writes (%d)",
                  t5_count_ev(EID_EGRESS_ERR, ev0));
    trace_run(&tr, CYCLE_WAKES + 5 - 25);
    UtAssert_True(tr.n == 3, "a cycle is the first attempt and GW_TCP_MAX_RETRIES (3) retries (%d retries)", tr.n);
    if (tr.n >= 3)
        UtAssert_True(near(tr.at[0], 50) && near(tr.at[1], 100) && near(tr.at[2], 150), "attempts start "
                      "GW_TCP_ATTEMPT_SEC (5) seconds apart, however early the previous one failed (retries at %d, %d and %d "
                      "wakeups after the first, expected 50, 100 and 150)", tr.at[0], tr.at[1], tr.at[2]);
    UtAssert_True(near(tr.down_at, CYCLE_WAKES), "the connection enters DOWN at the end of the fourth attempt's period, 20 s "
                  "after the cycle began (GW_CONN_TIMEOUT_ERR_EID at wakeup %d, expected 200)", tr.down_at);
    UtAssert_True(t5_count_ev(EID_TIMEOUT_ERR, tr.ev0) == 1, "entering DOWN sends one GW_CONN_TIMEOUT_ERR_EID (28) (%d)",
                  t5_count_ev(EID_TIMEOUT_ERR, tr.ev0));
    UtAssert_True(t5_last_ev_type(EID_TIMEOUT_ERR) == CFE_EVS_EventType_ERROR, "event 28 is ERROR type");
    UtAssert_True(timeout_ev(tr.ev0, 4, "127.0.0.1", 7009), "GW_CONN_TIMEOUT_ERR_EID names the connection index 4, "
                  "RemoteAddr 127.0.0.1 and RemotePort 7009 (%s)", t5_last_ev_text(EID_TIMEOUT_ERR, tr.ev0) ?
                  t5_last_ev_text(EID_TIMEOUT_ERR, tr.ev0) : "no event");
    if (get_cs(cs)) UtAssert_True(cs[4].state == ST_DOWN && cs[4].en == 1, "after the cycle the connection is DOWN (3) and "
                                  "Enabled stays 1 (State %u, Enabled %u)", cs[4].state, cs[4].en);
    /* DOWN: no further attempt; the rest of GW keeps running. */
    hb_from = t5_ntx;
    hk_ok = 0;
    for (i = 0; i < 6; i++)
    {
        trace_run(&tr, 50);
        hk_ok += get_hk(&h) ? 1 : 0;
    }
    UtAssert_True(tr.n == 3, "a DOWN connection makes no further attempt (%d retries after 30 s more)", tr.n);
    UtAssert_True(t5_count_ev(EID_TIMEOUT_ERR, tr.ev0) == 1, "GW_CONN_TIMEOUT_ERR_EID is not repeated while DOWN (%d)",
                  t5_count_ev(EID_TIMEOUT_ERR, tr.ev0));
    UtAssert_True(t5_tx_count(SFI_HB_MID, hb_from) == 30 && hk_ok == 6, "housekeeping and heartbeats keep flowing while a "
                  "connection is DOWN (%d heartbeats in 30 s, %d of 6 housekeeping replies)", t5_tx_count(SFI_HB_MID, hb_from),
                  hk_ok);
    if (h.up != 0) UtAssert_Failed("ConnectionsUp is %u with the only connection DOWN", h.up);
    from = (int)h.egerr;
    t5_deliver(pkt, pn);
    if (get_hk(&h)) UtAssert_True(h.egerr == (uint32)from + 1, "a write to a DOWN connection is skipped and counts an egress "
                                  "error (%u -> %u)", (unsigned)from, (unsigned)h.egerr);
    UtAssert_True(stream_socks() == 0, "no socket is left open after the cycle (%d)", stream_socks());
    UtAssert_True(t5_connect_waiting == 0 && t5_connect_again == 0, "connect calls never wait and never repeat on an "
                  "attempt in progress (%d, %d)", t5_connect_waiting, t5_connect_again);
}

static void Test_TcpCycleRestart(void)
{
    conn_t c;
    cs_t cs[8];
    hk_t h;
    trace_t tr;
    int ev0, n;
    if (!boot(true)) return;
    align_second();
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7010, NULL);
    add_conn(4, &c);
    trace_reset(&tr);
    trace_run(&tr, CYCLE_WAKES + 5);
    if (!(get_cs(cs) && UtAssert_True(cs[4].state == ST_DOWN, "set-up: the connection is DOWN after a refused cycle (%u)",
                                      cs[4].state)))
        return;
    ev0 = t5_nev;
    n = t5_n_connect;
    idx_cmd(CC_DISABLE, 4);
    UtAssert_True(t5_count_ev(EID_DISABLE_INF, ev0) == 1, "DISABLE_CONN of a DOWN connection succeeds");
    if (get_cs(cs)) UtAssert_True(cs[4].state == ST_DISABLED && cs[4].en == 0, "after DISABLE_CONN the connection is "
                                  "DISABLED (State %u)", cs[4].state);
    align_second();
    UtAssert_True(t5_n_connect == n, "a disabled connection makes no attempt");
    idx_cmd(CC_ENABLE, 4);
    UtAssert_True(t5_count_ev(EID_ENABLE_INF, ev0) == 1, "ENABLE_CONN from DOWN succeeds with GW_CONN_ENABLE_INF_EID");
    UtAssert_True(t5_n_connect == n + 1, "ENABLE_CONN begins a new connect cycle with its first attempt at once (%d "
                  "connect calls)", t5_n_connect - n);
    if (get_cs(cs)) UtAssert_True(cs[4].state == ST_CONNECTING && cs[4].en == 1, "the new cycle is CONNECTING (State %u)",
                                  cs[4].state);
    trace_reset(&tr);
    trace_run(&tr, 10);
    t5_listen[0] = 7010;
    trace_run(&tr, 50);
    UtAssert_True(near(tr.up_at, 50), "the new cycle's next attempt, 5 s after its first, reaches UP once the peer listens "
                  "(GW_CONN_UP_INF_EID at wakeup %d, expected 50)", tr.up_at);
    if (get_cs(cs)) UtAssert_True(cs[4].state == ST_UP, "the connection is UP (State %u)", cs[4].state);
    if (get_hk(&h)) UtAssert_True(h.up == 1, "ConnectionsUp is 1 (%u)", h.up);
}

static void Test_TcpPeerDrop(void)
{
    conn_t c;
    cs_t cs[8];
    trace_t tr;
    uint8 f[256], wr[512];
    size_t n;
    int from;
    if (!boot(true)) return;
    t5_listen[0] = 7011;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7011, NULL);
    add_conn(3, &c);
    if (!(get_cs(cs) && UtAssert_True(cs[3].state == ST_UP, "a TCP connection to a listening peer is UP at once (State %u)",
                                      cs[3].state)))
        return;
    align_second();
    t5_listen[0] = 0;
    t5_tcp_close_peer(7011);
    trace_reset(&tr);
    trace_run(&tr, 3);
    UtAssert_True(tr.n >= 1 && tr.at[0] <= 2, "when an UP connection drops, a new connect cycle begins with an attempt at once "
                  "(first attempt at wakeup %d after the peer closed)", tr.n >= 1 ? tr.at[0] : -1);
    if (get_cs(cs)) UtAssert_True(cs[3].state == ST_CONNECTING, "the dropped connection is CONNECTING (State %u)", cs[3].state);
    t5_listen[0] = 7011;
    trace_run(&tr, 55);
    UtAssert_True(tr.n >= 2 && near(tr.at[1] - tr.at[0], 50), "the next attempt starts 5 s after the first (%d wakeups)",
                  tr.n >= 2 ? tr.at[1] - tr.at[0] : -1);
    UtAssert_True(tr.up_at > 0 && tr.n >= 2 && tr.up_at == tr.at[1], "that attempt reaches UP with GW_CONN_UP_INF_EID");
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_tcp_write(7011, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 1, "traffic flows after the new cycle reached UP");
}

static void Test_TcpCompletedLater(void)
{
    conn_t c;
    cs_t cs[8];
    trace_t tr;
    uint8 f[256], wr[512], pkt[64], buf[4096];
    size_t n, pn, sn, off[4], len[4];
    int from, n0, cnt;
    bool clean;
    if (!boot(true)) return;
    align_second();
    t5_listen_pending[0] = 7012;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7012, NULL);
    conn_sub(&c, 0, V_E5);
    n0 = t5_n_connect;
    add_conn(2, &c);
    UtAssert_True(t5_n_connect - n0 == 1 && stream_socks() == 1, "an attempt in progress keeps its socket (%d connect calls, "
                  "%d sockets)", t5_n_connect - n0, stream_socks());
    trace_reset(&tr);
    trace_run(&tr, 20);
    UtAssert_True(tr.n == 0 && t5_connect_again == 0, "GW polls an attempt in progress without calling OS_SocketConnect "
                  "again (%d more connect calls, %d on the in-progress socket)", tr.n, t5_connect_again);
    if (get_cs(cs)) UtAssert_True(cs[2].state == ST_CONNECTING, "the connection is CONNECTING while the attempt is in "
                                  "progress (State %u)", cs[2].state);
    t5_tcp_complete(7012, true);
    trace_run(&tr, 2);
    UtAssert_True(tr.up_at == 21, "a socket that becomes writable is UP on the next WAKEUP (GW_CONN_UP_INF_EID at wakeup %d "
                  "after the peer accepted)", tr.up_at < 0 ? -1 : tr.up_at - 20);
    UtAssert_True(tr.n == 0 && t5_connect_again == 0 && t5_connect_waiting == 0, "the completed attempt is not connected "
                  "again (%d connect calls, %d repeated, %d waiting)", tr.n, t5_connect_again, t5_connect_waiting);
    if (get_cs(cs)) UtAssert_True(cs[2].state == ST_UP, "the connection is UP (State %u)", cs[2].state);
    pn = t5_build_verif(pkt, V_E5, 40, 5, 4);
    t5_deliver(pkt, pn);
    sn = t5_stream_out(7012, -1, buf, sizeof(buf));
    cnt = stream_frames(buf, sn, off, len, 4, &clean);
    UtAssert_True(cnt == 1 && clean, "egress flows on the completed connection (%d frames)", cnt);
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_tcp_write(7012, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 1, "ingress flows on the completed connection");
    /* Readable at completion: the data decides the attempt and goes to the deframer. */
    t5_listen_pending[1] = 7013;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7013, NULL);
    add_conn(1, &c);
    wakeup();
    t5_tcp_complete(7013, true);
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_tcp_write(7013, wr, framed(wr, NULL, 0, f, n));
    trace_reset(&tr);
    trace_run(&tr, 2);
    if (get_cs(cs)) UtAssert_True(cs[1].state == ST_UP, "an attempt whose socket is readable with data is UP (State %u)",
                                  cs[1].state);
    UtAssert_True(noops_since(from) == 1, "the data that decided the attempt goes to the stream deframer (%d published)",
                  noops_since(from));
    UtAssert_True(t5_connect_again == 0, "no connect call is repeated (%d)", t5_connect_again);
}

static void Test_TcpAttemptTimeout(void)
{
    conn_t c;
    cs_t cs[8];
    trace_t tr;
    if (!boot(true)) return;
    align_second();
    t5_listen_pending[0] = 7014;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7014, NULL);
    add_conn(2, &c);
    trace_reset(&tr);
    trace_run(&tr, 45);
    UtAssert_True(tr.n == 0 && stream_socks() == 1, "an undecided attempt stays in progress before 5 s (%d new attempts, %d "
                  "sockets)", tr.n, stream_socks());
    trace_run(&tr, 10);
    UtAssert_True(tr.n == 1 && near(tr.at[0], 50), "an attempt still undecided GW_TCP_ATTEMPT_SEC (5) seconds after it "
                  "started fails and the next attempt starts (at wakeup %d, expected 50)", tr.n >= 1 ? tr.at[0] : -1);
    UtAssert_True(stream_socks() == 1, "the failed attempt's socket is closed (%d stream sockets open, expected the new "
                  "attempt's only)", stream_socks());
    trace_run(&tr, 15);
    t5_tcp_complete(7014, false);
    trace_run(&tr, 2);
    UtAssert_True(stream_socks() == 0, "an attempt that ends in failure (readable, the read reports an error) closes its "
                  "socket (%d open)", stream_socks());
    if (get_cs(cs)) UtAssert_True(cs[2].state == ST_CONNECTING, "the cycle goes on CONNECTING (State %u)", cs[2].state);
    trace_run(&tr, CYCLE_WAKES + 5 - tr.w);
    UtAssert_True(tr.n == 3 && near(tr.at[1], 100) && near(tr.at[2], 150), "the next attempts still start 5 s apart from "
                  "the previous start, not earlier after an early failure (at %d and %d, expected 100 and 150)",
                  tr.n >= 2 ? tr.at[1] : -1, tr.n >= 3 ? tr.at[2] : -1);
    UtAssert_True(near(tr.down_at, CYCLE_WAKES), "a cycle of undecided attempts ends in DOWN at 20 s (wakeup %d)",
                  tr.down_at);
    UtAssert_True(stream_socks() == 0 && t5_connect_again == 0, "no socket is left open and no connect call is repeated "
                  "(%d, %d)", stream_socks(), t5_connect_again);
    if (get_cs(cs)) UtAssert_True(cs[2].state == ST_DOWN, "the connection is DOWN (State %u)", cs[2].state);
}

static void Test_Tcp(void)
{
    conn_t c;
    hk_t a, h;
    trace_t tr;
    uint8 f[256], pkt[64], buf[4096], wr[512];
    size_t n, pn, sn, off[8], len[8], before, wn;
    int from, ev0, cnt;
    bool clean;
    if (!boot(true)) return;
    align_second();
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7001, NULL);
    conn_sub(&c, 0, V_E5);
    add_conn(2, &c);
    trace_reset(&tr);
    trace_run(&tr, 10);
    t5_listen[0] = 7001;
    trace_run(&tr, 45);
    UtAssert_True(near(tr.up_at, 50), "a TCP connection reaches UP at its next attempt once the listener appears "
                  "(GW_CONN_UP_INF_EID at wakeup %d, expected 50)", tr.up_at);
    if (get_hk(&h)) UtAssert_True(h.up == 1, "ConnectionsUp is 1 (%u)", h.up);
    /* Egress: marker then frame. */
    before = t5_stream_out(7001, -1, buf, sizeof(buf));
    pn = t5_build_verif(pkt, V_E5, 40, 2, 4);
    t5_deliver(pkt, pn);
    sn = t5_stream_out(7001, -1, buf, sizeof(buf));
    cnt = stream_frames(buf + before, sn - before, off, len, 8, &clean);
    UtAssert_True(cnt == 1 && clean, "egress on TCP is the marker 1A CF FC 1D followed by one frame (%d frames, %s)", cnt,
                  clean ? "nothing else" : "other bytes present");
    if (cnt == 1) check_egress(buf + before + off[0], len[0], KEY0, pkt, pn, 1, 1, t5_now_sec, "TCP egress");
    /* Ingress: a whole frame. */
    from = t5_ntx;
    n = noop_frame(f, KEY0, 1, 0);
    t5_tcp_write(7001, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 1, "a marker-framed frame on TCP is validated and published (%d)", noops_since(from));
    /* Split across wakeups. */
    n = noop_frame(f, KEY0, 1, 0);
    wn = framed(wr, NULL, 0, f, n);
    t5_tcp_write(7001, wr, 21);
    wakeup();
    UtAssert_True(noops_since(from) == 1, "a partial frame is kept, not published yet");
    t5_tcp_write(7001, wr + 21, wn - 21);
    wakeup();
    UtAssert_True(noops_since(from) == 2, "a frame split across wakeups is reassembled and published");
    /* Garbage before a marker. */
    n = noop_frame(f, KEY0, 1, 0);
    t5_tcp_write(7001, wr, framed(wr, (const uint8 *)"\x00\x55\xAA\x1A\xCF", 5, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 3, "bytes before a marker are skipped while hunting");
    /* Out-of-range length, then a good frame right after. */
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    n = noop_frame(f, KEY0, 1, 0);
    memcpy(wr, MARKER, 4);
    memcpy(wr + 4, f, 16);
    t5_wbe16(wr + 4 + 6, 2000);
    wn = 20 + framed(wr + 20, NULL, 0, f, n);
    t5_tcp_write(7001, wr, wn);
    wakeup();
    if (get_hk(&h)) UtAssert_True(h.fmt == a.fmt + 1, "a header with L outside 8..1024 is a FORMAT rejection (%u of 1)",
                                  (unsigned)(h.fmt - a.fmt));
    UtAssert_True(noops_since(from) == 4, "hunting resumes after the rejected header's marker and the next frame is "
                  "published (%d of 4)", noops_since(from));
    UtAssert_True(reject_ev(ev0, "FORMAT", 2, -1), "the length rejection sends GW_FRAME_REJECT_ERR_EID naming FORMAT and "
                  "connection 2");
    /* Bad MAC. */
    if (!get_hk(&a)) return;
    n = noop_frame(f, KEY0, 1, 0);
    bad_mac(f, n);
    t5_tcp_write(7001, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 4, "a frame with a bad MAC on TCP is not published");
    if (get_hk(&h)) UtAssert_True(h.hmac == a.hmac + 1, "it counts under HmacErrors (%u of 1)", (unsigned)(h.hmac - a.hmac));
}

static void Test_TcpReadFailure(void)
{
    conn_t c;
    hk_t a, h;
    int ev0, n;
    if (!boot(true)) return;
    t5_listen[0] = 7002;
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7002, NULL);
    add_conn(3, &c);
    if (!wait_state(3, ST_UP, 3))
    {
        UtAssert_Failed("the TCP connection to a listening peer does not reach UP");
        return;
    }
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    n = t5_n_connect;
    fail_stream_read = true;
    t5_tcp_write(7002, "\x01", 1);
    wakeup();
    fail_stream_read = false;
    if (get_hk(&h)) UtAssert_True(h.ingerr == a.ingerr + 1, "a read failure is an ingress error (IngressErrors %u -> %u)",
                                  (unsigned)a.ingerr, (unsigned)h.ingerr);
    UtAssert_True(t5_count_ev(EID_INGRESS_ERR, ev0) == 1, "a read failure sends GW_INGRESS_ERR_EID (23) (%d)",
                  t5_count_ev(EID_INGRESS_ERR, ev0));
    wakeup();
    UtAssert_True(t5_n_connect > n, "the I/O error begins a new connect cycle with an attempt at once (%d connect calls "
                  "within 2 wakeups)", t5_n_connect - n);
    UtAssert_True(wait_state(3, ST_UP, 3), "with the peer still listening that attempt reaches UP");
}

static void Test_Serial(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    uint8 f[256], wr[512];
    size_t n, wn;
    int dev, from, ev0;
    if (!boot(true)) return;
    dev = t5_dev_create("/dev/pts/7");
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/7");
    conn_sub(&c, 0, V_E6);
    ev0 = t5_nev;
    add_conn(1, &c);
    UtAssert_True(t5_count_ev(EID_ADD_INF, ev0) == 1, "a SERIAL connection on an existing PTY slave is added and opened");
    if (get_cs(cs)) UtAssert_True(cs[1].state == ST_UP && cs[1].iface == IF_SERIAL, "the SERIAL connection is UP (State %u)",
                                  cs[1].state);
    serial_traffic(dev, V_E6, 1, "SERIAL");
    from = t5_ntx;
    /* Split across writes and wakeups. */
    n = noop_frame(f, KEY0, 1, 0);
    wn = framed(wr, NULL, 0, f, n);
    t5_dev_write(dev, wr, 10);
    wakeup();
    t5_dev_write(dev, wr + 10, 20);
    wakeup();
    UtAssert_True(noops_since(from) == 0, "a partial frame on SERIAL is kept across wakeups");
    t5_dev_write(dev, wr + 30, wn - 30);
    wakeup();
    UtAssert_True(noops_since(from) == 1, "a frame split across writes on SERIAL is published (%d)", noops_since(from));
    /* Garbage before the marker. */
    n = noop_frame(f, KEY0, 1, 0);
    t5_dev_write(dev, wr, framed(wr, (const uint8 *)"garbage", 7, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 2, "garbage before a marker on SERIAL is skipped");
    /* Bad MAC. */
    if (!get_hk(&a)) return;
    n = noop_frame(f, KEY0, 1, 0);
    bad_mac(f, n);
    t5_dev_write(dev, wr, framed(wr, NULL, 0, f, n));
    wakeup();
    UtAssert_True(noops_since(from) == 2, "a frame with a bad MAC on SERIAL is not published");
    if (get_hk(&h)) UtAssert_True(h.hmac == a.hmac + 1, "it counts under HmacErrors (%u of 1)", (unsigned)(h.hmac - a.hmac));
    if (get_cs(cs)) UtAssert_True(cs[1].rx == 3 && cs[1].rxerr == 1, "RxPackets %u (expected 3) and RxErrors %u (expected 1) "
                                  "on the SERIAL connection", (unsigned)cs[1].rx, (unsigned)cs[1].rxerr);
}

static void Test_SerialIdle(void)
{
    conn_t c;
    int i, from, hk_count;
    if (!boot(true)) return;
    t5_dev_create("/dev/pts/8");
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/8");
    add_conn(0, &c);
    from = t5_ntx;
    hk_count = 0;
    for (i = 0; i < 30; i++)
    {
        wakeup();
        if (i % 5 == 4)
        {
            int before = t5_tx_count(GW_HK_MID, from);
            t5_send_cmd(GW_SEND_HK_MID, 0, NULL, 0);
            hk_count += t5_tx_count(GW_HK_MID, from) > before;
        }
    }
    UtAssert_True(hk_count == 6, "GW answers every SEND_HK while its PTY has nothing pending (%d of 6)", hk_count);
    UtAssert_True(t5_tx_count(SFI_HB_MID, from) == 3, "GW keeps its heartbeat once per second (10 wakeups) with an idle PTY "
                  "(%d heartbeats in 30 wakeups)", t5_tx_count(SFI_HB_MID, from));
    UtAssert_True(t5_dev_blocking_reads == 0, "reads of the PTY never block GW's main loop (%d reads with no byte pending "
                  "would have waited)", t5_dev_blocking_reads);
    UtAssert_True(t5_dev_waiting_selects == 0, "a WAKEUP with nothing pending costs no more than a poll (%d selects with a "
                  "nonzero timeout)", t5_dev_waiting_selects);
}

static void Test_Restart(void)
{
    conn_t c;
    int dev7, dev9;
    if (!boot(true)) return;
    dev7 = t5_dev_create("/dev/pts/7");
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/7");
    conn_sub(&c, 0, V_E6);
    add_conn(0, &c);
    serial_traffic(dev7, V_E6, 1, "before the restart");
    if (!restart_app()) return;
    UtAssert_True(!t5_exit_called, "after ES STOP_APP and START_APP GW initializes again without an error exit "
                  "(the /dev/pts mapping and the PTY remain from the stopped instance)");
    if (t5_exit_called) return;
    UtAssert_True(t5_count_ev(EID_INIT, 0) == 2, "the restarted GW sends GW_INIT_INF_EID again (%d of 2)",
                  t5_count_ev(EID_INIT, 0));
    wakeups(2);
    dev9 = t5_dev_create("/dev/pts/9");
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/9");
    conn_sub(&c, 0, V_E6);
    add_conn(0, &c);
    serial_traffic(dev9, V_E6, 1, "after the restart, on a new PTY");
}

static void Test_ConnCommands(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    uint8 pkt[64];
    size_t pn;
    int ev0, from, idx[4];
    if (!boot(true)) return;
    udp_conn(&c, 1, 6100, 7100, V_E4);
    add_conn(0, &c);
    udp_conn(&c, 0, 0, 7101, V_E4);
    add_conn(1, &c);
    pn = t5_build_verif(pkt, V_E4, 40, 3, 4);
    t5_deliver(pkt, pn);
    t5_deliver(pkt, pn);
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    idx_cmd(CC_DISABLE, 0);                                         /* ok */
    UtAssert_True(t5_count_ev(EID_DISABLE_INF, ev0) == 1, "DISABLE_CONN of an enabled slot sends GW_CONN_DISABLE_INF_EID (17)");
    if (get_cs(cs)) UtAssert_True(cs[0].en == 0 && cs[0].state == ST_DISABLED, "a disabled connection reports Enabled 0, "
                                  "State DISABLED (%u, %u)", cs[0].en, cs[0].state); /* ok */
    from = t5_nsent;
    t5_deliver(pkt, pn);
    UtAssert_True(udp_out(7100, from, idx, 4) == 0, "a disabled connection egresses nothing");
    idx_cmd(CC_DISABLE, 0);                                         /* error */
    idx_cmd(CC_DISABLE, 7);                                         /* error */
    UtAssert_True(t5_count_ev(EID_DISABLE_ERR, ev0) == 2, "DISABLE_CONN of a disabled or unused slot sends "
                  "GW_CONN_DISABLE_ERR_EID (18) (%d of 2)", t5_count_ev(EID_DISABLE_ERR, ev0));
    idx_cmd(CC_ENABLE, 0);                                          /* ok */
    UtAssert_True(t5_count_ev(EID_ENABLE_INF, ev0) == 1, "ENABLE_CONN of a disabled slot sends GW_CONN_ENABLE_INF_EID (15)");
    if (get_cs(cs)) UtAssert_True(cs[0].en == 1 && cs[0].state == ST_UP, "the re-enabled connection is UP (%u, %u)",
                                  cs[0].en, cs[0].state); /* ok */
    from = t5_nsent;
    t5_deliver(pkt, pn);
    if (UtAssert_True(udp_out(7100, from, idx, 4) == 1, "the re-enabled connection egresses again"))
        UtAssert_True(t5_be32(t5_sent[idx[0]].bytes + 12) == 1, "Sequence starts at 1 again after the connection is opened "
                      "again (%u)", (unsigned)t5_be32(t5_sent[idx[0]].bytes + 12));
    idx_cmd(CC_ENABLE, 0);                                          /* error */
    idx_cmd(CC_ENABLE, 6);                                          /* error */
    idx_cmd(CC_ENABLE, 9);                                          /* error */
    UtAssert_True(t5_count_ev(EID_ENABLE_ERR, ev0) == 3, "ENABLE_CONN of an enabled, unused or out-of-range slot sends "
                  "GW_CONN_ENABLE_ERR_EID (16) (%d of 3)", t5_count_ev(EID_ENABLE_ERR, ev0));
    udp_conn(&c, 0, 6200, 7200, 0);
    add_conn(2, &c);                                                /* ok */
    t5_bind_fail_port = 6200;
    idx_cmd(CC_ENABLE, 2);                                          /* error */
    t5_bind_fail_port = 0;
    UtAssert_True(t5_count_ev(EID_ENABLE_ERR, ev0) == 4, "ENABLE_CONN whose opening fails sends GW_CONN_ENABLE_ERR_EID");
    if (get_cs(cs)) UtAssert_True(cs[2].en == 0, "a connection whose opening failed stays disabled"); /* ok */
    idx_cmd(CC_REMOVE, 1);                                          /* ok */
    UtAssert_True(t5_count_ev(EID_REMOVE_INF, ev0) == 1, "REMOVE_CONN of a used slot sends GW_CONN_REMOVE_INF_EID (13)");
    if (get_cs(cs)) UtAssert_True(cs[1].iface == 0, "a removed slot is unused"); /* ok */
    idx_cmd(CC_REMOVE, 1);                                          /* error */
    idx_cmd(CC_REMOVE, 8);                                          /* error */
    UtAssert_True(t5_count_ev(EID_REMOVE_ERR, ev0) == 2, "REMOVE_CONN of an unused or out-of-range slot sends "
                  "GW_CONN_REMOVE_ERR_EID (14) (%d of 2)", t5_count_ev(EID_REMOVE_ERR, ev0));
    simple_cmd(CC_RELOAD);                                          /* ok */
    UtAssert_True(t5_count_ev(EID_RELOAD_INF, ev0) == 1, "RELOAD sends GW_RELOAD_INF_EID (19)");
    if (get_cs(cs)) UtAssert_True(cs[0].iface == 0 && cs[2].iface == 0, "RELOAD re-applies the active table (every slot "
                                  "unused), discarding the connections added by command"); /* ok */
    if (get_hk(&h))
    {
        UtAssert_True(h.cfg == 0 && h.subs == 0, "after RELOAD ConnectionsConfigured %u and Subscriptions %u are 0", h.cfg,
                      h.subs);
        UtAssert_True((uint8)(h.cc - a.cc) == 10, "accepted commands count under CommandCounter (%u of 10: DISABLE, "
                      "ENABLE, ADD_CONN, REMOVE, RELOAD and five SEND_CONN_STATUS)", (unsigned)(uint8)(h.cc - a.cc));
        UtAssert_True((uint8)(h.cec - a.cec) == 8, "rejected commands count under CommandErrorCounter (%u of 8)",
                      (unsigned)(uint8)(h.cec - a.cec));
    }
    UtAssert_True(t5_subscribed(V_E4) == 0, "RELOAD unsubscribes the MIDs of the discarded connections");
}

static void Test_TableUpdate(void)
{
    conn_t c;
    cs_t cs[8];
    uint8 img[TBL_SIZE], pkt[64];
    size_t pn;
    int from, idx[4];
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7303, V_E4);
    add_conn(3, &c);
    memset(img, 0, sizeof(img));
    udp_conn(&c, 1, 6300, 7300, V_E7);
    tbl_put(img, 0, &c);
    if (!UtAssert_True(t5_tbl_submit("ConnTable", img) == CFE_SUCCESS, "a valid ConnTable update passes validation")) return;
    wakeups(3);
    t5_send_cmd(GW_SEND_HK_MID, 0, NULL, 0);
    wakeups(2);
    if (get_cs(cs))
    {
        UtAssert_True(cs[0].iface == IF_UDP && cs[0].state == ST_UP, "a table update opens the table's connections (slot 0 "
                      "Interface %u, State %u)", cs[0].iface, cs[0].state);
        UtAssert_True(cs[3].iface == 0, "a table update discards connections added by command (slot 3 Interface %u)",
                      cs[3].iface);
    }
    UtAssert_True(t5_sock_by_port(6300) >= 0, "the updated table's UDP connection binds its LocalPort");
    from = t5_nsent;
    pn = t5_build_verif(pkt, V_E7, 40, 1, 4);
    t5_deliver(pkt, pn);
    UtAssert_True(udp_out(7300, from, idx, 4) == 1, "the updated table's connection egresses its Subs");
    UtAssert_True(t5_subscribed(V_E4) == 0, "the discarded connection's Subs are unsubscribed");
    udp_conn(&c, 1, 0, 7304, 0);
    add_conn(4, &c);
    simple_cmd(CC_RELOAD);
    if (get_cs(cs)) UtAssert_True(cs[0].iface == IF_UDP && cs[4].iface == 0, "RELOAD re-applies the updated table");
}

static void Test_OpenFailures(void)
{
    conn_t c;
    if (!boot(true)) return;
    t5_bind_fail_port = 6500;
    udp_conn(&c, 1, 6500, 7500, 0);
    add_rejected(0, &c, "enabled, whose bind fails");
    t5_bind_fail_port = 0;
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/99");
    add_rejected(0, &c, "enabled SERIAL on a device that cannot be opened");
}

static void Test_StatusAndHk(void)
{
    conn_t c;
    cs_t cs[8];
    hk_t h;
    if (!boot(true)) return;
    udp_conn(&c, 1, 0, 7000, V_E4);
    conn_sub(&c, 1, V_E5);
    conn_sub(&c, 9, V_E6);
    add_conn(0, &c);
    conn_set(&c, IF_TCP, 0, 0, "127.0.0.1", 7001, NULL);
    add_conn(4, &c);
    t5_dev_create("/dev/pts/5");
    conn_set(&c, IF_SERIAL, 1, 0, NULL, 0, "/dev/pts/5");
    add_conn(7, &c);
    if (get_cs(cs))
    {
        UtAssert_True(cs[0].iface == IF_UDP && cs[0].en == 1 && cs[0].state == ST_UP && cs[0].subs == 3,
                      "slot 0: Interface UDP, Enabled 1, State UP, SubCount 3 (%u, %u, %u, %u)", cs[0].iface, cs[0].en,
                      cs[0].state, cs[0].subs);
        UtAssert_True(cs[4].iface == IF_TCP && cs[4].en == 0 && cs[4].state == ST_DISABLED && cs[4].subs == 0,
                      "slot 4: Interface TCP, Enabled 0, State DISABLED (%u, %u, %u)", cs[4].iface, cs[4].en, cs[4].state);
        UtAssert_True(cs[7].iface == IF_SERIAL && cs[7].state == ST_UP, "slot 7: Interface SERIAL, State UP (%u, %u)",
                      cs[7].iface, cs[7].state);
        UtAssert_True(cs[1].iface == 0 && cs[1].state == 0, "unused slots report Interface 0");
    }
    if (get_hk(&h))
    {
        UtAssert_True(h.cfg == 3 && h.up == 2, "ConnectionsConfigured %u (expected 3) and ConnectionsUp %u (expected 2)",
                      h.cfg, h.up);
        UtAssert_True(h.subs == 3, "Subscriptions %u (expected 3)", h.subs);
        UtAssert_True(h.shared == 1, "ValTableShared 1");
    }
}

static void Test_ResetCounters(void)
{
    conn_t c;
    hk_t a, h;
    cs_t cs[8];
    uint8 f[128], pkt[1200];
    size_t n;
    int ev0;
    if (!boot(true)) return;
    udp_conn(&c, 1, 6005, 7005, V_E4);
    add_conn(5, &c);
    n = t5_build_verif(pkt, V_E4, 40, 1, 4);
    t5_deliver(pkt, n);
    n = t5_build_verif(pkt, V_E4, 1100, 1, 4);
    t5_deliver(pkt, n);
    n = noop_frame(f, KEY0, 1, 0);
    t5_udp_inject(6005, f, n);
    n = noop_frame(f, KEY0, 1, 0);
    f[n - 1] ^= 1;
    t5_udp_inject(6005, f, n);
    t5_udp_inject(6005, f, 8);
    n = noop_frame(f, KEY0, 9, 0);
    t5_udp_inject(6005, f, n);
    wakeup();
    simple_cmd(CC_NOOP);
    simple_cmd(99);
    if (!get_hk(&a)) return;
    UtAssert_True(a.egr && a.egerr && a.ingr && a.frames && a.crc && a.fmt && a.src && a.cc && a.cec,
                  "set-up: counters are nonzero before RESET_COUNTERS");
    ev0 = t5_nev;
    simple_cmd(CC_RESET);
    UtAssert_True(t5_count_ev(EID_RESET, ev0) == 1, "RESET_COUNTERS sends GW_RESET_INF_EID (4)");
    if (get_hk(&h))
    {
        UtAssert_True(h.cc == 0 && h.cec == 0, "RESET_COUNTERS zeroes the command counters (%u, %u)", h.cc, h.cec);
        UtAssert_True(h.egr == 0 && h.ingr == 0 && h.egerr == 0 && h.ingerr == 0 && h.loop == 0,
                      "RESET_COUNTERS zeroes PacketsEgressed, PacketsIngressed, EgressErrors, IngressErrors and LoopDrops");
        UtAssert_True(h.frames == 0 && h.fmt == 0 && h.crc == 0 && h.hmac == 0 && h.src == 0 && h.age == 0,
                      "RESET_COUNTERS zeroes FramesReceived and the five reason counters");
        UtAssert_True(h.cfg == a.cfg && h.up == a.up && h.subs == a.subs && h.shared == a.shared,
                      "RESET_COUNTERS leaves ConnectionsConfigured, ConnectionsUp, Subscriptions and ValTableShared");
    }
    if (get_cs(cs))
        UtAssert_True(cs[5].tx == 0 && cs[5].rx == 0 && cs[5].txerr == 0 && cs[5].rxerr == 0 && cs[5].iface == IF_UDP,
                      "RESET_COUNTERS zeroes the per-connection counters (%u, %u, %u, %u)", (unsigned)cs[5].tx,
                      (unsigned)cs[5].rx, (unsigned)cs[5].txerr, (unsigned)cs[5].rxerr);
}

static void Test_CommandErrors(void)
{
    hk_t a, h;
    uint8 junk[4] = {0, 0, 0, 0}, big[200], msg[16];
    size_t n;
    int ev0, p, k, slot;
    if (!boot(true)) return;
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    simple_cmd(99);
    UtAssert_True(t5_count_ev(EID_CC_ERR, ev0) == 1, "an unknown function code sends GW_CC_ERR_EID (2)");
    t5_send_cmd(GW_CMD_MID, CC_NOOP, junk, sizeof(junk));
    memset(big, 0, sizeof(big));
    t5_send_cmd(GW_CMD_MID, CC_ADD, big, 100);
    t5_send_cmd(GW_CMD_MID, CC_REMOVE, big, 8);
    UtAssert_True(t5_count_ev(EID_LEN_ERR, ev0) == 3, "a wrong command length sends GW_CMD_LEN_ERR_EID (6) (%d of 3)",
                  t5_count_ev(EID_LEN_ERR, ev0));
    /* An unexpected message ID arriving on GW's command pipe. */
    p = -1;
    for (k = 0; k < T5_SUBS_MAX; k++)
        if (t5_subs[k].used && t5_subs[k].mid == GW_CMD_MID) p = t5_subs[k].pipe;
    if (UtAssert_True(p >= 0, "GW has a pipe subscribed to GW_CMD_MID"))
    {
        n = t5_build_cmd(msg, 0x18F9, 0, NULL, 0);
        slot = t5_pool_next++ % T5_POOL;
        memcpy(t5_pool[slot], msg, n);
        t5_pipes[p].q[(t5_pipes[p].head + t5_pipes[p].count) % T5_PIPE_Q] = slot;
        t5_pipes[p].count++;
        t5_kick();
        UtAssert_True(t5_count_ev(EID_MID_ERR, ev0) == 1, "an unexpected message ID sends GW_MID_ERR_EID (5)");
    }
    if (get_hk(&h)) UtAssert_True((uint8)(h.cec - a.cec) == 5 && h.cc == a.cc, "each rejected command counts under "
                                  "CommandErrorCounter and nothing else (%u of 5)", (unsigned)(uint8)(h.cec - a.cec));
}

static void Test_Heartbeat(void)
{
    int i, from, prev = 0, at[8], n = 0;
    const t5_tx_t *t;
    if (!boot(true)) return;
    from = t5_ntx;
    for (i = 0; i < 30; i++)
    {
        int before = t5_tx_count(SFI_HB_MID, from);
        wakeup();
        if (t5_tx_count(SFI_HB_MID, from) > before && n < 8) at[n++] = i;
    }
    UtAssert_True(n == 3, "GW sends one heartbeat per 10 WAKEUPs (%d in 30)", n);
    if (n >= 2) UtAssert_True(at[1] - at[0] == 10 && (n < 3 || at[2] - at[1] == 10), "heartbeats are every 10th WAKEUP "
                              "(at wakeups %d and %d)", at[0], at[1]);
    for (i = from; i < t5_ntx; i++)
    {
        t = &t5_tx[i];
        if (t->mid != SFI_HB_MID) continue;
        UtAssert_True(t->size == 32, "SFI_HeartbeatCmd_t is 32 bytes (%u)", (unsigned)t->size);
        UtAssert_True(t5_msg_fcn(t->bytes) == 0, "the heartbeat has function code 0");
        UtAssert_True(strncmp((const char *)t->bytes + 8, "GW", 20) == 0, "the heartbeat names GW (%.20s)",
                      (const char *)t->bytes + 8);
        UtAssert_True(t5_get32(t->bytes + 28) == (uint32)(prev + 1), "Sequence is 1 for the first heartbeat and +1 per "
                      "heartbeat (%u after %d)", (unsigned)t5_get32(t->bytes + 28), prev);
        prev = (int)t5_get32(t->bytes + 28);
    }
}

void UtTest_Setup(void)
{
    UtTest_Add(Test_Startup, Setup, Teardown, "Startup and housekeeping");
    UtTest_Add(Test_TableShare, Setup, Teardown, "Validation table share and retry");
    UtTest_Add(Test_UnsharedHoldsTraffic, Setup, Teardown, "No traffic without the shared table");
    UtTest_Add(Test_KeyChange, Setup, Teardown, "Key change of the shared table");
    UtTest_Add(Test_DefinitionValidation, Setup, Teardown, "Connection definition validation");
    UtTest_Add(Test_TableValidation, Setup, Teardown, "Connection table validation");
    UtTest_Add(Test_EgressFraming, Setup, Teardown, "Egress framing");
    UtTest_Add(Test_EgressNodeId, Setup, Teardown, "Egress source is the node ID");
    UtTest_Add(Test_EgressErrors, Setup, Teardown, "Egress errors");
    UtTest_Add(Test_Subscriptions, Setup, Teardown, "Subscription bookkeeping");
    UtTest_Add(Test_UdpIngress, Setup, Teardown, "UDP ingress and rejection reasons");
    UtTest_Add(Test_IngressBatch, Setup, Teardown, "Ingress batch per wakeup");
    UtTest_Add(Test_LoopRule, Setup, Teardown, "Loop rule");
    UtTest_Add(Test_Tcp, Setup, Teardown, "TCP connection and stream framing");
    UtTest_Add(Test_TcpReadFailure, Setup, Teardown, "TCP read failure");
    UtTest_Add(Test_TcpNotListening, Setup, Teardown, "TCP peer not listening is no failure");
    UtTest_Add(Test_TcpCycleDown, Setup, Teardown, "TCP connect cycle ends in DOWN");
    UtTest_Add(Test_TcpCycleRestart, Setup, Teardown, "TCP cycle restarted from DOWN");
    UtTest_Add(Test_TcpPeerDrop, Setup, Teardown, "TCP peer drop begins a new cycle");
    UtTest_Add(Test_TcpCompletedLater, Setup, Teardown, "TCP attempt completed later");
    UtTest_Add(Test_TcpAttemptTimeout, Setup, Teardown, "TCP attempt undecided after 5 s");
    UtTest_Add(Test_Serial, Setup, Teardown, "Serial connection and stream framing");
    UtTest_Add(Test_SerialIdle, Setup, Teardown, "Serial reads never block");
    UtTest_Add(Test_Restart, Setup, Teardown, "Restart with an existing serial mapping");
    UtTest_Add(Test_ConnCommands, Setup, Teardown, "Connection commands");
    UtTest_Add(Test_TableUpdate, Setup, Teardown, "Connection table update");
    UtTest_Add(Test_OpenFailures, Setup, Teardown, "Open failures");
    UtTest_Add(Test_StatusAndHk, Setup, Teardown, "Connection status and housekeeping");
    UtTest_Add(Test_ResetCounters, Setup, Teardown, "Reset counters");
    UtTest_Add(Test_CommandErrors, Setup, Teardown, "Command errors");
    UtTest_Add(Test_Heartbeat, Setup, Teardown, "Heartbeat");
}
