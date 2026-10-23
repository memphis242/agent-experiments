/*
 * Black-box behaviour test of the SFI application (spec S6, with C.3 to C.5).
 *
 * SFI_AppMain runs on its own thread against the behaviour model in t5_model.h. The test delivers
 * heartbeats, watched packets, wakeups and commands on the software bus, sets the clock before each
 * wakeup, loads SfiTable images, and reads the housekeeping and status packets SFI transmits.
 * Only the names, structure layouts (decoded by offset), message IDs, function codes and event IDs of
 * the specification are used.
 */
#include "t5_model.h"

extern void SFI_AppMain(void);

/* ------------------------------------------------------------------------- pinned identifiers */

#define M_CMD       0x18DCu
#define M_SEND_HK   0x18DDu
#define M_WAKEUP    0x18DEu
#define M_HEARTBEAT 0x18DFu
#define M_HK        0x08DCu
#define M_STATUS    0x08DDu
#define M_STOR_HK   0x08C4u
#define M_CMDI_HK   0x08D0u
#define M_TLMO_HK   0x08D4u
#define M_GW_HK     0x08D8u
#define M_STALL     0x18ECu
#define M_RATE      0x18EDu
#define M_EXTRA     0x18EEu
#define M_FOREIGN   0x18F7u

#define CC_NOOP          0
#define CC_RESET         1
#define CC_SET_MISS      2
#define CC_SET_WINDOW    3
#define CC_SEND_STATUS   4
#define CC_CLEAR_HISTORY 5

#define EID_INIT       1
#define EID_CC_ERR     2
#define EID_NOOP       3
#define EID_RESET      4
#define EID_MID_ERR    5
#define EID_LEN_ERR    6
#define EID_PIPE_ERR   7
#define EID_TBLREG     8
#define EID_TBLLOAD    9
#define EID_TBLVAL     10
#define EID_SUB_ERR    11
#define EID_MISS       12
#define EID_RESTORED   13
#define EID_UNKNOWN    14
#define EID_STALL      15
#define EID_RATE       16
#define EID_CLEAR      17
#define EID_CLUSTER    18
#define EID_PARAM_OK   19
#define EID_PARAM_ERR  20
#define EID_STATUS     21
#define EID_HIST_CLEAR 22

#define TBL_SIZE   780
#define HK_PAYLOAD 76
#define ST_PAYLOAD 3888
#define TLM_HDR    16

#define RULE_STALL 1
#define RULE_RATE  2

#define APP_UNARMED 0
#define APP_OK      1
#define APP_MISSING 2

#define W_UNUSED  0
#define W_PENDING 1
#define W_OK      2
#define W_FAULT   3

#define CORR_NONE  0
#define CORR_UNCOR 1
#define CORR_CLUST 2

/* ------------------------------------------------------------------------- table images (S6.1) */

typedef struct { uint8 b[TBL_SIZE]; } stbl_t;

static void stbl_params(stbl_t *t, uint8 n, uint16 w, uint16 h)
{
    t->b[0] = n;
    t5_put16(t->b + 2, w);
    t5_put16(t->b + 4, h);
}

static void stbl_app(stbl_t *t, int i, const char *name)
{
    memset(t->b + 8 + i * 24, 0, 20);
    memcpy(t->b + 8 + i * 24, name, strlen(name));
    memset(t->b + 28 + i * 24, 1, 2);
    t->b[30 + i * 24] = 5; t->b[31 + i * 24] = 10;
}

static void stbl_watch(stbl_t *t, int i, uint32 mid, uint16 off, uint8 width, uint8 rule, uint32 limit)
{
    uint8 *w = t->b + 392 + i * 24;
    t5_put32(w, mid);
    t5_put16(w + 4, off);
    w[6] = width;
    w[7] = rule;
    t5_put32(w + 8, limit);
    memset(w + 12, 0, 12);
    if (rule != 0) { w[21] = i < 4 ? (uint8)i : 255; w[22] = 1; w[23] = 1; }
}

static void stbl_default(stbl_t *t)
{
    static const char *const apps[] = {"STOR", "CMDI", "TLMO", "GW", "SFI_T1", "SFI_T2", "SFI_T3", "MODE"};
    int i;
    memset(t, 0, sizeof(*t));
    stbl_params(t, 3, 5, 60);
    for (i = 0; i < 8; i++) stbl_app(t, i, apps[i]);
    stbl_watch(t, 0, M_STOR_HK, 17, 1, RULE_RATE, 2);
    stbl_watch(t, 1, M_CMDI_HK, 17, 1, RULE_RATE, 2);
    stbl_watch(t, 2, M_TLMO_HK, 17, 1, RULE_RATE, 2);
    stbl_watch(t, 3, M_GW_HK, 17, 1, RULE_RATE, 2);
    stbl_watch(t, 4, M_STALL, 16, 4, RULE_STALL, 3);
    stbl_watch(t, 5, M_RATE, 16, 2, RULE_RATE, 5);
    stbl_watch(t, 6, 0x18EF, 16, 4, 3, 0);
    t5_put32(t->b + 392 + 6*24 + 12, 10);
    t5_put32(t->b + 392 + 6*24 + 16, 20);
    t->b[392 + 6*24 + 20] = 3; t->b[392 + 6*24 + 22] = 2;
    t->b[776] = 1; t->b[777] = 10;
}

/* ------------------------------------------------------------------------- driving SFI */

static void sfi_setup(void)
{
    stbl_t t;
    t5_setup();
    t5_app_entry = SFI_AppMain;
    stbl_default(&t);
    t5_tbl_set_file("SfiTable", t.b, TBL_SIZE);
}

static bool boot(const stbl_t *t)
{
    t5_tbl_set_file("SfiTable", t->b, TBL_SIZE);
    return t5_app_start();
}

static bool boot_default(void)
{
    stbl_t t;
    stbl_default(&t);
    return boot(&t);
}

static bool cmd(int cc, const void *payload, size_t plen) { return t5_send_cmd(M_CMD, cc, payload, plen); }

static bool hb(const char *name, uint32 seq)
{
    uint8 p[24];
    size_t n = strlen(name) < 20 ? strlen(name) : 19;
    memset(p, 0, sizeof(p));
    memcpy(p, name, n);
    t5_put32(p + 20, seq);
    return t5_send_cmd(M_HEARTBEAT, 0, p, sizeof(p));
}

/* One 1 Hz cycle: heartbeats of the listed apps, then a WAKEUP at time (sec, sub). */
static bool cycle_at(const char *const *alive, int n, uint32 sec, uint32 sub)
{
    int i;
    for (i = 0; i < n; i++) hb(alive[i], 1);
    t5_now_sec = sec;
    t5_now_sub = sub;
    return t5_send_cmd(M_WAKEUP, 0, NULL, 0);
}

static bool cycle(const char *const *alive, int n)
{
    return cycle_at(alive, n, t5_now_sec + 1, 0);
}

static bool set_miss_limit(uint8 v)
{
    uint8 p[4] = {0, 0, 0, 0};
    p[0] = v;
    return cmd(CC_SET_MISS, p, sizeof(p));
}

static bool set_window(uint16 v)
{
    uint8 p[4] = {0, 0, 0, 0};
    t5_put16(p, v);
    return cmd(CC_SET_WINDOW, p, sizeof(p));
}

/* A verification packet on `mid` of `size` bytes with `value` (width bytes) at byte 16. */
static bool watch_pkt(uint32 mid, size_t size, uint32 value, int width)
{
    uint8 b[64];
    size_t n = t5_build_verif(b, mid, size, value, width);
    return t5_deliver(b, n);
}

