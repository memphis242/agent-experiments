/*
 * Black-box behaviour test of the CMDI command ingest application (spec S3.3, S3.4, C.4, C.5).
 *
 * CMDI_AppMain runs on its own thread against the behaviour model in t5_model.h. The test drives it only
 * with software bus messages on the pinned MIDs, a ValTable image, and datagrams on the UDP port CMDI
 * binds; it observes housekeeping, events, the socket model and every message CMDI transmits. The
 * cmdval_lib API is the model's reference implementation (T5_MODEL_CMDVAL), so CMDI is tested apart
 * from the library; real frames are built with the default table key.
 */
#define T5_MODEL_CMDVAL
#include "t5_model.h"

extern void CMDI_AppMain(void);

/* ------------------------------------------------------------------------- pinned identifiers */

#define CMDI_CMD_MID     0x18D0u
#define CMDI_SEND_HK_MID 0x18D1u
#define CMDI_WAKEUP_MID  0x18D2u
#define CMDI_HK_MID      0x08D0u
#define SFI_HB_MID       0x18DFu
#define SAMPLE_CMD_MID   0x1882u
#define VERIF_MID        0x18EAu
#define UNKNOWN_MID      0x18EFu

#define CC_NOOP          0
#define CC_RESET         1
#define CC_SET_MAX_AGE   2
#define CC_SET_MASK      3

#define EID_INIT         1
#define EID_CC_ERR       2
#define EID_NOOP         3
#define EID_RESET        4
#define EID_MID_ERR      5
#define EID_LEN_ERR      6
#define EID_PIPE_ERR     7
#define EID_TBLREG       8
#define EID_TBLLOAD      9
#define EID_TBLVAL       10
#define EID_SOCKET_ERR   11
#define EID_REJECT       12
#define EID_ACCEPT       13
#define EID_PARAM_INF    14
#define EID_PARAM_ERR    15

#define TBL_SIZE         56
#define HK_PAYLOAD       36
#define TLM_HDR          16
#define CMD_HDR          8
#define HB_SIZE          32

static const uint8 KEY[32] = "cFS-cmdval-test-key-0123456789ab";
static const uint8 KEY2[32] = "another-32-byte-key-for-cmdi-ut!";

/* ------------------------------------------------------------------------- table images (S3.3) */

typedef struct { uint8 b[TBL_SIZE]; } vt_t;

static void vt_make(vt_t *t, const uint8 *key, const uint16 *src, int nsrc, uint32 max_age, uint8 mask)
{
    int i;
    memset(t, 0, sizeof(*t));
    memcpy(t->b, key, 32);
    for (i = 0; i < nsrc && i < 8; i++) t5_put16(t->b + 32 + 2 * i, src[i]);
    t5_put32(t->b + 48, max_age);
    t->b[52] = mask;
}

static void vt_default(vt_t *t)
{
    static const uint16 src[2] = {1, 2};
    vt_make(t, KEY, src, 2, 30, 0x0F);
}

/* ------------------------------------------------------------------------- transmissions with IsOrigination */

static bool tx_orig[T5_TX_MAX];

static void H_Tx(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    if (t5_ntx < T5_TX_MAX) tx_orig[t5_ntx] = UT_Hook_GetArgValueByName(c, "IsOrigination", bool);
    H_SB_TransmitMsg(ud, key, c);
}

static void H_TxBuf(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    if (t5_ntx < T5_TX_MAX) tx_orig[t5_ntx] = UT_Hook_GetArgValueByName(c, "IsOrigination", bool);
    H_SB_TransmitBuffer(ud, key, c);
}

/* ------------------------------------------------------------------------- fixture */

static uint32 seqno;

static void Setup(void)
{
    t5_setup();
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg), H_Tx, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitBuffer), H_TxBuf, NULL);
    memset(tx_orig, 0, sizeof(tx_orig));
    t5_app_entry = CMDI_AppMain;
    seqno = 0;
}

static void Teardown(void) { t5_teardown(); }

static bool boot(const vt_t *t)
{
    t5_tbl_set_file("ValTable", t->b, TBL_SIZE);
    return t5_app_start();
}

static bool boot_default(void)
{
    vt_t t;
    vt_default(&t);
    return boot(&t);
}

static uint16 cmdi_port(void) { return (uint16)(1234 + t5_node_id - 1); }

static bool wakeup(void) { return t5_send_cmd(CMDI_WAKEUP_MID, 0, NULL, 0); }

static bool cmd(int cc, const void *payload, size_t plen) { return t5_send_cmd(CMDI_CMD_MID, cc, payload, plen); }

static bool cmd_max_age(uint32 secs)
{
    uint8 p[4];
    t5_put32(p, secs);
    return cmd(CC_SET_MAX_AGE, p, 4);
}

static bool cmd_mask(uint8 mask)
{
    uint8 p[4] = {0, 0, 0, 0};
    p[0] = mask;
    return cmd(CC_SET_MASK, p, 4);
}

typedef struct
{
    uint8  cc, cec, mask;
    uint32 rx, acc, fmt, crc, hmac, src, age, max_age;
} hk_t;