/* A housekeeping telemetry packet on `mid` whose byte 17 (CommandErrorCounter) is `v`. */
static bool hk_tlm_pkt(uint32 mid, uint8 v)
{
    uint8 b[64];
    size_t n = t5_build_tlm(b, mid, 52, 0);
    b[17] = v;
    return t5_deliver(b, n);
}

/* ------------------------------------------------------------------------- reading telemetry */

typedef struct
{
    uint8  cc, cec, monitored, missing, infault, corr, misslimit;
    uint16 recent, ccount, largest, window;
    uint32 total, unknown, missev, stall, rate;
} hk_t;

static bool get_hk(hk_t *h)
{
    int before = t5_ntx;
    const t5_tx_t *t;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!t5_send_cmd(M_SEND_HK, 0, NULL, 0)) return false;
    t = t5_last_tx(M_HK, before);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_HK did not transmit a housekeeping packet on SFI_HK_TLM_MID (0x08DC)");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + HK_PAYLOAD, "SFI_HkTlm_t is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + HK_PAYLOAD));
    p = t->bytes + TLM_HDR;
    h->cc = p[0]; h->cec = p[1]; h->monitored = p[2]; h->missing = p[3];
    h->infault = p[4]; h->corr = p[5]; h->misslimit = p[6];
    h->recent = t5_get16(p + 8); h->ccount = t5_get16(p + 10); h->largest = t5_get16(p + 12); h->window = t5_get16(p + 14);
    h->total = t5_get32(p + 16); h->unknown = t5_get32(p + 20); h->missev = t5_get32(p + 24);
    h->stall = t5_get32(p + 28); h->rate = t5_get32(p + 32);
    return true;
}

typedef struct { char name[21]; uint8 state, missed; uint16 total; uint32 lsec, lsub; } app_st_t;
typedef struct { uint8 idx; uint32 sec, sub; } hist_t;
typedef struct { uint8 state; uint32 last; } watch_st_t;
typedef struct
{
    uint8      missing, corr;
    uint16     hcount, ccount, largest;
    app_st_t   app[16];
    hist_t     hist[16];
    watch_st_t watch[16];
} st_t;

static bool get_status(st_t *s)
{
    int before = t5_ntx, ev0 = t5_nev, i;
    const t5_tx_t *t;
    const uint8 *p;
    memset(s, 0, sizeof(*s));
    if (!cmd(CC_SEND_STATUS, NULL, 0)) return false;
    t = t5_last_tx(M_STATUS, before);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_STATUS did not transmit an SFI_StatusTlm_t on SFI_STATUS_TLM_MID (0x08DD)");
        return false;
    }
    UtAssert_True(t5_count_ev(EID_STATUS, ev0) == 1, "SEND_STATUS emits SFI_STATUS_INF_EID (21) once");
    UtAssert_True(t->size == TLM_HDR + ST_PAYLOAD, "SFI_StatusTlm_t is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + ST_PAYLOAD));
    if (t->size < TLM_HDR + ST_PAYLOAD) return false;
    p = t->bytes + TLM_HDR;
    s->missing = p[0]; s->corr = p[1]; s->hcount = t5_get16(p + 2);
    s->ccount = t5_get16(p + 4); s->largest = t5_get16(p + 6);
    for (i = 0; i < 16; i++)
    {
        const uint8 *a = p + 8 + i * 36;
        memcpy(s->app[i].name, a, 20);
        s->app[i].name[20] = 0;
        s->app[i].state = a[20]; s->app[i].missed = a[21]; s->app[i].total = t5_get16(a + 22);
        s->app[i].lsec = t5_get32(a + 24); s->app[i].lsub = t5_get32(a + 28);
    }
    for (i = 0; i < 16; i++)
    {
        const uint8 *h = p + 584 + i * 16;
        s->hist[i].idx = h[0]; s->hist[i].sec = t5_get32(h + 4); s->hist[i].sub = t5_get32(h + 8);
    }
    for (i = 0; i < 16; i++)
    {
        const uint8 *w = p + 840 + i * 12;
        s->watch[i].state = w[0]; s->watch[i].last = t5_get32(w + 4);
    }
    return true;
}

static const app_st_t *st_app(const st_t *s, const char *name)
{
    int i;
    for (i = 0; i < 16; i++) if (strcmp(s->app[i].name, name) == 0) return &s->app[i];
    return NULL;
}

static int st_app_index(const st_t *s, const char *name)
{
    int i;
    for (i = 0; i < 16; i++) if (strcmp(s->app[i].name, name) == 0) return i;
    return -1;
}

static int app_state(const char *name)
{
    st_t s;
    const app_st_t *a;
    if (!get_status(&s)) return -1;
    a = st_app(&s, name);
    return a ? a->state : -1;
}

static const char *ev_text(uint16 id, int from)
{
    const char *t = t5_last_ev_text(id, from);
    return t ? t : "";
}

/* Queue a message on the pipe that carries SFI's commands, whatever its MID (an unexpected message). */
static bool deliver_to_cmd_pipe(const uint8 *msg, size_t size)
{
    int k, slot;
    for (k = 0; k < T5_SUBS_MAX; k++)
    {
        if (t5_subs[k].used && t5_subs[k].mid == M_CMD)
        {
            t5_pipe_t *p = &t5_pipes[t5_subs[k].pipe];
            slot = t5_pool_next++ % T5_POOL;
            memset(t5_pool[slot], 0, T5_MSG_MAX);
            memcpy(t5_pool[slot], msg, size);
            p->q[(p->head + p->count) % T5_PIPE_Q] = slot;
            p->count++;
            return t5_kick();
        }
    }
    UtAssert_Failed("SFI did not subscribe to SFI_CMD_MID (0x18DC)");
    return false;
}

/* ===================================================================== tests */

static void Test_StartupAndHousekeeping(void)
{
    hk_t h;
    st_t s;
    int i, k;
    static const char *const apps[] = {"STOR", "CMDI", "TLMO", "GW", "SFI_T1", "SFI_T2", "SFI_T3", "MODE"};
    static const uint32 wmids[] = {M_STOR_HK, M_CMDI_HK, M_TLMO_HK, M_GW_HK, M_STALL, M_RATE, 0x18EF};
    UtAssert_True(boot_default(), "SFI_AppMain starts and waits on its pipe");
    UtAssert_True(t5_count_ev(EID_INIT, 0) == 1, "SFI_INIT_INF_EID (1) emitted once (saw %d)", t5_count_ev(EID_INIT, 0));
    UtAssert_True(t5_last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION");
    k = t5_tbl_find("SfiTable");
    UtAssert_True(k >= 0 && t5_tbl[k].owned, "the table is registered as SfiTable");
    if (k >= 0)
    {
        UtAssert_True(t5_tbl[k].size == TBL_SIZE, "SfiTable is %u bytes, expected %u", (unsigned)t5_tbl[k].size, TBL_SIZE);
        UtAssert_True(t5_tbl[k].validate != NULL, "a table validation function is registered");
    }
    UtAssert_True(t5_subscribed(M_CMD) && t5_subscribed(M_SEND_HK) && t5_subscribed(M_WAKEUP),
                  "subscribed to SFI_CMD_MID, SFI_SEND_HK_MID and SFI_WAKEUP_MID");
    UtAssert_True(t5_subscribed(M_HEARTBEAT) && t5_sub_msglim(M_HEARTBEAT) == 32,
                  "subscribed to SFI_HEARTBEAT_MID with message limit 32 (limit %u)", t5_sub_msglim(M_HEARTBEAT));
    for (i = 0; i < 7; i++)
        UtAssert_True(t5_subscribed(wmids[i]) && t5_sub_msglim(wmids[i]) == 8,
                      "subscribed to watch MID 0x%04X with message limit 8 (limit %u)", (unsigned)wmids[i],
                      t5_sub_msglim(wmids[i]));
    if (get_hk(&h))
    {
        UtAssert_True(h.cc == 0 && h.cec == 0, "counters start at 0");
        UtAssert_True(h.misslimit == 3 && h.window == 5, "HbMissLimit %u and ClusterWindowSec %u from the table (3, 5)",
                      h.misslimit, h.window);
        UtAssert_True(h.missing == 0 && h.infault == 0 && h.corr == CORR_NONE && h.recent == 0 && h.ccount == 0 &&
                      h.largest == 0, "no misses, faults or clusters after startup");
        UtAssert_True(h.total == 0 && h.unknown == 0 && h.missev == 0 && h.stall == 0 && h.rate == 0,
                      "heartbeat and fault counters start at 0");
    }
    UtAssert_True(cmd(CC_NOOP, NULL, 0) && t5_count_ev(EID_NOOP, 0) == 1, "NOOP emits event 3");
    UtAssert_True(t5_last_ev_type(EID_NOOP) == CFE_EVS_EventType_INFORMATION, "event 3 is INFORMATION");
    if (get_hk(&h)) UtAssert_True(h.cc == 1, "NOOP increments CommandCounter (%u)", h.cc);
    if (!get_status(&s)) return;
    for (i = 0; i < 8; i++)
    {
        const app_st_t *a = st_app(&s, apps[i]);
        UtAssert_True(a != NULL, "the status packet lists %s", apps[i]);
        if (a) UtAssert_True(a->state == APP_UNARMED && a->total == 0 && a->lsec == 0, "%s is UNARMED before any heartbeat", apps[i]);
    }
    k = 0;
    for (i = 0; i < 16; i++) if (s.app[i].name[0] == 0) k++;
    UtAssert_True(k == 8, "unused app slots carry an empty name (%d empty)", k);
    for (i = 0; i < 7; i++)
        UtAssert_True(s.watch[i].state == W_PENDING, "watch %d is BASELINE_PENDING after startup (state %u)", i, s.watch[i].state);
    for (i = 7; i < 16; i++)
        UtAssert_True(s.watch[i].state == W_UNUSED, "unused watch %d reports UNUSED (state %u)", i, s.watch[i].state);
    UtAssert_True(s.hcount == 0 && s.missing == 0 && s.corr == CORR_NONE, "empty history after startup");
}

static void Test_StartupErrors(void)
{
    t5_tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_TBLREG, 0) >= 1, "a table registration failure emits event 8");
    t5_app_end();

    sfi_setup();
    t5_tbl_load_rc = CFE_TBL_ERR_NO_ACCESS;
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_TBLLOAD, 0) >= 1, "a table load failure emits event 9");
    t5_app_end();

    sfi_setup();
    t5_sub_rc = CFE_SB_BAD_ARGUMENT;
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_SUB_ERR, 0) >= 1, "a subscription failure emits event 11");
    t5_app_end();

    sfi_setup();
    t5_recv_rc = CFE_SB_PIPE_RD_ERR;
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_PIPE_ERR, 0) >= 1, "a pipe read failure emits event 7");
}

static bool vtbl(const stbl_t *t) { return t5_tbl_validate("SfiTable", t->b) == CFE_SUCCESS; }

static void Test_TableValidation(void)
{
    stbl_t t, good;
    int ev0, i, k;
    if (!boot_default()) return;
    k = t5_tbl_find("SfiTable");
    if (k < 0 || t5_tbl[k].validate == NULL)
    {
        UtAssert_Failed("no validation function was registered for SfiTable");
        return;
    }
    stbl_default(&good);
    UtAssert_True(vtbl(&good), "the default table is accepted");
    ev0 = t5_nev;
    t = good; stbl_params(&t, 0, 5, 60);
    UtAssert_True(!vtbl(&t), "HbMissLimit 0 is rejected");
    UtAssert_True(t5_count_ev(EID_TBLVAL, ev0) >= 1, "a rejected table emits SFI_TBL_VAL_ERR_EID (10)");
    UtAssert_True(t5_last_ev_type(EID_TBLVAL) == CFE_EVS_EventType_ERROR, "event 10 is ERROR");
    t = good; stbl_params(&t, 61, 5, 60);
    UtAssert_True(!vtbl(&t), "HbMissLimit 61 is rejected");
    t = good; stbl_params(&t, 3, 0, 60);
    UtAssert_True(!vtbl(&t), "ClusterWindowSec 0 is rejected");
    t = good; stbl_params(&t, 3, 601, 60);
    UtAssert_True(!vtbl(&t), "ClusterWindowSec 601 is rejected");
    t = good; stbl_params(&t, 3, 5, 0);
    UtAssert_True(!vtbl(&t), "HistorySec 0 is rejected");
    t = good; stbl_params(&t, 3, 5, 3601);
    UtAssert_True(!vtbl(&t), "HistorySec 3601 is rejected");
    t = good; memset(t.b + 8 + 7 * 24, 'X', 20);
    UtAssert_True(!vtbl(&t), "an unterminated app name is rejected");
    t = good; stbl_app(&t, 9, "GW");
    UtAssert_True(!vtbl(&t), "a duplicate app name is rejected");
    t = good; stbl_watch(&t, 7, M_EXTRA, 16, 3, RULE_STALL, 2);
    UtAssert_True(!vtbl(&t), "a used watch of width 3 is rejected");
    t = good; stbl_watch(&t, 7, M_EXTRA, 16, 0, RULE_RATE, 2);
    UtAssert_True(!vtbl(&t), "a used watch of width 0 is rejected");
    t = good; stbl_watch(&t, 7, M_EXTRA, 16, 4, RULE_RATE, 0);
    UtAssert_True(!vtbl(&t), "a used watch with Limit 0 is rejected");
    t = good; stbl_watch(&t, 7, M_EXTRA, 1021, 4, RULE_STALL, 2);
    UtAssert_True(!vtbl(&t), "a used watch with ByteOffset + Width 1025 is rejected");

    t = good; stbl_params(&t, 1, 1, 1);
    UtAssert_True(vtbl(&t), "HbMissLimit 1, ClusterWindowSec 1 and HistorySec 1 are accepted");
    t = good; stbl_params(&t, 60, 600, 3600);
    UtAssert_True(vtbl(&t), "HbMissLimit 60, ClusterWindowSec 600 and HistorySec 3600 are accepted");
    t = good; stbl_watch(&t, 7, M_EXTRA, 1020, 4, RULE_STALL, 2);
    UtAssert_True(vtbl(&t), "a watch with ByteOffset + Width exactly 1024 is accepted");
    t = good; stbl_watch(&t, 7, M_EXTRA, 1023, 1, RULE_RATE, 1);
    UtAssert_True(vtbl(&t), "a width-1 watch at offset 1023 is accepted");
    t = good; stbl_watch(&t, 7, 0, 0, 3, 0, 0);
    UtAssert_True(vtbl(&t), "an unused watch (Rule UNUSED) is not checked");
    t = good;
    for (i = 0; i < 16; i++)
    {
        char n[24];
        snprintf(n, sizeof(n), "APP_%02d_XXXXXXXXXXXX", i);
        n[19] = 0;
        stbl_app(&t, i, n);
    }
    UtAssert_True(vtbl(&t), "16 distinct app names of 19 characters are accepted");
    t = good; memset(t.b + 8, 0, 16 * 24);
    for (i = 0; i < 4; i++) t.b[392+i*24+21] = 255;
    UtAssert_True(vtbl(&t), "a table with no apps is accepted");
}