static bool get_hk(hk_t *h)
{
    const t5_tx_t *t;
    const uint8 *p;
    int before = t5_ntx;
    memset(h, 0, sizeof(*h));
    if (!t5_send_cmd(CMDI_SEND_HK_MID, 0, NULL, 0)) return false;
    t = t5_last_tx(CMDI_HK_MID, before);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_HK did not transmit CMDI housekeeping on CMDI_HK_TLM_MID (0x08D0)");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + HK_PAYLOAD, "CMDI housekeeping packet is %u bytes, the specified payload makes %u",
                  (unsigned)t->size, (unsigned)(TLM_HDR + HK_PAYLOAD));
    p = t->bytes + TLM_HDR;
    h->cc = p[0];
    h->cec = p[1];
    h->mask = p[2];
    h->rx = t5_get32(p + 4);
    h->acc = t5_get32(p + 8);
    h->fmt = t5_get32(p + 12);
    h->crc = t5_get32(p + 16);
    h->hmac = t5_get32(p + 20);
    h->src = t5_get32(p + 24);
    h->age = t5_get32(p + 28);
    h->max_age = t5_get32(p + 32);
    return true;
}

/* ------------------------------------------------------------------------- frames */

typedef struct { uint8 f[1200]; size_t n; uint8 cmd[1100]; size_t clen; } frame_t;

/* A frame around a command of `mid`/`cc` with `plen` payload bytes; time = onboard seconds + `age`. */
static void mkframe(frame_t *fr, uint32 mid, int cc, const void *payload, size_t plen, uint16 src, int32 age,
                    const uint8 *key)
{
    fr->clen = t5_build_cmd(fr->cmd, mid, cc, payload, plen);
    fr->n = t5_frame(fr->f, fr->cmd, fr->clen, key, src, (uint32)((int64)t5_now_sec + age), ++seqno);
}

static void noop_frame(frame_t *fr, uint16 src, int32 age) { mkframe(fr, SAMPLE_CMD_MID, 0, NULL, 0, src, age, KEY); }

/* Recompute MAC (with `key`) and CRC after the header was changed. */
static void reseal(frame_t *fr, const uint8 *key)
{
    size_t clen = fr->n - 50;
    t5_hmac(key, 32, fr->f, 16 + clen, fr->f + 16 + clen);
    t5_wbe16(fr->f + 48 + clen, t5_crc16(fr->f, 48 + clen));
}

static void bad_crc(frame_t *fr) { fr->f[fr->n - 1] ^= 0x01; }

static void bad_mac(frame_t *fr)
{
    size_t clen = fr->n - 50;
    fr->f[16 + clen + 3] ^= 0x10;
    t5_wbe16(fr->f + 48 + clen, t5_crc16(fr->f, 48 + clen));
}

static bool inject(const void *d, size_t n)
{
    bool ok = t5_udp_inject(cmdi_port(), d, n);
    if (!ok) UtAssert_Failed("no UDP socket is bound to port %u (1234 + node ID - 1)", (unsigned)cmdi_port());
    return ok;
}

static bool inject_frame(const frame_t *fr) { return inject(fr->f, fr->n); }

/* Index of the first transmission of `mid` at or after `from`, or -1. */
static int find_tx(uint32 mid, int from)
{
    int i;
    for (i = from; i < t5_ntx; i++) if (t5_tx[i].mid == mid) return i;
    return -1;
}

/* Send one frame in a WAKEUP of its own; true when CMDI transmitted its embedded command. */
static bool frame_executes(const frame_t *fr)
{
    int before = t5_ntx;
    if (!inject_frame(fr)) return false;
    wakeup();
    return find_tx(t5_msg_mid(fr->cmd), before) >= 0;
}

/* A datagram CMDI must reject for `reason`: nothing sent, one REJECT_ERR naming the reason (and source). */
static void check_rejection(const char *what, const frame_t *fr, const char *reason, const char *src_text)
{
    int before = t5_ntx, ev0 = t5_nev;
    const char *txt;
    if (!inject(fr->f, fr->n)) return;
    wakeup();
    txt = t5_last_ev_text(EID_REJECT, ev0);
    UtAssert_True(find_tx(t5_msg_mid(fr->cmd), before) < 0, "%s: nothing is sent on the software bus", what);
    UtAssert_True(t5_count_ev(EID_REJECT, ev0) == 1, "%s: one CMDI_REJECT_ERR_EID (12), saw %d", what, t5_count_ev(EID_REJECT, ev0));
    UtAssert_True(t5_ev_has(EID_REJECT, ev0, reason, src_text, NULL), "%s: the rejection event names %s%s%s (\"%s\")", what, reason,
                  src_text ? " and source " : "", src_text ? src_text : "", txt ? txt : "");
    UtAssert_True(t5_last_ev_type(EID_REJECT) == CFE_EVS_EventType_ERROR, "%s: event 12 is ERROR", what);
}

/* Put a message with an unexpected MID on the pipe that receives CMDI_CMD_MID. */
static bool deliver_unknown_mid(void)
{
    int k, p = -1, slot = -1;
    size_t n;
    for (k = 0; k < T5_SUBS_MAX; k++)
        if (t5_subs[k].used && t5_subs[k].mid == CMDI_CMD_MID) { p = t5_subs[k].pipe; break; }
    if (p < 0) { UtAssert_Failed("CMDI does not subscribe to CMDI_CMD_MID"); return false; }
    for (k = 0; k < T5_SUBS_MAX; k++) if (!t5_subs[k].used) { slot = k; break; }
    if (slot < 0) return false;
    t5_subs[slot].used = true;
    t5_subs[slot].mid = UNKNOWN_MID;
    t5_subs[slot].pipe = p;
    t5_subs[slot].msglim = 0;
    n = t5_build_cmd(t5_scratch, UNKNOWN_MID, 0, NULL, 0);
    t5_route(t5_scratch, n);
    t5_subs[slot].used = false;
    return t5_kick();
}