static void Test_ArmingAndMiss(void)
{
    static const char *const gw[] = {"GW"};
    hk_t h;
    st_t s;
    const app_st_t *a;
    int ev0, i, gi;
    if (!boot_default()) return;
    if (!cycle_at(gw, 1, 2000000, 0)) return;
    if (get_hk(&h)) UtAssert_True(h.total == 1, "a heartbeat naming a table app counts in TotalHeartbeats (%u)", h.total);
    if (get_status(&s))
    {
        a = st_app(&s, "GW");
        UtAssert_True(a && a->state == APP_OK && a->missed == 0, "the first heartbeat arms GW: OK with MissedCycles 0");
        a = st_app(&s, "STOR");
        UtAssert_True(a && a->state == APP_UNARMED, "an app never heard stays UNARMED");
    }
    ev0 = t5_nev;
    cycle_at(NULL, 0, 2000001, 0);
    if (get_status(&s) && (a = st_app(&s, "GW")) != NULL)
        UtAssert_True(a->state == APP_OK && a->missed == 1, "one wakeup without a heartbeat: MissedCycles 1 (%u), still OK", a->missed);
    cycle_at(NULL, 0, 2000002, 0);
    if (get_status(&s) && (a = st_app(&s, "GW")) != NULL)
        UtAssert_True(a->state == APP_OK && a->missed == 2, "two wakeups without a heartbeat: MissedCycles 2 (%u), still OK", a->missed);
    UtAssert_True(t5_count_ev(EID_MISS, ev0) == 0, "no miss event before MissedCycles reaches HbMissLimit");
    cycle_at(NULL, 0, 2000003, 0x40000000);
    UtAssert_True(t5_count_ev(EID_MISS, ev0) == 1, "MissedCycles reaching HbMissLimit (3) emits SFI_HB_MISS_ERR_EID (12) once");
    UtAssert_True(t5_last_ev_type(EID_MISS) == CFE_EVS_EventType_ERROR, "event 12 is ERROR");
    UtAssert_True(t5_ev_has(EID_MISS, ev0, "GW", "2000003", NULL),
                  "event 12 names the app (GW) and the onset seconds (2000003): '%s'", ev_text(EID_MISS, ev0));
    if (get_hk(&h))
    {
        UtAssert_True(h.missing == 1, "AppsMissing is 1 (%u)", h.missing);
        UtAssert_True(h.missev == 1, "HbMissEvents is 1 (%u)", h.missev);
    }
    if (get_status(&s))
    {
        a = st_app(&s, "GW");
        gi = st_app_index(&s, "GW");
        UtAssert_True(a && a->state == APP_MISSING, "GW is MISSING");
        if (a)
        {
            UtAssert_True(a->total == 1, "TotalMisses of GW is 1 (%u)", a->total);
            UtAssert_True(a->lsec == 2000003 && a->lsub == 0x40000000,
                          "the onset is CFE_TIME_GetTime at the wakeup (seconds %lu, subseconds 0x%08lX)",
                          (unsigned long)a->lsec, (unsigned long)a->lsub);
        }
        UtAssert_True(s.missing == 1, "the status packet reports AppsMissing 1");
        UtAssert_True(s.hcount == 1, "the miss is appended to the history (HistoryCount %u)", s.hcount);
        UtAssert_True(s.hist[0].idx == gi && s.hist[0].sec == 2000003 && s.hist[0].sub == 0x40000000,
                      "the history record names GW's slot (%u, expected %d) and the onset time", s.hist[0].idx, gi);
    }
    for (i = 0; i < 4; i++) cycle(NULL, 0);
    UtAssert_True(t5_count_ev(EID_MISS, ev0) == 1, "no repeated miss events while GW stays MISSING");
    if (get_status(&s) && (a = st_app(&s, "GW")) != NULL)
        UtAssert_True(a->state == APP_MISSING && a->total == 1 && s.hcount == 1, "one miss recorded for one silence");
    UtAssert_True(app_state("STOR") == APP_UNARMED && app_state("SFI_T1") == APP_UNARMED,
                  "apps never heard are not evaluated (still UNARMED)");
}

static void Test_Restore(void)
{
    static const char *const gw[] = {"GW"};
    hk_t h, h0;
    st_t s;
    const app_st_t *a;
    int ev0, i;
    if (!boot_default()) return;
    cycle(gw, 1);
    for (i = 0; i < 3; i++) cycle(NULL, 0);
    if (app_state("GW") != APP_MISSING)
    {
        UtAssert_Failed("GW did not become MISSING after 3 wakeups without a heartbeat");
        return;
    }
    get_hk(&h0);
    ev0 = t5_nev;
    hb("GW", 9);
    UtAssert_True(t5_count_ev(EID_RESTORED, ev0) == 1, "a heartbeat from a MISSING app emits SFI_HB_RESTORED_INF_EID (13) at once");
    UtAssert_True(t5_last_ev_type(EID_RESTORED) == CFE_EVS_EventType_INFORMATION, "event 13 is INFORMATION");
    UtAssert_True(app_state("GW") == APP_OK, "the heartbeat makes GW OK at once, before the next wakeup");
    if (get_hk(&h))
    {
        UtAssert_True(h.missing == 0, "AppsMissing returns to 0 (%u)", h.missing);
        UtAssert_True(h.total == h0.total + 1, "the restoring heartbeat counts in TotalHeartbeats");
    }
    cycle(NULL, 0);
    if (get_status(&s) && (a = st_app(&s, "GW")) != NULL)
        UtAssert_True(a->missed == 0 && a->state == APP_OK, "the wakeup after the heartbeat resets MissedCycles to 0 (%u)", a->missed);
    cycle(NULL, 0);
    cycle(NULL, 0);
    cycle(NULL, 0);
    if (get_status(&s) && (a = st_app(&s, "GW")) != NULL)
    {
        UtAssert_True(a->state == APP_MISSING && a->total == 2, "a second silence gives a second miss (TotalMisses %u)", a->total);
        UtAssert_True(s.hcount == 2, "both misses are in the history (%u)", s.hcount);
    }
    if (get_hk(&h)) UtAssert_True(h.missev == 2, "HbMissEvents is 2 (%u)", h.missev);
    UtAssert_True(t5_count_ev(EID_MISS, 0) == 2, "one miss event per miss");
}

static void Test_UnknownHeartbeat(void)
{
    hk_t h;
    st_t s;
    int ev0, i;
    if (!boot_default()) return;
    ev0 = t5_nev;
    hb("NOT_AN_APP", 1);
    hb("GW2", 1);
    UtAssert_True(t5_count_ev(EID_UNKNOWN, ev0) == 2, "each heartbeat naming no table app emits SFI_HB_UNKNOWN_DBG_EID (14)");
    UtAssert_True(t5_last_ev_type(EID_UNKNOWN) == CFE_EVS_EventType_DEBUG, "event 14 is DEBUG");
    if (get_hk(&h))
    {
        UtAssert_True(h.unknown == 2, "UnknownHeartbeats is 2 (%u)", h.unknown);
        UtAssert_True(h.total == 0, "unknown heartbeats do not count in TotalHeartbeats (%u)", h.total);
    }
    for (i = 0; i < 5; i++) cycle(NULL, 0);
    if (get_status(&s))
    {
        int armed = 0;
        for (i = 0; i < 16; i++) if (s.app[i].state != APP_UNARMED) armed++;
        UtAssert_True(armed == 0 && s.hcount == 0, "unknown heartbeats arm nothing and cause no misses");
    }
    UtAssert_True(t5_count_ev(EID_MISS, 0) == 0, "no miss event");
}

static void Test_ParameterCommands(void)
{
    static const char *const stor[] = {"STOR"};
    hk_t h, h0;
    int ev0;
    if (!boot_default()) return;
    get_hk(&h0);
    ev0 = t5_nev;
    set_miss_limit(0);
    set_miss_limit(61);
    UtAssert_True(t5_count_ev(EID_PARAM_ERR, ev0) == 2, "SET_MISS_LIMIT 0 and 61 each emit SFI_SET_PARAM_ERR_EID (20)");
    UtAssert_True(t5_last_ev_type(EID_PARAM_ERR) == CFE_EVS_EventType_ERROR, "event 20 is ERROR");
    if (get_hk(&h))
    {
        UtAssert_True(h.misslimit == 3, "rejected values leave HbMissLimit at 3 (%u)", h.misslimit);
        UtAssert_True(h.cec == h0.cec + 2 && h.cc == h0.cc, "rejected values count as command errors only");
    }
    ev0 = t5_nev;
    set_miss_limit(60);
    UtAssert_True(t5_count_ev(EID_PARAM_OK, ev0) == 1, "SET_MISS_LIMIT 60 emits SFI_SET_PARAM_INF_EID (19)");
    UtAssert_True(t5_last_ev_type(EID_PARAM_OK) == CFE_EVS_EventType_INFORMATION, "event 19 is INFORMATION");
    if (get_hk(&h)) UtAssert_True(h.misslimit == 60, "HbMissLimit 60 (%u)", h.misslimit);
    set_miss_limit(1);
    if (get_hk(&h)) UtAssert_True(h.misslimit == 1 && h.cc == h0.cc + 2, "HbMissLimit 1, both accepted commands counted");
    cycle(stor, 1);
    cycle(NULL, 0);
    UtAssert_True(app_state("STOR") == APP_MISSING, "with HbMissLimit 1 one wakeup without a heartbeat makes STOR MISSING");

    ev0 = t5_nev;
    get_hk(&h0);
    set_window(0);
    set_window(601);
    UtAssert_True(t5_count_ev(EID_PARAM_ERR, ev0) == 2, "SET_CLUSTER_WINDOW 0 and 601 each emit event 20");
    if (get_hk(&h)) UtAssert_True(h.window == 5 && h.cec == h0.cec + 2, "rejected windows leave ClusterWindowSec at 5 (%u)", h.window);
    ev0 = t5_nev;
    set_window(600);
    if (get_hk(&h)) UtAssert_True(h.window == 600, "SET_CLUSTER_WINDOW 600 is accepted (%u)", h.window);
    set_window(1);
    if (get_hk(&h)) UtAssert_True(h.window == 1, "SET_CLUSTER_WINDOW 1 is accepted (%u)", h.window);
    UtAssert_True(t5_count_ev(EID_PARAM_OK, ev0) == 2, "each accepted window emits event 19");
}

/* A table with HbMissLimit 1, so that one wakeup without a heartbeat gives a miss. */
static void quick_table(stbl_t *t, uint16 w, uint16 h)
{
    stbl_default(t);
    stbl_params(t, 1, w, h);
}

static void Test_Clustering(void)
{
    static const char *const all4[] = {"SFI_T1", "SFI_T2", "SFI_T3", "STOR"};
    static const char *const t3stor[] = {"SFI_T3", "STOR"};
    static const char *const stor[] = {"STOR"};
    stbl_t t;
    hk_t h;
    st_t s;
    int ev0, i;
    quick_table(&t, 13, 600);
    if (!boot(&t)) return;
    cycle_at(all4, 4, 3000000, 0);
    ev0 = t5_nev;
    /* SFI_T1 and SFI_T2 fall silent together: two misses with equal onsets. */
    cycle_at(t3stor, 2, 3000001, 0);
    cycle_at(t3stor, 2, 3000002, 0);
    if (get_hk(&h))
    {
        UtAssert_True(h.recent == 2, "RecentMisses 2 (%u)", h.recent);
        UtAssert_True(h.ccount == 1 && h.largest == 2, "two misses with equal onsets form one cluster of 2 (count %u, largest %u)",
                      h.ccount, h.largest);
        UtAssert_True(h.corr == CORR_CLUST, "Correlation is CLUSTERED (%u)", h.corr);
    }
    UtAssert_True(t5_count_ev(EID_CLUSTER, ev0) == 1, "becoming CLUSTERED emits SFI_CLUSTER_ERR_EID (18) once (%d)",
                  t5_count_ev(EID_CLUSTER, ev0));
    UtAssert_True(t5_last_ev_type(EID_CLUSTER) == CFE_EVS_EventType_ERROR, "event 18 is ERROR");
    UtAssert_True(t5_ev_has(EID_CLUSTER, ev0, "2", "13", NULL), "event 18 names the cluster size (2) and W (13): '%s'",
                  ev_text(EID_CLUSTER, ev0));
    /* SFI_T3 falls silent 4 s later: the cluster grows to 3 while CLUSTERED. */
    cycle_at(stor, 1, 3000005, 0);
    cycle_at(stor, 1, 3000006, 0);
    if (get_hk(&h))
        UtAssert_True(h.ccount == 1 && h.largest == 3 && h.recent == 3, "a miss 4 s later (W 13) joins the cluster (largest %u)", h.largest);
    UtAssert_True(t5_count_ev(EID_CLUSTER, ev0) == 2, "LargestClusterSize growing while CLUSTERED emits event 18 again (%d)",
                  t5_count_ev(EID_CLUSTER, ev0));
    UtAssert_True(t5_ev_has(EID_CLUSTER, ev0, "3", "13", NULL), "the second event 18 names the size 3");
    /* STOR falls silent 20 s later: a separate cluster of 1. */
    cycle_at(stor, 1, 3000025, 0);
    cycle_at(NULL, 0, 3000026, 0);
    for (i = 0; i < 3; i++) cycle_at(NULL, 0, 3000027 + (uint32)i, 0);
    if (get_hk(&h))
        UtAssert_True(h.recent == 4 && h.ccount == 2 && h.largest == 3 && h.corr == CORR_CLUST,
                      "a miss more than W later starts a new cluster (count %u, largest %u, recent %u)", h.ccount, h.largest, h.recent);
    UtAssert_True(t5_count_ev(EID_CLUSTER, ev0) == 2, "no further event 18 while the largest cluster does not grow (%d)",
                  t5_count_ev(EID_CLUSTER, ev0));
    if (get_status(&s))
        UtAssert_True(s.corr == CORR_CLUST && s.ccount == 2 && s.largest == 3 && s.hcount == 4 && s.missing == 4,
                      "the status packet reports the same clusters, HistoryCount 4 and AppsMissing 4");
}