/* ===================================================================== tests */

static void Test_StartupAndHousekeeping(void)
{
    hk_t h;
    int i, k, found_cmd = 0, found_hk = 0, found_wake = 0;
    if (!boot_default()) return;
    UtAssert_True(t5_count_ev(EID_INIT, 0) == 1, "CMDI_INIT_INF_EID (1) emitted once at start-up, saw %d", t5_count_ev(EID_INIT, 0));
    UtAssert_True(t5_last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION");
    k = t5_tbl_find("ValTable");
    UtAssert_True(k >= 0 && t5_tbl[k].owned, "CMDI registers its table as ValTable");
    if (k >= 0)
    {
        UtAssert_True(t5_tbl[k].size == TBL_SIZE, "ValTable is %u bytes, CMDI_ValTable_t as specified is %u",
                      (unsigned)t5_tbl[k].size, TBL_SIZE);
        UtAssert_True(t5_tbl[k].validate != NULL, "ValTable has a validation function");
    }
    for (i = 0; i < T5_SUBS_MAX; i++)
    {
        if (!t5_subs[i].used) continue;
        found_cmd += t5_subs[i].mid == CMDI_CMD_MID;
        found_hk += t5_subs[i].mid == CMDI_SEND_HK_MID;
        found_wake += t5_subs[i].mid == CMDI_WAKEUP_MID;
    }
    UtAssert_True(found_cmd && found_hk && found_wake, "CMDI subscribes to CMDI_CMD_MID, CMDI_SEND_HK_MID and CMDI_WAKEUP_MID");
    UtAssert_True(t5_sock_by_port(1234) >= 0, "CMDI binds a UDP socket to port 1234 on node 1 at initialization");
    if (!get_hk(&h)) return;
    UtAssert_True(h.cc == 0 && h.cec == 0, "command counters start at 0 (%u/%u)", h.cc, h.cec);
    UtAssert_True(h.mask == 0x0F, "EnableMask %#x reported, the table has 0x0F", h.mask);
    UtAssert_True(h.max_age == 30, "MaxAgeSeconds %u reported, the table has 30", (unsigned)h.max_age);
    UtAssert_True(h.rx == 0 && h.acc == 0 && h.fmt == 0 && h.crc == 0 && h.hmac == 0 && h.src == 0 && h.age == 0,
                  "frame counters start at 0");
    cmd(CC_NOOP, NULL, 0);
    UtAssert_True(t5_count_ev(EID_NOOP, 0) == 1, "NOOP emits CMDI_NOOP_INF_EID (3)");
    UtAssert_True(t5_last_ev_type(EID_NOOP) == CFE_EVS_EventType_INFORMATION, "event 3 is INFORMATION");
    if (get_hk(&h)) UtAssert_True(h.cc == 1 && h.cec == 0, "NOOP counts as one accepted command (%u/%u)", h.cc, h.cec);
}

static void Test_PortPerNode(void)
{
    frame_t fr;
    t5_node_id = 2;
    if (!boot_default()) return;
    UtAssert_True(t5_sock_by_port(1235) >= 0, "on node 2 CMDI binds UDP port 1235 (1234 + node ID - 1)");
    UtAssert_True(t5_sock_by_port(1234) < 0, "on node 2 CMDI does not bind port 1234");
    noop_frame(&fr, 1, 0);
    UtAssert_True(frame_executes(&fr), "a valid frame sent to port 1235 on node 2 is accepted and its command sent");
}

static void Test_SocketError(void)
{
    hk_t h;
    int i, before;
    t5_bind_fail_port = 1234;
    if (!boot_default()) return;
    UtAssert_True(t5_count_ev(EID_SOCKET_ERR, 0) >= 1, "a socket that cannot be bound is reported with CMDI_SOCKET_ERR_EID (11)");
    UtAssert_True(t5_last_ev_type(EID_SOCKET_ERR) == CFE_EVS_EventType_ERROR, "event 11 is ERROR");
    UtAssert_True(get_hk(&h), "CMDI keeps running and answers SEND_HK without a socket");
    before = t5_ntx;
    for (i = 0; i < 20; i++) wakeup();
    UtAssert_True(t5_tx_count(SFI_HB_MID, before) == 2, "CMDI keeps sending its heartbeat without a socket (%d in 20 WAKEUPs)",
                  t5_tx_count(SFI_HB_MID, before));
}

static void Test_StartupErrors(void)
{
    t5_tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    boot_default();
    UtAssert_True(t5_count_ev(EID_TBLREG, 0) >= 1, "a table registration failure emits CMDI_TBL_REG_ERR_EID (8)");
    t5_app_end();

    Setup();
    t5_tbl_load_rc = CFE_TBL_ERR_FILE_TOO_LARGE;
    boot_default();
    UtAssert_True(t5_count_ev(EID_TBLLOAD, 0) >= 1, "a table load failure emits CMDI_TBL_LOAD_ERR_EID (9)");
    t5_app_end();

    Setup();
    t5_recv_rc = CFE_SB_PIPE_RD_ERR;
    boot_default();
    UtAssert_True(t5_count_ev(EID_PIPE_ERR, 0) >= 1, "a software bus read failure emits CMDI_PIPE_ERR_EID (7)");
}

static void Test_ValidFrame(void)
{
    frame_t fr;
    hk_t h;
    int before, i, ev0;
    if (!boot_default()) return;
    t5_now_sec = 1234567;
    noop_frame(&fr, 1, 0);
    before = t5_ntx;
    ev0 = t5_nev;
    if (!inject_frame(&fr)) return;
    wakeup();
    i = find_tx(SAMPLE_CMD_MID, before);
    UtAssert_True(i >= 0, "a valid frame's embedded command is sent on the software bus");
    if (i >= 0)
    {
        UtAssert_True(t5_tx[i].size == fr.clen && memcmp(t5_tx[i].bytes, fr.cmd, fr.clen) == 0,
                      "the command is sent exactly as embedded in the frame (%u bytes)", (unsigned)t5_tx[i].size);
        UtAssert_True(!tx_orig[i], "the embedded command is sent with IsOrigination false");
        UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 1, "the command is sent once");
    }
    UtAssert_True(cmdval_n_validate >= 1, "CMDI validates the frame with CMDVAL_ValidateFrame");
    UtAssert_True(cmdval_last_now == 1234567, "validation uses the current onboard seconds (%u, expected 1234567)",
                  (unsigned)cmdval_last_now);
    UtAssert_True(memcmp(cmdval_last_key, KEY, 32) == 0, "validation uses the active table's key");
    UtAssert_True(t5_count_ev(EID_ACCEPT, ev0) == 1 && t5_ev_has(EID_ACCEPT, ev0, "0x1882", NULL, NULL),
                  "CMDI_ACCEPT_DBG_EID (13) names the command's MID as 0x1882");
    UtAssert_True(t5_last_ev_type(EID_ACCEPT) == CFE_EVS_EventType_DEBUG, "event 13 is DEBUG");
    UtAssert_True(t5_count_ev(EID_REJECT, 0) == 0, "no rejection event for a valid frame");
    ev0 = t5_nev;
    mkframe(&fr, VERIF_MID, 0, "abcdefgh", 8, 2, 0, KEY);
    UtAssert_True(frame_executes(&fr), "a valid frame from source 2 carrying another command MID is accepted");
    UtAssert_True(t5_ev_has(EID_ACCEPT, ev0, "0x18EA", NULL, NULL), "CMDI_ACCEPT_DBG_EID prints the MID as 0x%%04X (0x18EA)");
    if (get_hk(&h))
        UtAssert_True(h.rx == 2 && h.acc == 2 && h.fmt + h.crc + h.hmac + h.src + h.age == 0,
                      "FramesReceived %u and FramesAccepted %u after two valid frames", (unsigned)h.rx, (unsigned)h.acc);
}

static void Test_RejectionReasons(void)
{
    vt_t t;
    frame_t fr;
    hk_t h;
    static const uint16 src[3] = {1, 2, 4321};
    uint8 raw[1100];
    size_t n;
    int before;
    vt_make(&t, KEY, src, 3, 30, 0x0F);
    if (!boot(&t)) return;

    noop_frame(&fr, 4321, 0);
    bad_crc(&fr);
    check_rejection("a frame with a bad CRC", &fr, "CRC", "4321");
    if (get_hk(&h)) UtAssert_True(h.crc == 1 && h.hmac == 0, "CrcErrors %u (expected 1)", (unsigned)h.crc);

    noop_frame(&fr, 4321, 0);
    bad_mac(&fr);
    check_rejection("a frame with a bad MAC", &fr, "HMAC", "4321");
    if (get_hk(&h)) UtAssert_True(h.hmac == 1, "HmacErrors %u (expected 1)", (unsigned)h.hmac);

    noop_frame(&fr, 4321, 0);
    bad_mac(&fr);
    bad_crc(&fr);
    check_rejection("a frame with a bad CRC and a bad MAC", &fr, "CRC", "4321");
    if (get_hk(&h))
        UtAssert_True(h.crc == 2 && h.hmac == 1, "a frame with both defects counts as a CRC error (CRC %u, HMAC %u)", (unsigned)h.crc,
                      (unsigned)h.hmac);

    noop_frame(&fr, 6789, 0);
    check_rejection("a frame from an unlisted source", &fr, "SOURCE", "6789");
    if (get_hk(&h)) UtAssert_True(h.src == 1, "SourceErrors %u (expected 1)", (unsigned)h.src);

    noop_frame(&fr, 4321, -31);
    check_rejection("a frame 31 s older than the onboard time", &fr, "AGE", "4321");
    noop_frame(&fr, 4321, 31);
    check_rejection("a frame 31 s ahead of the onboard time", &fr, "AGE", "4321");
    if (get_hk(&h)) UtAssert_True(h.age == 2, "AgeErrors %u (expected 2)", (unsigned)h.age);
    noop_frame(&fr, 4321, -30);
    UtAssert_True(frame_executes(&fr), "a frame exactly MaxAgeSeconds (30) old is accepted");
    noop_frame(&fr, 4321, 30);
    UtAssert_True(frame_executes(&fr), "a frame exactly MaxAgeSeconds (30) ahead is accepted");

    n = t5_build_cmd(raw, SAMPLE_CMD_MID, 0, NULL, 0);
    fr.n = n;
    memcpy(fr.f, raw, n);
    memcpy(fr.cmd, raw, n);
    fr.clen = n;
    check_rejection("an unframed command packet", &fr, "FORMAT", NULL);

    noop_frame(&fr, 4321, 0);
    fr.f[0] = 0x43;
    fr.f[1] = 0x57;
    reseal(&fr, KEY);
    check_rejection("a frame with a wrong magic", &fr, "FORMAT", NULL);

    /* A complete valid frame with L = 1024 followed by 26 more bytes: one 1100-byte datagram. */
    memset(raw, 0x5A, sizeof(raw));
    mkframe(&fr, SAMPLE_CMD_MID, 0, raw, 1016, 4321, 0, KEY);
    UtAssert_True(fr.n == 1074, "the largest frame is 1074 bytes (%u)", (unsigned)fr.n);
    memset(fr.f + fr.n, 0xA5, 26);
    fr.n += 26;
    check_rejection("a datagram longer than 1074 bytes", &fr, "FORMAT", NULL);
    if (get_hk(&h))
    {
        UtAssert_True(h.fmt == 3, "FormatErrors %u (expected 3)", (unsigned)h.fmt);
        UtAssert_True(h.rx == 11 && h.acc == 2, "FramesReceived %u (expected 11), FramesAccepted %u (expected 2)", (unsigned)h.rx,
                      (unsigned)h.acc);
    }
    before = t5_ntx;
    noop_frame(&fr, 1, 0);
    UtAssert_True(frame_executes(&fr) && t5_tx_count(SAMPLE_CMD_MID, before) == 1, "a valid frame is accepted after the rejections");
}