static void Test_UncorrelatedAndClear(void)
{
    static const char *const both[] = {"SFI_T1", "SFI_T2"};
    static const char *const t2[] = {"SFI_T2"};
    stbl_t t;
    hk_t h;
    st_t s;
    int ev0;
    quick_table(&t, 5, 600);
    if (!boot(&t)) return;
    cycle_at(both, 2, 4000000, 0);
    ev0 = t5_nev;
    cycle_at(t2, 1, 4000010, 0);                /* SFI_T1 misses at 4000010.0 */
    cycle_at(t2, 1, 4000014, 0);
    cycle_at(NULL, 0, 4000015, 0x10000000);     /* SFI_T2 misses 5 s and a fraction later */
    cycle_at(NULL, 0, 4000016, 0);
    if (get_hk(&h))
    {
        UtAssert_True(h.recent == 2 && h.ccount == 2 && h.largest == 1,
                      "misses 5 s plus a fraction apart (W 5) are separate clusters, subseconds included (count %u, largest %u)",
                      h.ccount, h.largest);
        UtAssert_True(h.corr == CORR_UNCOR, "Correlation is UNCORRELATED (%u)", h.corr);
    }
    UtAssert_True(t5_count_ev(EID_CLUSTER, ev0) == 0, "no event 18 without a cluster of 2");
    ev0 = t5_nev;
    cmd(CC_CLEAR_HISTORY, NULL, 0);
    UtAssert_True(t5_count_ev(EID_HIST_CLEAR, ev0) == 1, "CLEAR_HISTORY emits SFI_HISTORY_CLEAR_INF_EID (22)");
    UtAssert_True(t5_last_ev_type(EID_HIST_CLEAR) == CFE_EVS_EventType_INFORMATION, "event 22 is INFORMATION");
    if (get_hk(&h))
        UtAssert_True(h.recent == 0 && h.ccount == 0 && h.largest == 0 && h.corr == CORR_NONE,
                      "CLEAR_HISTORY empties the cluster metrics (recent %u, count %u, largest %u, correlation %u)",
                      h.recent, h.ccount, h.largest, h.corr);
    if (get_status(&s))
        UtAssert_True(s.hcount == 0 && s.ccount == 0 && s.largest == 0 && s.corr == CORR_NONE, "the status history is empty");
    cycle_at(NULL, 0, 4000017, 0);
    if (get_hk(&h)) UtAssert_True(h.recent == 0 && h.corr == CORR_NONE, "the cleared misses do not come back on the next wakeup");

    /* Restore both, then two misses exactly W apart: one cluster. */
    cycle_at(both, 2, 4000100, 0);
    cycle_at(t2, 1, 4000101, 0x20000000);       /* SFI_T1 misses at 4000101 + a fraction */
    cycle_at(NULL, 0, 4000106, 0x20000000);     /* SFI_T2 misses exactly 5 s later */
    cycle_at(NULL, 0, 4000107, 0);
    if (get_hk(&h))
        UtAssert_True(h.ccount == 1 && h.largest == 2 && h.corr == CORR_CLUST,
                      "misses exactly W apart form one cluster (count %u, largest %u)", h.ccount, h.largest);
}

static void Test_HistoryRing(void)
{
    static const char *const t1[] = {"SFI_T1"};
    stbl_t t;
    st_t s;
    const app_st_t *a;
    int i;
    const uint32 base = 5000000;
    quick_table(&t, 1, 3600);
    if (!boot(&t)) return;
    for (i = 0; i < 17; i++)
    {
        cycle_at(t1, 1, base + (uint32)i * 10, 0);                 /* heard (restores after a miss) */
        cycle_at(NULL, 0, base + (uint32)i * 10 + 5, (uint32)i);   /* miss number i at base + 10 i + 5 */
    }
    if (!get_status(&s)) return;
    UtAssert_True(s.hcount == 16, "the history holds SFI_HISTORY_LEN (16) entries after 17 misses (%u)", s.hcount);
    UtAssert_True(s.hist[0].sec == base + 15 && s.hist[0].sub == 1,
                  "the oldest entry was overwritten: entry 0 is the second miss (seconds %lu, expected %lu)",
                  (unsigned long)s.hist[0].sec, (unsigned long)(base + 15));
    UtAssert_True(s.hist[15].sec == base + 165 && s.hist[15].sub == 16, "entry 15 is the latest miss (seconds %lu)",
                  (unsigned long)s.hist[15].sec);
    for (i = 1; i < 16; i++)
    {
        if (s.hist[i].sec <= s.hist[i - 1].sec)
        {
            UtAssert_Failed("history entries are not oldest first at entry %d", i);
            break;
        }
    }
    a = st_app(&s, "SFI_T1");
    if (a != NULL) UtAssert_True(a->total == 17, "TotalMisses counts all 17 misses (%u)", a->total);
}

static void Test_RecentWindow(void)
{
    static const char *const t1[] = {"SFI_T1"};
    stbl_t t;
    hk_t h;
    st_t s;
    quick_table(&t, 5, 10);
    if (!boot(&t)) return;
    cycle_at(t1, 1, 6000000, 0);
    cycle_at(NULL, 0, 6000001, 0);              /* miss at 6000001 */
    cycle_at(NULL, 0, 6000011, 0);              /* exactly HistorySec (10 s) old */
    if (get_hk(&h))
        UtAssert_True(h.recent == 1 && h.corr == CORR_UNCOR, "a miss exactly HistorySec old is recent (RecentMisses %u)", h.recent);
    cycle_at(NULL, 0, 6000013, 0);
    if (get_hk(&h))
        UtAssert_True(h.recent == 0 && h.ccount == 0 && h.largest == 0 && h.corr == CORR_NONE,
                      "a miss older than HistorySec is no longer recent (RecentMisses %u, correlation %u)", h.recent, h.corr);
    if (get_status(&s)) UtAssert_True(s.hcount == 1, "the history keeps the old miss (HistoryCount %u)", s.hcount);
}

static void Test_StallWatch(void)
{
    hk_t h;
    st_t s;
    int ev0, i;
    if (!boot_default()) return;
    ev0 = t5_nev;
    watch_pkt(M_STALL, 24, 7, 4);
    if (get_status(&s))
        UtAssert_True(s.watch[4].state == W_OK && s.watch[4].last == 7,
                      "the first packet sets the baseline: watch OK with LastValue 7 (state %u, value %lu)",
                      s.watch[4].state, (unsigned long)s.watch[4].last);
    watch_pkt(M_STALL, 24, 7, 4);
    watch_pkt(M_STALL, 24, 7, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 0, "two unchanged values stay below the STALL Limit 3");
    watch_pkt(M_STALL, 24, 7, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 1, "the third unchanged value reaches Limit 3: SFI_STALL_ERR_EID (15)");
    UtAssert_True(t5_last_ev_type(EID_STALL) == CFE_EVS_EventType_ERROR, "event 15 is ERROR");
    UtAssert_True(t5_ev_has(EID_STALL, ev0, "0x18EC", "16", NULL), "event 15 names the MID (0x18EC) and the offset (16): '%s'",
                  ev_text(EID_STALL, ev0));
    if (get_hk(&h)) UtAssert_True(h.stall == 1 && h.infault == 1, "StallFaults 1, WatchesInFault 1 (%u, %u)", h.stall, h.infault);
    if (get_status(&s)) UtAssert_True(s.watch[4].state == W_FAULT, "the watch is in FAULT (%u)", s.watch[4].state);
    watch_pkt(M_STALL, 24, 7, 4);
    watch_pkt(M_STALL, 24, 7, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 1, "no repeated event 15 while in FAULT");
    if (get_hk(&h)) UtAssert_True(h.stall == 1, "StallFaults does not grow while in FAULT (%u)", h.stall);
    ev0 = t5_nev;
    watch_pkt(M_STALL, 24, 8, 4);
    UtAssert_True(t5_count_ev(EID_CLEAR, ev0) == 1, "a changed value clears the fault: SFI_WATCH_CLEAR_INF_EID (17)");
    UtAssert_True(t5_last_ev_type(EID_CLEAR) == CFE_EVS_EventType_INFORMATION, "event 17 is INFORMATION");
    if (get_status(&s))
        UtAssert_True(s.watch[4].state == W_OK && s.watch[4].last == 8, "the watch is OK with LastValue 8 (%u, %lu)",
                      s.watch[4].state, (unsigned long)s.watch[4].last);
    if (get_hk(&h)) UtAssert_True(h.infault == 0, "WatchesInFault 0 (%u)", h.infault);
    /* Packets too short for ByteOffset + Width do not update the watch. */
    watch_pkt(M_STALL, 24, 8, 4);
    watch_pkt(M_STALL, 24, 8, 4);
    for (i = 0; i < 3; i++) watch_pkt(M_STALL, 18, 8, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 0, "packets shorter than ByteOffset + Width (16 + 4) are not counted");
    watch_pkt(M_STALL, 24, 8, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 1, "the stall count restarts after a change: FAULT again at Limit");
    if (get_hk(&h)) UtAssert_True(h.stall == 2, "StallFaults 2 (%u)", h.stall);
}