static void Test_RejectionStatuses(void)
{
    vt_t t;
    frame_t fr;
    hk_t h;
    static const uint16 src[3] = {1, 2, 4321};
    static const char *const names[6] = {"", "FORMAT", "CRC", "HMAC", "SOURCE", "AGE"};
    int st;
    vt_make(&t, KEY, src, 3, 30, 0x0F);
    if (!boot(&t)) return;
    for (st = 1; st <= 5; st++)
    {
        char what[72];
        snprintf(what, sizeof(what), "a frame CMDVAL_ValidateFrame rejects with status %d", st);
        cmdval_force_status = st;
        noop_frame(&fr, 4321, 0);
        check_rejection(what, &fr, names[st], st == 1 ? NULL : "4321");
    }
    cmdval_force_status = -1;
    if (get_hk(&h))
        UtAssert_True(h.fmt == 1 && h.crc == 1 && h.hmac == 1 && h.src == 1 && h.age == 1 && h.rx == 5 && h.acc == 0,
                      "each status counts under its reason: FORMAT %u CRC %u HMAC %u SOURCE %u AGE %u, received %u, accepted %u",
                      (unsigned)h.fmt, (unsigned)h.crc, (unsigned)h.hmac, (unsigned)h.src, (unsigned)h.age, (unsigned)h.rx,
                      (unsigned)h.acc);
}

static void Test_ReadsPerWakeup(void)
{
    frame_t fr;
    int i, before;
    if (!boot_default()) return;
    for (i = 0; i < 17; i++)
    {
        noop_frame(&fr, 1, 0);
        if (!inject_frame(&fr)) return;
    }
    before = t5_ntx;
    wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 16, "one WAKEUP reads up to 16 pending datagrams (%d of 17 executed)",
                  t5_tx_count(SAMPLE_CMD_MID, before));
    before = t5_ntx;
    wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 1, "the 17th datagram is read on the next WAKEUP (%d)",
                  t5_tx_count(SAMPLE_CMD_MID, before));
    before = t5_ntx;
    wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 0, "a WAKEUP with nothing pending sends nothing");
    UtAssert_True(t5_blocking_recvs == 0 && t5_dev_waiting_selects == 0,
                  "datagrams are read without blocking (%d waiting receives, %d waiting selects)", t5_blocking_recvs,
                  t5_dev_waiting_selects);
}

static void Test_Heartbeat(void)
{
    int i, before, k = 0, idx[8];
    if (!boot_default()) return;
    before = t5_ntx;
    for (i = 0; i < 10; i++) wakeup();
    UtAssert_True(t5_tx_count(SFI_HB_MID, before) == 1, "one heartbeat on SFI_HEARTBEAT_MID per 10 WAKEUPs (%d in the first 10)",
                  t5_tx_count(SFI_HB_MID, before));
    for (i = 0; i < 20; i++) wakeup();
    UtAssert_True(t5_tx_count(SFI_HB_MID, before) == 3, "three heartbeats in 30 WAKEUPs (%d)", t5_tx_count(SFI_HB_MID, before));
    for (i = before; i < t5_ntx && k < 8; i++) if (t5_tx[i].mid == SFI_HB_MID) idx[k++] = i;
    for (i = 0; i < k && i < 3; i++)
    {
        const t5_tx_t *t = &t5_tx[idx[i]];
        UtAssert_True(t->size == HB_SIZE, "heartbeat %d is an SFI_HeartbeatCmd_t of %u bytes (%u)", i + 1, HB_SIZE, (unsigned)t->size);
        UtAssert_True(t5_msg_is_cmd(t->bytes) && t5_msg_fcn(t->bytes) == 0, "heartbeat %d is a command with function code 0", i + 1);
        UtAssert_True(strncmp((const char *)t->bytes + CMD_HDR, "CMDI", 20) == 0, "heartbeat %d names AppName CMDI", i + 1);
        UtAssert_True(t5_get32(t->bytes + CMD_HDR + 20) == (uint32)(i + 1), "heartbeat %d has Sequence %u (expected %d)", i + 1,
                      (unsigned)t5_get32(t->bytes + CMD_HDR + 20), i + 1);
    }
}

static void Test_SetMaxAge(void)
{
    frame_t fr;
    hk_t a, h;
    if (!boot_default()) return;
    get_hk(&a);
    noop_frame(&fr, 1, -100);
    UtAssert_True(!frame_executes(&fr), "a frame 100 s old is rejected with MaxAgeSeconds 30");
    cmd_max_age(120);
    UtAssert_True(t5_count_ev(EID_PARAM_INF, 0) == 1, "SET_MAX_AGE 120 emits CMDI_SET_PARAM_INF_EID (14)");
    if (get_hk(&h))
        UtAssert_True(h.max_age == 120 && h.cc == (uint8)(a.cc + 1), "MaxAgeSeconds %u after SET_MAX_AGE 120", (unsigned)h.max_age);
    noop_frame(&fr, 1, -100);
    UtAssert_True(frame_executes(&fr), "a frame 100 s old is accepted after SET_MAX_AGE 120");
    get_hk(&a);
    cmd_max_age(0);
    cmd_max_age(86401);
    UtAssert_True(t5_count_ev(EID_PARAM_ERR, 0) == 2, "SET_MAX_AGE 0 and 86401 each emit CMDI_SET_PARAM_ERR_EID (15), saw %d",
                  t5_count_ev(EID_PARAM_ERR, 0));
    UtAssert_True(t5_last_ev_type(EID_PARAM_ERR) == CFE_EVS_EventType_ERROR, "event 15 is ERROR");
    if (get_hk(&h))
        UtAssert_True(h.max_age == 120 && h.cec == (uint8)(a.cec + 2) && h.cc == a.cc,
                      "rejected values change nothing and count as command errors (MaxAge %u, errors %u -> %u)", (unsigned)h.max_age,
                      a.cec, h.cec);
    cmd_max_age(86400);
    if (get_hk(&h)) UtAssert_True(h.max_age == 86400, "SET_MAX_AGE 86400 is accepted (%u)", (unsigned)h.max_age);
    cmd_max_age(1);
    if (get_hk(&h)) UtAssert_True(h.max_age == 1, "SET_MAX_AGE 1 is accepted (%u)", (unsigned)h.max_age);
}

static void Test_SetEnableMask(void)
{
    frame_t fr;
    hk_t a, h;
    if (!boot_default()) return;
    noop_frame(&fr, 1, 0);
    bad_mac(&fr);
    UtAssert_True(!frame_executes(&fr), "a frame with a bad MAC is rejected with EnableMask 0x0F");
    cmd_mask(0x0D);
    UtAssert_True(t5_count_ev(EID_PARAM_INF, 0) == 1, "SET_ENABLE_MASK 0x0D emits CMDI_SET_PARAM_INF_EID (14)");
    get_hk(&a);
    UtAssert_True(a.mask == 0x0D, "EnableMask %#x after SET_ENABLE_MASK 0x0D", a.mask);
    noop_frame(&fr, 1, 0);
    bad_mac(&fr);
    UtAssert_True(frame_executes(&fr), "with the HMAC bit cleared a frame with a bad MAC is accepted");
    if (get_hk(&h))
        UtAssert_True(h.hmac == a.hmac && h.acc == a.acc + 1, "HmacErrors unchanged (%u), FramesAccepted %u -> %u", (unsigned)h.hmac,
                      (unsigned)a.acc, (unsigned)h.acc);
    cmd_mask(0x10);
    UtAssert_True(t5_count_ev(EID_PARAM_ERR, 0) == 1, "SET_ENABLE_MASK 0x10 emits CMDI_SET_PARAM_ERR_EID (15)");
    if (get_hk(&h)) UtAssert_True(h.mask == 0x0D && h.cec == (uint8)(a.cec + 1), "a rejected mask changes nothing (%#x)", h.mask);
    cmd_mask(0x00);
    if (get_hk(&h)) UtAssert_True(h.mask == 0x00, "SET_ENABLE_MASK 0 is accepted (%#x)", h.mask);
    noop_frame(&fr, 77, 5000);
    bad_crc(&fr);
    UtAssert_True(frame_executes(&fr), "with EnableMask 0 only FORMAT is checked: a bad CRC, unlisted source and old time pass");
    cmd_mask(0x0F);
    if (get_hk(&h)) UtAssert_True(h.mask == 0x0F, "SET_ENABLE_MASK 0x0F is accepted (%#x)", h.mask);
}