static void Test_RateWatch(void)
{
    hk_t h;
    st_t s;
    int ev0;
    if (!boot_default()) return;
    ev0 = t5_nev;
    watch_pkt(M_RATE, 24, 10, 2);
    watch_pkt(M_RATE, 24, 14, 2);
    watch_pkt(M_RATE, 24, 15, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 0, "increases of 4 and 1 stay within the RATE Limit 5");
    watch_pkt(M_RATE, 24, 21, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 1, "an increase of 6 above Limit 5 emits SFI_RATE_ERR_EID (16)");
    UtAssert_True(t5_last_ev_type(EID_RATE) == CFE_EVS_EventType_ERROR, "event 16 is ERROR");
    UtAssert_True(t5_ev_has(EID_RATE, ev0, "0x18ED", "16", "6"), "event 16 names the MID, the offset and the delta 6: '%s'",
                  ev_text(EID_RATE, ev0));
    if (get_hk(&h)) UtAssert_True(h.rate == 1 && h.infault == 1, "RateFaults 1, WatchesInFault 1 (%u, %u)", h.rate, h.infault);
    if (get_status(&s)) UtAssert_True(s.watch[5].state == W_FAULT && s.watch[5].last == 21, "the watch is in FAULT with LastValue 21");
    watch_pkt(M_RATE, 24, 40, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 1, "no repeated event 16 while in FAULT");
    if (get_hk(&h)) UtAssert_True(h.rate == 1, "RateFaults does not grow while in FAULT (%u)", h.rate);
    ev0 = t5_nev;
    watch_pkt(M_RATE, 24, 45, 2);
    UtAssert_True(t5_count_ev(EID_CLEAR, ev0) == 1, "a delta equal to Limit (5) clears the fault with event 17");
    watch_pkt(M_RATE, 24, 3, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 0, "a counter reset to 3 is a delta of 3 (the new value), within Limit");
    watch_pkt(M_RATE, 24, 0x1234, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 1, "an increase of 4657 is a RATE fault");
    watch_pkt(M_RATE, 24, 0x1235, 2);
    UtAssert_True(t5_count_ev(EID_CLEAR, ev0) == 2, "an increase of 1 clears it");
    if (get_status(&s))
        UtAssert_True(s.watch[5].last == 0x1235, "a width-2 value is read in host byte order (LastValue 0x%lX)",
                      (unsigned long)s.watch[5].last);
    watch_pkt(M_RATE, 24, 20, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 2 && t5_ev_has(EID_RATE, ev0 + 0, "20", NULL, NULL),
                  "a counter reset to 20 is a delta of 20 (the new value), above Limit 5");
    if (get_hk(&h)) UtAssert_True(h.rate == 3, "RateFaults 3 (%u)", h.rate);

    /* A housekeeping watch: CMDI housekeeping CommandErrorCounter (byte 17, width 1, RATE 2). */
    ev0 = t5_nev;
    hk_tlm_pkt(M_CMDI_HK, 0);
    hk_tlm_pkt(M_CMDI_HK, 2);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 0, "CommandErrorCounter rising by 2 stays within Limit 2");
    hk_tlm_pkt(M_CMDI_HK, 5);
    UtAssert_True(t5_count_ev(EID_RATE, ev0) == 1 && t5_ev_has(EID_RATE, ev0, "0x08D0", "17", "3"),
                  "CommandErrorCounter rising by 3 in CMDI housekeeping (0x08D0, offset 17) is a RATE fault with delta 3: '%s'",
                  ev_text(EID_RATE, ev0));
    if (get_status(&s)) UtAssert_True(s.watch[1].state == W_FAULT && s.watch[1].last == 5, "watch 1 is in FAULT with LastValue 5");
}

static void Test_ResetCounters(void)
{
    static const char *const gw[] = {"GW"};
    hk_t pre, post;
    st_t s;
    int i, ev0;
    if (!boot_default()) return;
    cycle(gw, 1);
    for (i = 0; i < 3; i++) cycle(NULL, 0);
    hb("NOBODY", 1);
    for (i = 0; i < 4; i++) watch_pkt(M_STALL, 24, 1, 4);
    watch_pkt(M_RATE, 24, 1, 2);
    watch_pkt(M_RATE, 24, 100, 2);
    cmd(99, NULL, 0);
    cmd(CC_NOOP, NULL, 0);
    if (!get_hk(&pre)) return;
    UtAssert_True(pre.total >= 1 && pre.unknown == 1 && pre.missev == 1 && pre.stall == 1 && pre.rate == 1 && pre.cec >= 1 &&
                  pre.cc >= 1, "set-up: every counter is nonzero");
    ev0 = t5_nev;
    cmd(CC_RESET, NULL, 0);
    UtAssert_True(t5_count_ev(EID_RESET, ev0) == 1, "RESET_COUNTERS emits event 4");
    if (get_hk(&post))
    {
        UtAssert_True(post.cc == 0 && post.cec == 0, "command counters are zeroed (%u, %u)", post.cc, post.cec);
        UtAssert_True(post.total == 0 && post.unknown == 0 && post.missev == 0 && post.stall == 0 && post.rate == 0,
                      "TotalHeartbeats, UnknownHeartbeats, HbMissEvents, StallFaults and RateFaults are zeroed");
        UtAssert_True(post.missing == pre.missing && post.infault == pre.infault && post.recent == pre.recent &&
                      post.corr == pre.corr, "states and cluster metrics are kept (AppsMissing %u, WatchesInFault %u)",
                      post.missing, post.infault);
    }
    if (get_status(&s))
    {
        UtAssert_True(st_app(&s, "GW") != NULL && st_app(&s, "GW")->state == APP_MISSING, "GW stays MISSING");
        UtAssert_True(s.hcount == 3, "the history is kept (%u)", s.hcount);
        UtAssert_True(s.watch[4].state == W_FAULT && s.watch[5].state == W_FAULT, "watches stay in FAULT");
    }
}