static void Test_FramedParameterCommands(void)
{
    frame_t fr;
    hk_t h;
    uint8 p[4] = {0x0D, 0, 0, 0};
    int before;
    if (!boot_default()) return;
    mkframe(&fr, CMDI_CMD_MID, CC_SET_MASK, p, 4, 1, 0, KEY);
    bad_mac(&fr);
    UtAssert_True(!frame_executes(&fr), "a SET_ENABLE_MASK frame with a bad MAC is rejected");
    if (get_hk(&h))
        UtAssert_True(h.mask == 0x0F && h.hmac == 1, "a rejected SET_ENABLE_MASK frame changes nothing (mask %#x)", h.mask);
    before = t5_ntx;
    t5_build_cmd(fr.cmd, CMDI_CMD_MID, CC_SET_MASK, p, 4);
    inject(fr.cmd, 12);
    wakeup();
    UtAssert_True(find_tx(CMDI_CMD_MID, before) < 0, "an unframed SET_ENABLE_MASK packet is not sent on the bus");
    if (get_hk(&h))
        UtAssert_True(h.mask == 0x0F && h.fmt == 1, "an unframed SET_ENABLE_MASK is a FORMAT rejection and changes nothing (mask %#x)",
                      h.mask);
    mkframe(&fr, CMDI_CMD_MID, CC_SET_MASK, p, 4, 1, 0, KEY);
    UtAssert_True(frame_executes(&fr), "a valid SET_ENABLE_MASK frame is accepted and its command sent to CMDI");
    if (get_hk(&h)) UtAssert_True(h.mask == 0x0D, "the framed SET_ENABLE_MASK takes effect (mask %#x)", h.mask);
    noop_frame(&fr, 1, 0);
    bad_mac(&fr);
    UtAssert_True(frame_executes(&fr), "the next frame is checked against the new mask (bad MAC accepted)");
    t5_put32(p, 300);
    mkframe(&fr, CMDI_CMD_MID, CC_SET_MAX_AGE, p, 4, 1, 0, KEY);
    UtAssert_True(frame_executes(&fr), "a valid SET_MAX_AGE frame is accepted");
    if (get_hk(&h)) UtAssert_True(h.max_age == 300, "the framed SET_MAX_AGE takes effect (%u)", (unsigned)h.max_age);
}

static void Test_TableOverrides(void)
{
    vt_t t;
    frame_t fr;
    hk_t h;
    static const uint16 src[2] = {5, 9};
    if (!boot_default()) return;
    cmd_max_age(500);
    cmd_mask(0x07);
    if (get_hk(&h))
        UtAssert_True(h.max_age == 500 && h.mask == 0x07, "run-time values set (%u, %#x)", (unsigned)h.max_age, h.mask);
    vt_make(&t, KEY2, src, 2, 45, 0x0F);
    UtAssert_True(t5_tbl_submit("ValTable", t.b) == CFE_SUCCESS, "a valid ValTable image passes validation");
    wakeup();
    t5_send_cmd(CMDI_SEND_HK_MID, 0, NULL, 0);
    if (get_hk(&h))
        UtAssert_True(h.max_age == 45 && h.mask == 0x0F, "a table load overrides SET_MAX_AGE and SET_ENABLE_MASK (MaxAge %u, mask %#x)",
                      (unsigned)h.max_age, h.mask);
    mkframe(&fr, SAMPLE_CMD_MID, 0, NULL, 0, 5, 0, KEY2);
    UtAssert_True(frame_executes(&fr), "after the table load a frame under the new key from a new source is accepted");
    UtAssert_True(memcmp(cmdval_last_key, KEY2, 32) == 0, "validation uses the newly loaded key");
    noop_frame(&fr, 5, 0);
    UtAssert_True(!frame_executes(&fr), "a frame under the old key is rejected after the table load");
    mkframe(&fr, SAMPLE_CMD_MID, 0, NULL, 0, 1, 0, KEY2);
    UtAssert_True(!frame_executes(&fr), "a source no longer listed is rejected after the table load");
}

static void Test_TableValidation(void)
{
    vt_t t;
    static const uint16 two[2] = {1, 2};
    static const uint16 dup[3] = {7, 0, 7};
    static const uint16 zeros[4] = {3, 0, 0, 4};
    static const uint16 eight[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    static const uint8 zero_key[32] = {0};
    int ev0;
    if (!boot_default()) return;
    vt_default(&t);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "the default table is valid");
    ev0 = t5_nev;
    vt_make(&t, zero_key, two, 2, 30, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "an all-zero key is rejected");
    UtAssert_True(t5_count_ev(EID_TBLVAL, ev0) >= 1, "a rejected table emits CMDI_TBL_VAL_ERR_EID (10)");
    UtAssert_True(t5_last_ev_type(EID_TBLVAL) == CFE_EVS_EventType_ERROR, "event 10 is ERROR");
    vt_make(&t, KEY, two, 2, 0, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "MaxAgeSeconds 0 is rejected");
    vt_make(&t, KEY, two, 2, 86401, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "MaxAgeSeconds 86401 is rejected");
    vt_make(&t, KEY, two, 2, 30, 0x10);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "EnableMask 0x10 is rejected");
    vt_make(&t, KEY, two, 2, 30, 0x8F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "EnableMask 0x8F is rejected");
    vt_make(&t, KEY, NULL, 0, 30, 0x04);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "the SOURCE bit with no nonzero source is rejected");
    vt_make(&t, KEY, dup, 3, 30, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) != CFE_SUCCESS, "duplicate nonzero sources are rejected");
    ev0 = t5_nev;
    vt_make(&t, KEY, two, 2, 1, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "MaxAgeSeconds 1 is accepted");
    vt_make(&t, KEY, two, 2, 86400, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "MaxAgeSeconds 86400 is accepted");
    vt_make(&t, KEY, NULL, 0, 30, 0x0B);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "no source is needed without the SOURCE bit");
    vt_make(&t, KEY, NULL, 0, 30, 0x00);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "EnableMask 0 is accepted");
    vt_make(&t, KEY, zeros, 4, 30, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "repeated unused (0) source slots are accepted");
    vt_make(&t, KEY, eight, 8, 30, 0x0F);
    UtAssert_True(t5_tbl_validate("ValTable", t.b) == CFE_SUCCESS, "eight distinct sources are accepted");
    UtAssert_True(t5_count_ev(EID_TBLVAL, ev0) == 0, "valid tables emit no CMDI_TBL_VAL_ERR_EID");
}

static void Test_CommandsAndCounters(void)
{
    frame_t fr;
    hk_t a, h;
    uint8 extra[8] = {0};
    int i;
    if (!boot_default()) return;
    for (i = 0; i < 3; i++)
    {
        noop_frame(&fr, 1, 0);
        frame_executes(&fr);
    }
    noop_frame(&fr, 1, 0);
    bad_crc(&fr);
    frame_executes(&fr);
    cmd(CC_NOOP, NULL, 0);
    cmd(CC_NOOP, NULL, 0);
    cmd(99, NULL, 0);
    UtAssert_True(t5_count_ev(EID_CC_ERR, 0) == 1, "an invalid function code emits CMDI_CC_ERR_EID (2)");
    UtAssert_True(t5_last_ev_type(EID_CC_ERR) == CFE_EVS_EventType_ERROR, "event 2 is ERROR");
    cmd(CC_NOOP, extra, 4);
    cmd(CC_RESET, extra, 4);
    cmd(CC_SET_MAX_AGE, extra, 2);
    cmd(CC_SET_MASK, extra, 1);
    UtAssert_True(t5_count_ev(EID_LEN_ERR, 0) == 4, "each wrongly sized command emits CMDI_CMD_LEN_ERR_EID (6), saw %d of 4",
                  t5_count_ev(EID_LEN_ERR, 0));
    UtAssert_True(t5_last_ev_type(EID_LEN_ERR) == CFE_EVS_EventType_ERROR, "event 6 is ERROR");
    UtAssert_True(t5_count_ev(EID_NOOP, 0) == 2 && t5_count_ev(EID_RESET, 0) == 0 && t5_count_ev(EID_PARAM_INF, 0) == 0,
                  "wrongly sized commands do nothing");
    if (!get_hk(&a)) return;
    UtAssert_True(a.cc == 2 && a.cec == 5, "two accepted and five rejected commands: CommandCounter %u, CommandErrorCounter %u", a.cc,
                  a.cec);
    UtAssert_True(a.rx == 4 && a.acc == 3 && a.crc == 1, "frame counters before the reset: received %u accepted %u CRC %u",
                  (unsigned)a.rx, (unsigned)a.acc, (unsigned)a.crc);
    deliver_unknown_mid();
    UtAssert_True(t5_count_ev(EID_MID_ERR, 0) == 1, "a message with an unexpected MID emits CMDI_MID_ERR_EID (5)");
    UtAssert_True(t5_last_ev_type(EID_MID_ERR) == CFE_EVS_EventType_ERROR, "event 5 is ERROR");
    cmd_max_age(77);
    cmd_mask(0x0B);
    cmd(CC_RESET, NULL, 0);
    UtAssert_True(t5_count_ev(EID_RESET, 0) == 1, "RESET_COUNTERS emits CMDI_RESET_INF_EID (4)");
    if (get_hk(&h))
    {
        UtAssert_True(h.cc == 0 && h.cec == 0, "RESET_COUNTERS zeroes the command counters (%u/%u)", h.cc, h.cec);
        UtAssert_True(h.rx == 0 && h.acc == 0 && h.fmt == 0 && h.crc == 0 && h.hmac == 0 && h.src == 0 && h.age == 0,
                      "RESET_COUNTERS zeroes FramesReceived, FramesAccepted and the five reason counters");
        UtAssert_True(h.max_age == 77 && h.mask == 0x0B, "RESET_COUNTERS keeps MaxAgeSeconds (%u) and EnableMask (%#x)",
                      (unsigned)h.max_age, h.mask);
    }
}

void UtTest_Setup(void)
{
    UtAssert_True(sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes",
                  (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    UtAssert_True(sizeof(CFE_MSG_CommandHeader_t) == CMD_HDR, "command header is %u bytes", (unsigned)sizeof(CFE_MSG_CommandHeader_t));
    UtAssert_True(CFE_MISSION_MAX_API_LEN == 20, "CFE_MISSION_MAX_API_LEN is %d", (int)CFE_MISSION_MAX_API_LEN);
    UtTest_Add(Test_StartupAndHousekeeping, Setup, Teardown, "Startup and housekeeping");
    UtTest_Add(Test_PortPerNode, Setup, Teardown, "Port per node");
    UtTest_Add(Test_SocketError, Setup, Teardown, "Socket error");
    UtTest_Add(Test_StartupErrors, Setup, Teardown, "Startup error events");
    UtTest_Add(Test_ValidFrame, Setup, Teardown, "Valid frame");
    UtTest_Add(Test_RejectionReasons, Setup, Teardown, "Rejection reasons");
    UtTest_Add(Test_RejectionStatuses, Setup, Teardown, "Rejection statuses");
    UtTest_Add(Test_ReadsPerWakeup, Setup, Teardown, "Reads per wakeup");
    UtTest_Add(Test_Heartbeat, Setup, Teardown, "Heartbeat");
    UtTest_Add(Test_SetMaxAge, Setup, Teardown, "Set max age");
    UtTest_Add(Test_SetEnableMask, Setup, Teardown, "Set enable mask");
    UtTest_Add(Test_FramedParameterCommands, Setup, Teardown, "Framed parameter commands");
    UtTest_Add(Test_TableOverrides, Setup, Teardown, "Table load overrides");
    UtTest_Add(Test_TableValidation, Setup, Teardown, "Table validation");
    UtTest_Add(Test_CommandsAndCounters, Setup, Teardown, "Commands and counters");
}