static void Test_CommandErrors(void)
{
    hk_t h0, h;
    uint8 junk[8] = {0};
    uint8 msg[32];
    size_t n;
    int ev0;
    if (!boot_default()) return;
    get_hk(&h0);
    ev0 = t5_nev;
    cmd(99, NULL, 0);
    UtAssert_True(t5_count_ev(EID_CC_ERR, ev0) == 1, "an invalid function code emits SFI_CC_ERR_EID (2)");
    cmd(CC_NOOP, junk, 4);
    cmd(CC_SET_MISS, junk, 2);
    cmd(CC_SEND_STATUS, junk, 4);
    UtAssert_True(t5_count_ev(EID_LEN_ERR, ev0) == 3, "wrong command lengths each emit SFI_CMD_LEN_ERR_EID (6) (%d)",
                  t5_count_ev(EID_LEN_ERR, ev0));
    UtAssert_True(t5_count_ev(EID_NOOP, ev0) == 0 && t5_count_ev(EID_STATUS, ev0) == 0, "commands of the wrong length are not executed");
    if (get_hk(&h)) UtAssert_True(h.cec == h0.cec + 4 && h.cc == h0.cc, "each rejected command increments only CommandErrorCounter");
    n = t5_build_cmd(msg, M_FOREIGN, 0, NULL, 0);
    deliver_to_cmd_pipe(msg, n);
    UtAssert_True(t5_count_ev(EID_MID_ERR, ev0) == 1, "an unexpected message ID emits SFI_MID_ERR_EID (5)");
    if (get_hk(&h0))
        UtAssert_True(h0.cec == (uint8)(h.cec + 1) && h0.cc == h.cc,
                      "the unexpected message ID counts as one rejected command (CommandErrorCounter %u -> %u)", h.cec, h0.cec);
    cmd(CC_SEND_STATUS, NULL, 0);
    cmd(CC_CLEAR_HISTORY, NULL, 0);
    if (get_hk(&h))
        UtAssert_True(h.cc == (uint8)(h0.cc + 2) && h.cec == h0.cec,
                      "SEND_STATUS and CLEAR_HISTORY are accepted commands and each increment CommandCounter (%u -> %u)",
                      h0.cc, h.cc);
}

static void Test_TableUpdate(void)
{
    static const char *const gw[] = {"GW"};
    static const char *const both[] = {"GW", "STOR"};
    stbl_t t, bad;
    hk_t h;
    st_t s0, s;
    int i, ev0;
    if (!boot_default()) return;
    cycle(both, 2);
    cycle(gw, 1);
    cycle(gw, 1);
    cycle(gw, 1);                                             /* STOR misses */
    for (i = 0; i < 4; i++) watch_pkt(M_STALL, 24, 3, 4);    /* STALL watch in FAULT */
    set_miss_limit(9);
    set_window(77);
    if (!get_status(&s0)) return;
    UtAssert_True(s0.hcount == 2 && s0.watch[4].state == W_FAULT, "set-up: heartbeat and watch onsets in history, STALL watch in FAULT");
    stbl_default(&bad);
    stbl_params(&bad, 0, 5, 60);
    UtAssert_True(t5_tbl_submit("SfiTable", bad.b) != CFE_SUCCESS, "an invalid table update is rejected by validation");
    cycle(both, 2);
    if (get_hk(&h)) UtAssert_True(h.misslimit == 9, "a rejected table leaves the active parameters (HbMissLimit %u)", h.misslimit);

    stbl_default(&t);
    stbl_params(&t, 4, 7, 100);
    stbl_watch(&t, 6, M_EXTRA, 16, 1, RULE_STALL, 2);
    UtAssert_True(t5_tbl_submit("SfiTable", t.b) == CFE_SUCCESS, "a valid table update passes validation");
    t5_send_cmd(M_SEND_HK, 0, NULL, 0);
    cycle(NULL, 0);
    t5_send_cmd(M_SEND_HK, 0, NULL, 0);
    if (get_hk(&h))
        UtAssert_True(h.misslimit == 4 && h.window == 7, "the table load overrides SET_MISS_LIMIT and SET_CLUSTER_WINDOW (%u, %u)",
                      h.misslimit, h.window);
    if (!get_status(&s)) return;
    for (i = 0; i < 16; i++)
    {
        if (s.app[i].name[0] != 0 && s.app[i].state != APP_UNARMED)
        {
            UtAssert_Failed("after a table load %s is in state %u, expected UNARMED", s.app[i].name, s.app[i].state);
            break;
        }
    }
    UtAssert_True(s.missing == 0, "no app is MISSING after a table load (%u)", s.missing);
    UtAssert_True(s.watch[4].state == W_PENDING && s.watch[6].state == W_PENDING,
                  "used watches are BASELINE_PENDING after a table load (states %u, %u)", s.watch[4].state, s.watch[6].state);
    UtAssert_True(s.hcount == s0.hcount, "the history is kept across a table load (%u, was %u)", s.hcount, s0.hcount);
    ev0 = t5_nev;
    for (i = 0; i < 3; i++) watch_pkt(M_STALL, 24, 3, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 0, "after the load the first packet is a new baseline: two unchanged values "
                  "stay below Limit 3");
    watch_pkt(M_STALL, 24, 3, 4);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 1 && t5_ev_has(EID_STALL, ev0, "0x18EC", NULL, NULL),
                  "the reloaded watch evaluates afresh: three unchanged values after the new baseline reach Limit 3");
    ev0 = t5_nev;
    watch_pkt(M_EXTRA, 24, 5, 1);
    watch_pkt(M_EXTRA, 24, 5, 1);
    watch_pkt(M_EXTRA, 24, 5, 1);
    UtAssert_True(t5_count_ev(EID_STALL, ev0) == 1 && t5_ev_has(EID_STALL, ev0, "0x18EE", NULL, NULL),
                  "a watch added by the table update (0x18EE, STALL 2) is subscribed and evaluated");
    for (i = 0; i < 6; i++) cycle(NULL, 0);
    UtAssert_True(t5_count_ev(EID_MISS, ev0) == 0, "apps unarmed by the table load are not evaluated until heard again");
}

#include "sfi_expanded_cases.h"

void UtTest_Setup(void)
{
    register_expanded_cases();
    UtAssert_True(sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes",
                  (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    UtAssert_True(sizeof(CFE_MSG_CommandHeader_t) == 8, "command header is 8 bytes");
    UtTest_Add(Test_StartupAndHousekeeping, sfi_setup, t5_teardown, "Startup and housekeeping");
    UtTest_Add(Test_StartupErrors, sfi_setup, t5_teardown, "Startup error events");
    UtTest_Add(Test_TableValidation, sfi_setup, t5_teardown, "Table validation");
    UtTest_Add(Test_ArmingAndMiss, sfi_setup, t5_teardown, "Heartbeat arming and miss");
    UtTest_Add(Test_Restore, sfi_setup, t5_teardown, "Heartbeat restore");
    UtTest_Add(Test_UnknownHeartbeat, sfi_setup, t5_teardown, "Unknown heartbeats");
    UtTest_Add(Test_ParameterCommands, sfi_setup, t5_teardown, "Parameter commands");
    UtTest_Add(Test_Clustering, sfi_setup, t5_teardown, "Clustered misses");
    UtTest_Add(Test_UncorrelatedAndClear, sfi_setup, t5_teardown, "Uncorrelated misses and history clear");
    UtTest_Add(Test_HistoryRing, sfi_setup, t5_teardown, "Miss history ring");
    UtTest_Add(Test_RecentWindow, sfi_setup, t5_teardown, "Recent miss window");
    UtTest_Add(Test_StallWatch, sfi_setup, t5_teardown, "STALL watch");
    UtTest_Add(Test_RateWatch, sfi_setup, t5_teardown, "RATE watch");
    UtTest_Add(Test_ResetCounters, sfi_setup, t5_teardown, "Reset counters");
    UtTest_Add(Test_CommandErrors, sfi_setup, t5_teardown, "Command errors");
    UtTest_Add(Test_TableUpdate, sfi_setup, t5_teardown, "Table update");
}
