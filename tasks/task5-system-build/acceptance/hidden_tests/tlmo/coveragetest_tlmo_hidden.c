/*
 * Black-box behaviour test of the TLMO prioritised telemetry output application (spec S4, C.3 to C.5).
 *
 * TLMO_AppMain runs on its own thread against the behaviour model of t5_model.h: stream packets
 * and commands are software bus messages, the PriorityTable image comes from the test, datagrams
 * are captured from OS_SocketSendTo. Only the names, layouts, message IDs, function codes and event
 * IDs of the specification are used; structures are decoded by offset.
 *
 * Whether TLMO reads its stream pipe before or after it drains on a WAKEUP is not pinned, so every
 * sequence enqueues with the budget at 0 (or sends one more WAKEUP) before it counts datagrams.
 */
#include "t5_model.h"

extern void TLMO_AppMain(void);

/* ------------------------------------------------------------------------- spec constants (cpu1) */

#define MID_CMD     0x18D4u
#define MID_SEND_HK 0x18D5u
#define MID_WAKEUP  0x18D6u
#define MID_HK      0x08D4u
#define MID_CLASS   0x08D5u
#define MID_HB      0x18DFu
#define MID_UNUSED  0x18D7u /* a command MID TLMO does not subscribe to */
#define V0          0x18E8u /* verification streams of the default table, classes 0..3 */
#define V1          0x18E9u
#define V2          0x18EAu
#define V3          0x18EBu
#define VC          0x18ECu
#define VE          0x18EEu
#define VF          0x18EFu

#define CC_NOOP   0
#define CC_RESET  1
#define CC_CLASS  2
#define CC_BUDGET 3
#define CC_MAP    4
#define CC_ADD    5
#define CC_REMOVE 6

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
#define EID_SOCKET_ERR 12
#define EID_CLASS_INF  13
#define EID_CLASS_ERR  14
#define EID_BUDGET_INF 15
#define EID_BUDGET_ERR 16
#define EID_DROP       17
#define EID_MAP_INF    18
#define EID_TBL_UPDATE 19
#define EID_ADD_INF    20
#define EID_ADD_ERR    21
#define EID_REM_INF    22
#define EID_REM_ERR    23

#define TBL_NAME      "PriorityTable"
#define TBL_SIZE      516
#define MAX_STREAMS   64
#define HK_PAYLOAD    52
#define MAP_PAYLOAD   516
#define TLM_HDR       16
#define PKT           40
#define TLM_PORT_BASE 2234

/* ------------------------------------------------------------------------- table images */

typedef struct { uint8 b[TBL_SIZE]; } ptbl_t;

static void ptbl_init(ptbl_t *t, uint16 budget, uint16 cap)
{
    memset(t, 0, sizeof(*t));
    t5_put16(t->b, budget);
    t5_put16(t->b + 2, cap);
}

static void ptbl_set(ptbl_t *t, int slot, uint32 mid, uint8 cls)
{
    uint8 *e = t->b + 4 + slot * 8;
    t5_put32(e, mid);
    e[4] = cls;
    e[5] = e[6] = e[7] = 0;
}

/* The four verification streams in slots 0..3, classes 0..3. */
static void ptbl_quad(ptbl_t *t, uint16 budget, uint16 cap)
{
    ptbl_init(t, budget, cap);
    ptbl_set(t, 0, V0, 0);
    ptbl_set(t, 1, V1, 1);
    ptbl_set(t, 2, V2, 2);
    ptbl_set(t, 3, V3, 3);
}

/* The default table of S4.1 on cpu1 (48 streams with SBN). */
static const uint32 default_streams[48][2] = {
    {0x0808, 0}, {0x08DC, 0}, {0x18E8, 0},
    {0x0800, 1}, {0x0801, 1}, {0x0803, 1}, {0x0804, 1}, {0x08C4, 1}, {0x08D0, 1}, {0x08D4, 1}, {0x08D8, 1},
    {0x08B0, 1}, {0x0890, 1}, {0x08AD, 1}, {0x089B, 1}, {0x08AA, 1}, {0x088A, 1}, {0x08B8, 1}, {0x08A7, 1},
    {0x08A4, 1}, {0x18E9, 1},
    {0x0805, 2}, {0x0883, 2}, {0x0806, 2}, {0x080A, 2}, {0x080C, 2}, {0x080B, 2}, {0x0810, 2}, {0x08B3, 2},
    {0x088B, 2}, {0x088C, 2}, {0x088D, 2}, {0x088E, 2}, {0x08C5, 2}, {0x08C6, 2}, {0x08D5, 2}, {0x08D9, 2},
    {0x08DD, 2}, {0x18EA, 2},
    {0x08C2, 3}, {0x08C3, 3}, {0x08C7, 3}, {0x18EB, 3},
    {0x08FB, 1}, {0x08FC, 1}, {0x08FD, 1}, {0x08FE, 1}, {0x08FF, 1}};

static void ptbl_default(ptbl_t *t)
{
    int i;
    ptbl_init(t, 16, 64);
    for (i = 0; i < 48; i++) ptbl_set(t, i, default_streams[i][0], (uint8)default_streams[i][1]);
}

/* ------------------------------------------------------------------------- subscription fault injection */

static uint32 fail_sub_mid; /* subscribing to this MID fails */
static char   with_limit;   /* marker: the stub carries a MsgLim argument */

static void H_SubscribeFault(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    if (fail_sub_mid != 0 && mid == fail_sub_mid)
    {
        t5_n_subscribe++;
        t5_ret(key, CFE_SB_BAD_ARGUMENT);
        return;
    }
    t5_subscribe(key, c, ud == &with_limit ? UT_Hook_GetArgValueByName(c, "MsgLim", uint16) : 4);
}

/* ------------------------------------------------------------------------- fixture */

static void Setup(void)
{
    t5_setup();
    t5_app_entry = TLMO_AppMain;
    fail_sub_mid = 0;
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Subscribe), H_SubscribeFault, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeEx), H_SubscribeFault, &with_limit);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeLocal), H_SubscribeFault, &with_limit);
}

static void Teardown(void)
{
    t5_teardown();
}

static bool wakeup(void)
{
    uint32 before = t5_now_sub;
    t5_now_sub += 429496730u; /* 0.1 s */
    if (t5_now_sub < before) t5_now_sec++;
    return t5_send_cmd(MID_WAKEUP, 0, NULL, 0);
}

static bool wakeups(int n)
{
    int i;
    for (i = 0; i < n; i++)
        if (!wakeup()) return false;
    return true;
}

/* Boot TLMO with `t` as /cf/tlmo_tbl.tbl; one WAKEUP lets deferred start-up work complete. */
static bool boot(const ptbl_t *t)
{
    t5_tbl_set_file(TBL_NAME, t->b, TBL_SIZE);
    if (!t5_app_start()) return false;
    return wakeup();
}

static bool cmd0(int cc) { return t5_send_cmd(MID_CMD, cc, NULL, 0); }

static bool cmd_class(uint32 mid, uint8 cls)
{
    uint8 p[8] = {0};
    t5_put32(p, mid);
    p[4] = cls;
    return t5_send_cmd(MID_CMD, CC_CLASS, p, sizeof(p));
}

static bool cmd_budget(uint16 n)
{
    uint8 p[4] = {0};
    t5_put16(p, n);
    return t5_send_cmd(MID_CMD, CC_BUDGET, p, sizeof(p));
}

static bool cmd_add(uint32 mid, uint8 cls)
{
    uint8 p[8] = {0};
    t5_put32(p, mid);
    p[4] = cls;
    return t5_send_cmd(MID_CMD, CC_ADD, p, sizeof(p));
}

static bool cmd_remove(uint32 mid)
{
    uint8 p[4] = {0};
    t5_put32(p, mid);
    return t5_send_cmd(MID_CMD, CC_REMOVE, p, sizeof(p));
}

/* ------------------------------------------------------------------------- stream packets */

static uint8 pkt_buf[T5_MSG_MAX] __attribute__((aligned(16)));

static bool inj_size(uint32 mid, uint32 val, size_t size)
{
    size_t n = t5_build_verif(pkt_buf, mid, size, val, 4);
    return t5_deliver(pkt_buf, n);
}

static bool inj(uint32 mid, uint32 val) { return inj_size(mid, val, PKT); }

/* ------------------------------------------------------------------------- datagrams */

#define SD_MAX 160

typedef struct { uint32 mid; uint32 val; } sd_t;

static int sent_get(int from, sd_t *out, int max)
{
    int i, n = 0;
    for (i = from; i < t5_nsent && n < max; i++)
    {
        const uint8 *b = t5_sent[i].bytes;
        out[n].mid = t5_sent[i].size >= 2 ? t5_be16(b) : 0;
        out[n].val = t5_sent[i].size >= 20 ? t5_get32(b + 16) : 0;
        n++;
    }
    return n;
}

static void fmt_list(char *buf, size_t n, const sd_t *l, int count)
{
    size_t at = 0;
    int i;
    buf[0] = 0;
    for (i = 0; i < count && at + 16 < n; i++)
        at += (size_t)snprintf(buf + at, n - at, "%s%04X:%u", i ? " " : "", (unsigned)l[i].mid, (unsigned)l[i].val);
    if (i < count && at + 4 < n) snprintf(buf + at, n - at, " ...");
}

/* The datagrams sent since `from` are exactly `exp`, in this order. */
static void expect_sent(int from, const sd_t *exp, int n, const char *what)
{
    sd_t got[SD_MAX];
    char a[600], e[600];
    int m = sent_get(from, got, SD_MAX), i;
    bool same = (m == n);
    for (i = 0; same && i < n; i++) same = got[i].mid == exp[i].mid && got[i].val == exp[i].val;
    fmt_list(a, sizeof(a), got, m);
    fmt_list(e, sizeof(e), exp, n);
    UtAssert_True(same, "%s: datagrams [%s], expected [%s] (MID:value at packet byte 16)", what, a, e);
}

/* Every datagram since `from` goes to 127.0.0.1:2234+node-1 and, for verification packets, carries the
 * packet bytes unchanged. */
static void expect_intact(int from)
{
    static uint8 want[T5_MSG_MAX];
    int i;
    uint16 port = (uint16)(TLM_PORT_BASE + t5_node_id - 1);
    for (i = from; i < t5_nsent; i++)
    {
        uint32 mid;
        if (t5_sent[i].port != port || strcmp(t5_sent[i].ip, "127.0.0.1") != 0)
        {
            UtAssert_Failed("telemetry datagram sent to %s:%u, expected 127.0.0.1:%u", t5_sent[i].ip,
                            (unsigned)t5_sent[i].port, (unsigned)port);
            return;
        }
        mid = t5_sent[i].size >= 2 ? t5_be16(t5_sent[i].bytes) : 0;
        if (mid >= 0x18E0 && mid <= 0x18EF && t5_sent[i].size >= 20)
        {
            t5_build_verif(want, mid, t5_sent[i].size, t5_get32(t5_sent[i].bytes + 16), 4);
            if (memcmp(want, t5_sent[i].bytes, t5_sent[i].size) != 0)
            {
                UtAssert_Failed("datagram of MID 0x%04X is not the packet bytes unchanged", (unsigned)mid);
                return;
            }
        }
    }
}

/* ------------------------------------------------------------------------- telemetry readers */

typedef struct
{
    uint8  cc, cec;
    uint16 budget, queued, cap, active;
    uint32 rx, sent[4], drop[4], sockerr;
} hk_t;

static bool get_hk(hk_t *h)
{
    int from = t5_ntx, i;
    const t5_tx_t *t;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!t5_send_cmd(MID_SEND_HK, 0, NULL, 0)) return false;
    t = t5_last_tx(MID_HK, from);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_HK did not transmit a TLMO_HkTlm_t on TLMO_HK_TLM_MID (0x08D4)");
        return false;
    }
    if (t->size != TLM_HDR + HK_PAYLOAD)
    {
        UtAssert_Failed("housekeeping packet is %u bytes, expected %u", (unsigned)t->size, TLM_HDR + HK_PAYLOAD);
        if (t->size < TLM_HDR + HK_PAYLOAD) return false;
    }
    p = t->bytes + TLM_HDR;
    h->cc = p[0];
    h->cec = p[1];
    h->budget = t5_get16(p + 2);
    h->queued = t5_get16(p + 4);
    h->cap = t5_get16(p + 6);
    h->active = t5_get16(p + 8);
    h->rx = t5_get32(p + 12);
    for (i = 0; i < 4; i++)
    {
        h->sent[i] = t5_get32(p + 16 + 4 * i);
        h->drop[i] = t5_get32(p + 32 + 4 * i);
    }
    h->sockerr = t5_get32(p + 48);
    return true;
}

typedef struct
{
    uint16 budget, count;
    uint32 mid[MAX_STREAMS];
    uint8  cls[MAX_STREAMS];
} cmap_t;

static bool get_map(cmap_t *m)
{
    int from = t5_ntx, ev0 = t5_nev, i;
    const t5_tx_t *t;
    const uint8 *p;
    memset(m, 0, sizeof(*m));
    if (!cmd0(CC_MAP)) return false;
    t = t5_last_tx(MID_CLASS, from);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_CLASS_MAP did not transmit a TLMO_ClassTlm_t on TLMO_CLASS_TLM_MID (0x08D5)");
        return false;
    }
    UtAssert_True(t5_count_ev(EID_MAP_INF, ev0) == 1, "SEND_CLASS_MAP emits TLMO_CLASS_MAP_INF_EID (18)");
    if (t->size != TLM_HDR + MAP_PAYLOAD)
    {
        UtAssert_Failed("class map packet is %u bytes, expected %u", (unsigned)t->size, TLM_HDR + MAP_PAYLOAD);
        if (t->size < TLM_HDR + MAP_PAYLOAD) return false;
    }
    p = t->bytes + TLM_HDR;
    m->budget = t5_get16(p);
    m->count = t5_get16(p + 2);
    for (i = 0; i < MAX_STREAMS; i++)
    {
        m->mid[i] = t5_get32(p + 4 + 8 * i);
        m->cls[i] = p[8 + 8 * i];
    }
    return true;
}

/* The class map equals the table image `t` in every slot (unused slots invalid). */
static bool map_equals(const cmap_t *m, const ptbl_t *t, char *why, size_t n)
{
    int i;
    for (i = 0; i < MAX_STREAMS; i++)
    {
        uint32 mid = t5_get32(t->b + 4 + 8 * i);
        uint8 cls = t->b[8 + 8 * i];
        if (m->mid[i] != mid || (mid != 0 && m->cls[i] != cls))
        {
            snprintf(why, n, "slot %d holds MID 0x%04X class %u, expected MID 0x%04X class %u", i, (unsigned)m->mid[i],
                     m->cls[i], (unsigned)mid, cls);
            return false;
        }
    }
    return true;
}

static int map_slot_of(const cmap_t *m, uint32 mid)
{
    int i;
    for (i = 0; i < MAX_STREAMS; i++) if (m->mid[i] == mid) return i;
    return -1;
}

/* Depth of the pipe that holds the subscription of `mid` (-1 when not subscribed). */
static int pipe_depth_of(uint32 mid)
{
    int k;
    for (k = 0; k < T5_SUBS_MAX; k++)
        if (t5_subs[k].used && t5_subs[k].mid == mid) return t5_pipes[t5_subs[k].pipe].depth;
    return -1;
}

/* Put a message straight on the pipe that holds the subscription of `sub_mid` (as SB would when it
 * routes a message there), then let TLMO run. */
static bool deliver_to_pipe_of(uint32 sub_mid, const uint8 *msg, size_t size)
{
    int k, p = -1, slot;
    for (k = 0; k < T5_SUBS_MAX; k++)
        if (t5_subs[k].used && t5_subs[k].mid == sub_mid) { p = t5_subs[k].pipe; break; }
    if (p < 0)
    {
        UtAssert_Failed("TLMO is not subscribed to its command MID 0x%04X", (unsigned)sub_mid);
        return false;
    }
    slot = t5_pool_next++ % T5_POOL;
    memset(t5_pool[slot], 0, T5_MSG_MAX);
    memcpy(t5_pool[slot], msg, size);
    t5_pipes[p].q[(t5_pipes[p].head + t5_pipes[p].count) % T5_PIPE_Q] = slot;
    t5_pipes[p].count++;
    return t5_kick();
}

static bool ev_names_class(int from, int cls)
{
    char d[4];
    snprintf(d, sizeof(d), "%d", cls);
    return t5_ev_has(EID_DROP, from, d, NULL, NULL);
}

static uint32 drops(const hk_t *h) { return h->drop[0] + h->drop[1] + h->drop[2] + h->drop[3]; }

/* ===================================================================== tests */

static void Test_Startup(void)
{
    ptbl_t t;
    hk_t h;
    int i, k, missing = 0, badlim = 0, depth;
    ptbl_default(&t);
    UtAssert_True(boot(&t), "TLMO_AppMain starts and waits on its pipe");
    UtAssert_True(t5_count_ev(EID_INIT, 0) == 1, "TLMO_INIT_INF_EID (1) emitted once, saw %d", t5_count_ev(EID_INIT, 0));
    UtAssert_True(t5_last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION");
    k = t5_tbl_find(TBL_NAME);
    UtAssert_True(k >= 0, "the table is registered as PriorityTable");
    if (k >= 0)
    {
        UtAssert_True(t5_tbl[k].size == TBL_SIZE, "PriorityTable is %u bytes, the specified layout is %u",
                      (unsigned)t5_tbl[k].size, TBL_SIZE);
        UtAssert_True(t5_tbl[k].validate != NULL, "a validation function is registered with PriorityTable");
    }
    UtAssert_True(t5_subscribed(MID_CMD) && t5_subscribed(MID_SEND_HK) && t5_subscribed(MID_WAKEUP),
                  "subscribed to TLMO_CMD_MID, TLMO_SEND_HK_MID and TLMO_WAKEUP_MID");
    for (i = 0; i < 48; i++)
    {
        if (!t5_subscribed(default_streams[i][0])) missing++;
        else if (t5_sub_msglim(default_streams[i][0]) != 32) badlim++;
    }
    UtAssert_True(missing == 0, "subscribed to every used stream of the table (%d of 48 missing)", missing);
    UtAssert_True(badlim == 0, "every stream subscription has a message limit of 32 (%d differ)", badlim);
    depth = pipe_depth_of(0x0800);
    UtAssert_True(depth == 48, "the streams are subscribed on a pipe of depth 48 (depth %d)", depth);
    UtAssert_True(t5_count_ev(EID_TBL_UPDATE, 0) == 0, "no TLMO_TBL_UPDATE_INF_EID (19) on the initial table load");
    if (get_hk(&h))
    {
        UtAssert_True(h.budget == 16 && h.cap == 64 && h.active == 48,
                      "housekeeping BudgetPacketsPerCycle %u, QueueCapacity %u, ActiveStreams %u (expected 16, 64, 48)",
                      h.budget, h.cap, h.active);
        UtAssert_True(h.cc == 0 && h.cec == 0 && h.queued == 0 && h.rx == 0 && h.sockerr == 0,
                      "counters are zero after start-up (CommandCounter %u, CommandErrorCounter %u, QueuedPackets %u, "
                      "PacketsReceived %u)", h.cc, h.cec, h.queued, (unsigned)h.rx);
    }
    k = t5_nev;
    UtAssert_True(cmd0(CC_NOOP) && t5_count_ev(EID_NOOP, k) == 1, "NOOP emits TLMO_NOOP_INF_EID (3)");
    UtAssert_True(t5_last_ev_type(EID_NOOP) == CFE_EVS_EventType_INFORMATION, "event 3 is INFORMATION");
    if (get_hk(&h)) UtAssert_True(h.cc == 1, "CommandCounter %u after one NOOP, expected 1", h.cc);
}

static void Test_Output(void)
{
    ptbl_t t;
    uint8 es[60];
    size_t n;
    int from, i;
    bool intact = false;
    ptbl_default(&t);
    if (!boot(&t)) return;
    from = t5_nsent;
    n = t5_build_tlm(es, 0x0800, sizeof(es), 9);
    t5_deliver(es, n);
    inj(V2, 7);
    wakeups(2);
    {
        sd_t exp[2] = {{0x0800, 9}, {V2, 7}};
        expect_sent(from, exp, 2, "with no enable command, a class 1 and a class 2 stream packet are sent");
    }
    expect_intact(from);
    for (i = from; i < t5_nsent; i++)
        if (t5_be16(t5_sent[i].bytes) == 0x0800 && t5_sent[i].size == n && memcmp(t5_sent[i].bytes, es, n) == 0)
            intact = true;
    UtAssert_True(intact, "a telemetry packet is sent as one datagram with its bytes unchanged");

    /* Node 2: port 2234 + node ID - 1. */
    t5_app_end();
    Setup();
    t5_node_id = 2;
    ptbl_quad(&t, 16, 64);
    if (!boot(&t)) return;
    from = t5_nsent;
    inj(V0, 1);
    wakeups(2);
    UtAssert_True(t5_nsent - from == 1, "one datagram for one packet on node 2 (%d)", t5_nsent - from);
    if (t5_nsent > from)
        UtAssert_True(t5_sent[from].port == 2235, "on node 2 telemetry goes to UDP 2235 (port %u)",
                      (unsigned)t5_sent[from].port);
    expect_intact(from);
}

static void Test_DrainOrder(void)
{
    ptbl_t t;
    hk_t h;
    int from;
    ptbl_quad(&t, 0, 8);
    if (!boot(&t)) return;
    from = t5_nsent;
    inj(V3, 3001);
    inj(V1, 1001);
    inj(V2, 2001);
    inj(V0, 1);
    inj(V3, 3002);
    inj(V2, 2002);
    inj(V1, 1002);
    inj(V0, 2);
    wakeups(2);
    UtAssert_True(t5_nsent == from, "budget 0 sends nothing (%d datagrams)", t5_nsent - from);
    if (get_hk(&h))
        UtAssert_True(h.queued == 8 && h.rx == 8 && h.budget == 0,
                      "QueuedPackets %u, PacketsReceived %u, BudgetPacketsPerCycle %u after 8 packets with budget 0",
                      h.queued, (unsigned)h.rx, h.budget);
    cmd_budget(3);
    from = t5_nsent;
    wakeup();
    {
        sd_t e1[3] = {{V0, 1}, {V0, 2}, {V1, 1001}};
        expect_sent(from, e1, 3, "first WAKEUP with budget 3: oldest packets of the highest-priority class first");
    }
    from = t5_nsent;
    wakeup();
    {
        sd_t e2[3] = {{V1, 1002}, {V2, 2001}, {V2, 2002}};
        expect_sent(from, e2, 3, "second WAKEUP with budget 3");
    }
    from = t5_nsent;
    wakeup();
    {
        sd_t e3[2] = {{V3, 3001}, {V3, 3002}};
        expect_sent(from, e3, 2, "third WAKEUP with budget 3");
    }
    from = t5_nsent;
    wakeup();
    UtAssert_True(t5_nsent == from, "nothing is sent once the queue is empty");
    if (get_hk(&h))
        UtAssert_True(h.sent[0] == 2 && h.sent[1] == 2 && h.sent[2] == 2 && h.sent[3] == 2 && h.queued == 0,
                      "SentByClass %u/%u/%u/%u, QueuedPackets %u (expected 2/2/2/2, 0)", (unsigned)h.sent[0],
                      (unsigned)h.sent[1], (unsigned)h.sent[2], (unsigned)h.sent[3], h.queued);

    /* A higher-priority packet enqueued later goes ahead of older lower-priority ones. */
    cmd_budget(0);
    inj(V2, 2003);
    inj(V2, 2004);
    wakeup();
    cmd_budget(1);
    from = t5_nsent;
    wakeup();
    cmd_budget(0);
    inj(V0, 3);
    wakeup();
    cmd_budget(1);
    wakeups(2);
    {
        sd_t e4[3] = {{V2, 2003}, {V0, 3}, {V2, 2004}};
        expect_sent(from, e4, 3, "with budget 1, a class 0 packet enqueued later is sent before the remaining class 2 packet");
    }
}

static void Test_Eviction(void)
{
    ptbl_t t;
    hk_t h;
    int from, i;
    ptbl_quad(&t, 0, 8);
    if (!boot(&t)) return;
    from = t5_nsent;
    for (i = 1; i <= 6; i++) inj(V3, 300 + (uint32)i);
    wakeup();
    for (i = 1; i <= 4; i++) inj(V2, 200 + (uint32)i);
    wakeup();
    for (i = 1; i <= 5; i++) inj(V1, 100 + (uint32)i);
    wakeup();
    inj(V0, 1);
    inj(V0, 2);
    inj(V0, 3);
    inj(V2, 205);
    inj(V2, 206);
    inj(V0, 4);
    wakeups(2);
    UtAssert_True(t5_nsent == from, "budget 0 holds all output while the queue fills and evicts (%d sent)", t5_nsent - from);
    if (get_hk(&h))
    {
        UtAssert_True(h.drop[0] == 0 && h.drop[1] == 1 && h.drop[2] == 6 && h.drop[3] == 6,
                      "DroppedByClass %u/%u/%u/%u, expected 0/1/6/6 (oldest packet of the lowest-priority class present "
                      "evicted for a higher-priority arrival, otherwise the arrival dropped)",
                      (unsigned)h.drop[0], (unsigned)h.drop[1], (unsigned)h.drop[2], (unsigned)h.drop[3]);
        UtAssert_True(h.queued == 8 && h.rx == 21, "QueuedPackets %u, PacketsReceived %u (expected 8, 21)", h.queued,
                      (unsigned)h.rx);
    }
    cmd_budget(3);
    from = t5_nsent;
    wakeups(4);
    {
        sd_t e[8] = {{V0, 1}, {V0, 2}, {V0, 3}, {V0, 4}, {V1, 102}, {V1, 103}, {V1, 104}, {V1, 105}};
        expect_sent(from, e, 8, "the queue that remains after eviction drains class 0 then class 1, each in arrival order");
    }
    expect_intact(from);
    if (get_hk(&h))
        UtAssert_True(h.sent[0] == 4 && h.sent[1] == 4 && h.sent[2] == 0 && h.sent[3] == 0 && h.queued == 0,
                      "SentByClass %u/%u/%u/%u, QueuedPackets %u (expected 4/4/0/0, 0)", (unsigned)h.sent[0],
                      (unsigned)h.sent[1], (unsigned)h.sent[2], (unsigned)h.sent[3], h.queued);

    /* A full queue of the arrival's own class keeps its packets: the arrival is the one dropped. */
    cmd_budget(0);
    for (i = 1; i <= 5; i++) inj(V2, 400 + (uint32)i);
    wakeup();
    for (i = 6; i <= 9; i++) inj(V2, 400 + (uint32)i);
    wakeups(2);
    cmd_budget(8);
    from = t5_nsent;
    wakeup();
    {
        sd_t e[8] = {{V2, 401}, {V2, 402}, {V2, 403}, {V2, 404}, {V2, 405}, {V2, 406}, {V2, 407}, {V2, 408}};
        expect_sent(from, e, 8, "with the queue full of the arriving packet's own class, the arriving packet is dropped");
    }
    if (get_hk(&h))
        UtAssert_True(h.drop[2] == 7, "DroppedByClass[2] %u, expected 7 (one more class 2 drop)", (unsigned)h.drop[2]);
}

static void Test_OversizeAndSocketErrors(void)
{
    ptbl_t t;
    hk_t h;
    int from, i;
    bool found = false;
    ptbl_quad(&t, 0, 8);
    if (!boot(&t)) return;
    inj_size(V0, 77, 1025);
    inj_size(V1, 78, 1024);
    wakeup();
    if (get_hk(&h))
        UtAssert_True(h.drop[0] == 1 && h.queued == 1 && h.rx == 2,
                      "a 1025-byte packet is dropped as a class 0 drop and a 1024-byte one is queued "
                      "(DroppedByClass[0] %u, QueuedPackets %u, PacketsReceived %u)", (unsigned)h.drop[0], h.queued,
                      (unsigned)h.rx);
    cmd_budget(4);
    from = t5_nsent;
    wakeup();
    for (i = from; i < t5_nsent; i++)
        if (t5_be16(t5_sent[i].bytes) == V1 && t5_sent[i].size == 1024) found = true;
    UtAssert_True(found && t5_nsent - from == 1, "the 1024-byte packet is sent as one 1024-byte datagram");
    expect_intact(from);

    /* Socket errors: counted, and the packet leaves the queue either way; output stays on. */
    cmd_budget(0);
    inj(V0, 1);
    inj(V1, 101);
    wakeup();
    t5_sendto_rc = OS_ERROR;
    cmd_budget(8);
    wakeup();
    t5_sendto_rc = OS_SUCCESS;
    if (get_hk(&h))
        UtAssert_True(h.sockerr == 2 && h.queued == 0 && h.sent[0] == 0 && h.sent[1] == 1,
                      "two failed sends count SocketErrors 2 and leave the queue (SocketErrors %u, QueuedPackets %u, "
                      "SentByClass[0..1] %u/%u)", (unsigned)h.sockerr, h.queued, (unsigned)h.sent[0], (unsigned)h.sent[1]);
    from = t5_nsent;
    wakeup();
    UtAssert_True(t5_nsent == from, "packets that failed to send are not sent again");
    inj(V0, 2);
    wakeups(2);
    UtAssert_True(t5_nsent - from == 1, "output continues after socket errors (%d datagrams for one packet)", t5_nsent - from);
}

static void Test_DropEvents(void)
{
    ptbl_t t;
    int ev0;
    ptbl_init(&t, 0, 2);
    ptbl_set(&t, 0, V0, 0);
    ptbl_set(&t, 1, V1, 1);
    if (!boot(&t)) return;
    ev0 = t5_nev;
    inj(V1, 101);
    inj(V1, 102);
    inj(V1, 103);
    wakeup();
    UtAssert_True(t5_count_ev(EID_DROP, ev0) == 1, "the first drop in class 1 emits one TLMO_DROP_INF_EID (17), saw %d",
                  t5_count_ev(EID_DROP, ev0));
    UtAssert_True(ev_names_class(ev0, 1), "the drop event names the class (1)");
    inj(V1, 104);
    inj(V1, 105);
    inj(V0, 1);
    wakeup();
    UtAssert_True(t5_count_ev(EID_DROP, ev0) == 1, "further drops in class 1 emit no further event, saw %d",
                  t5_count_ev(EID_DROP, ev0));
    inj(V0, 2);
    inj(V0, 3);
    wakeup();
    UtAssert_True(t5_count_ev(EID_DROP, ev0) == 2, "the first class 0 drop emits its own event (%d events in total)",
                  t5_count_ev(EID_DROP, ev0));
    cmd0(CC_RESET);
    ev0 = t5_nev;
    inj(V0, 4);
    wakeup();
    UtAssert_True(t5_count_ev(EID_DROP, ev0) == 1, "after RESET_COUNTERS the next drop emits the event again, saw %d",
                  t5_count_ev(EID_DROP, ev0));
    UtAssert_True(t5_tbl_submit(TBL_NAME, t.b) == CFE_SUCCESS, "the same table is accepted for an update");
    wakeups(2);
    t5_send_cmd(MID_SEND_HK, 0, NULL, 0);
    UtAssert_True(t5_count_ev(EID_TBL_UPDATE, 0) == 1, "the table update is reported with TLMO_TBL_UPDATE_INF_EID (19)");
    ev0 = t5_nev;
    inj(V0, 5);
    wakeup();
    UtAssert_True(t5_count_ev(EID_DROP, ev0) == 1, "after a table load the next drop emits the event again, saw %d",
                  t5_count_ev(EID_DROP, ev0));
}

static void Test_SetClass(void)
{
    ptbl_t t;
    cmap_t m;
    hk_t h, h0;
    int from, ev0, s;
    ptbl_quad(&t, 0, 8);
    if (!boot(&t)) return;
    inj(V1, 101);
    wakeup();
    if (!get_hk(&h0)) return;
    ev0 = t5_nev;
    cmd_class(V1, 3);
    UtAssert_True(t5_count_ev(EID_CLASS_INF, ev0) == 1, "SET_CLASS of a used stream emits TLMO_SET_CLASS_INF_EID (13)");
    if (get_map(&m))
    {
        s = map_slot_of(&m, V1);
        UtAssert_True(s == 1 && m.cls[1] == 3, "the class map shows the stream in its slot with its new class 3 (slot %d)", s);
    }
    inj(V1, 102);
    inj(V2, 201);
    wakeup();
    cmd_budget(8);
    from = t5_nsent;
    wakeup();
    {
        sd_t e[3] = {{V1, 101}, {V2, 201}, {V1, 102}};
        expect_sent(from, e, 3, "SET_CLASS affects only packets enqueued afterwards");
    }
    ev0 = t5_nev;
    cmd_class(VF, 1);
    cmd_class(V1, 4);
    UtAssert_True(t5_count_ev(EID_CLASS_ERR, ev0) == 2, "SET_CLASS of an unused stream or to class 4 emits "
                  "TLMO_SET_CLASS_ERR_EID (14) each time, saw %d", t5_count_ev(EID_CLASS_ERR, ev0));
    UtAssert_True(t5_last_ev_type(EID_CLASS_ERR) == CFE_EVS_EventType_ERROR, "event 14 is ERROR");
    if (get_hk(&h))
        UtAssert_True(h.cec == h0.cec + 2, "two rejected SET_CLASS count as command errors (%u -> %u)", h0.cec, h.cec);
    if (get_map(&m)) UtAssert_True(m.cls[1] == 3, "a rejected SET_CLASS changes nothing (class %u)", m.cls[1]);
}

static void Test_SetBudget(void)
{
    ptbl_t t;
    hk_t h;
    int from, ev0, i, a, b, c;
    ptbl_quad(&t, 16, 64);
    if (!boot(&t)) return;
    if (get_hk(&h)) UtAssert_True(h.budget == 16, "BudgetPacketsPerCycle %u from the table, expected 16", h.budget);
    ev0 = t5_nev;
    cmd_budget(257);
    UtAssert_True(t5_count_ev(EID_BUDGET_ERR, ev0) == 1, "SET_BUDGET 257 emits TLMO_SET_BUDGET_ERR_EID (16)");
    if (get_hk(&h)) UtAssert_True(h.budget == 16 && h.cec == 1, "a rejected budget changes nothing (budget %u, cec %u)",
                                  h.budget, h.cec);
    ev0 = t5_nev;
    cmd_budget(256);
    UtAssert_True(t5_count_ev(EID_BUDGET_INF, ev0) == 1, "SET_BUDGET 256 emits TLMO_SET_BUDGET_INF_EID (15)");
    if (get_hk(&h)) UtAssert_True(h.budget == 256, "BudgetPacketsPerCycle %u after SET_BUDGET 256", h.budget);
    cmd_budget(0);
    if (get_hk(&h)) UtAssert_True(h.budget == 0, "BudgetPacketsPerCycle %u after SET_BUDGET 0", h.budget);
    from = t5_nsent;
    for (i = 1; i <= 5; i++) inj(V0, (uint32)i);
    wakeups(2);
    UtAssert_True(t5_nsent == from, "budget 0 sends nothing");
    cmd_budget(2);
    a = t5_nsent;
    wakeup();
    b = t5_nsent;
    wakeup();
    c = t5_nsent;
    wakeup();
    UtAssert_True(b - a == 2 && c - b == 2 && t5_nsent - c == 1,
                  "budget 2 sends at most two packets per WAKEUP (%d, %d, %d)", b - a, c - b, t5_nsent - c);
    ev0 = t5_nev;
    {
        uint8 p[2] = {1, 0};
        t5_send_cmd(MID_CMD, CC_BUDGET, p, sizeof(p));
    }
    UtAssert_True(t5_count_ev(EID_LEN_ERR, ev0) == 1, "SET_BUDGET with a wrong length emits TLMO_CMD_LEN_ERR_EID (6)");
}

static void Test_ClassMap(void)
{
    ptbl_t t;
    cmap_t m;
    hk_t h0, h1;
    char why[160];
    int s;
    ptbl_default(&t);
    if (!boot(&t) || !get_hk(&h0)) return;
    if (get_map(&m) && get_hk(&h1))
        UtAssert_True(h1.cc == (uint8)(h0.cc + 1) && h1.cec == h0.cec,
                      "SEND_CLASS_MAP is an accepted command and increments CommandCounter (%u -> %u)", h0.cc, h1.cc);
    if (get_map(&m))
    {
        UtAssert_True(m.budget == 16 && m.count == 48, "class map BudgetPacketsPerCycle %u, StreamCount %u (expected 16, 48)",
                      m.budget, m.count);
        UtAssert_True(map_equals(&m, &t, why, sizeof(why)), "the class map is the runtime list in slot order: %s",
                      map_equals(&m, &t, why, sizeof(why)) ? "" : why);
    }
    cmd_class(0x0800, 3);
    if (get_map(&m))
    {
        s = map_slot_of(&m, 0x0800);
        UtAssert_True(s == 3 && m.cls[3] == 3, "after SET_CLASS the map reports the runtime class 3 in slot 3 (slot %d)", s);
    }
}

static void Test_AddStream(void)
{
    ptbl_t t;
    cmap_t m, m0;
    hk_t h, h0;
    int from, ev0;
    ptbl_init(&t, 16, 64);
    ptbl_set(&t, 0, V0, 0);
    ptbl_set(&t, 2, V1, 1);
    if (!boot(&t)) return;
    ev0 = t5_nev;
    cmd_add(VE, 2);
    UtAssert_True(t5_count_ev(EID_ADD_INF, ev0) == 1, "ADD_STREAM emits TLMO_ADD_STREAM_INF_EID (20)");
    UtAssert_True(t5_ev_has(EID_ADD_INF, ev0, "0x18EE", NULL, NULL), "the ADD_STREAM event names the MID as 0x18EE");
    UtAssert_True(t5_subscribed(VE) && t5_sub_msglim(VE) == 32, "the added stream is subscribed with a message limit of 32");
    if (get_map(&m))
        UtAssert_True(m.mid[1] == VE && m.cls[1] == 2 && m.count == 3,
                      "ADD_STREAM fills the lowest unused slot (slot 1: MID 0x%04X class %u, StreamCount %u)",
                      (unsigned)m.mid[1], m.cls[1], m.count);
    if (get_hk(&h)) UtAssert_True(h.active == 3, "ActiveStreams %u after ADD_STREAM, expected 3", h.active);
    from = t5_nsent;
    inj(VE, 501);
    wakeups(2);
    {
        sd_t e[1] = {{VE, 501}};
        expect_sent(from, e, 1, "packets of the added stream are forwarded");
    }
    if (!get_map(&m0) || !get_hk(&h0)) return;
    ev0 = t5_nev;
    cmd_add(0, 1);
    cmd_add(V0, 1);
    cmd_add(VF, 4);
    fail_sub_mid = VF;
    cmd_add(VF, 1);
    fail_sub_mid = 0;
    UtAssert_True(t5_count_ev(EID_ADD_ERR, ev0) == 4, "an invalid MID, a used stream, class 4 and a failed subscription "
                  "each emit TLMO_ADD_STREAM_ERR_EID (21), saw %d", t5_count_ev(EID_ADD_ERR, ev0));
    UtAssert_True(t5_last_ev_type(EID_ADD_ERR) == CFE_EVS_EventType_ERROR, "event 21 is ERROR");
    UtAssert_True(!t5_subscribed(VF), "a rejected ADD_STREAM leaves no subscription");
    if (get_map(&m))
    {
        char why[160];
        ptbl_t now;
        int i;
        ptbl_init(&now, 0, 0);
        for (i = 0; i < MAX_STREAMS; i++) ptbl_set(&now, i, m0.mid[i], m0.cls[i]);
        UtAssert_True(map_equals(&m, &now, why, sizeof(why)), "rejected ADD_STREAM commands change nothing: %s",
                      map_equals(&m, &now, why, sizeof(why)) ? "" : why);
    }
    if (get_hk(&h))
        UtAssert_True(h.cec == h0.cec + 4 && h.active == 3, "four rejections count CommandErrorCounter (+%d), ActiveStreams %u",
                      h.cec - h0.cec, h.active);
}

static void Test_AddStreamFull(void)
{
    ptbl_t t, full;
    cmap_t m;
    hk_t h;
    int i, ev0;
    ptbl_init(&t, 16, 64);
    for (i = 0; i < 63; i++) ptbl_set(&t, i, 0x0900u + (uint32)i, (uint8)(i % 4));
    ptbl_init(&full, 16, 64);
    for (i = 0; i < 64; i++) ptbl_set(&full, i, 0x0900u + (uint32)i, (uint8)(i % 4));
    if (!boot(&t)) return;
    UtAssert_True(t5_tbl_validate(TBL_NAME, full.b) == CFE_SUCCESS, "a table with all 64 streams used is valid");
    if (get_hk(&h)) UtAssert_True(h.active == 63, "ActiveStreams %u, expected 63", h.active);
    cmd_add(VE, 2);
    ev0 = t5_nev;
    cmd_add(VF, 1);
    UtAssert_True(t5_count_ev(EID_ADD_ERR, ev0) == 1, "ADD_STREAM with a full runtime list emits TLMO_ADD_STREAM_ERR_EID (21)");
    UtAssert_True(!t5_subscribed(VF), "the rejected stream is not subscribed");
    if (get_map(&m))
        UtAssert_True(m.mid[63] == VE && m.count == 64 && map_slot_of(&m, VF) < 0,
                      "the last free slot took the first added stream and the list holds 64 (StreamCount %u)", m.count);
    if (get_hk(&h)) UtAssert_True(h.active == 64 && h.cec == 1, "ActiveStreams %u, CommandErrorCounter %u", h.active, h.cec);
    cmd_remove(0x0905);
    cmd_add(VF, 1);
    if (get_map(&m))
        UtAssert_True(m.mid[5] == VF && m.cls[5] == 1, "after REMOVE_STREAM the freed slot 5 is the lowest unused slot "
                      "(slot 5 holds MID 0x%04X)", (unsigned)m.mid[5]);
}

static void Test_RemoveStream(void)
{
    ptbl_t t;
    cmap_t m;
    hk_t h;
    int from, ev0;
    ptbl_quad(&t, 16, 64);
    if (!boot(&t)) return;
    cmd_budget(0);
    inj(V1, 101);
    inj(V1, 102);
    inj(V1, 103);
    inj(V0, 1);
    inj(V0, 2);
    wakeup();
    ev0 = t5_nev;
    cmd_remove(V1);
    UtAssert_True(t5_count_ev(EID_REM_INF, ev0) == 1, "REMOVE_STREAM emits TLMO_REMOVE_STREAM_INF_EID (22)");
    UtAssert_True(!t5_subscribed(V1), "REMOVE_STREAM unsubscribes the stream");
    if (get_map(&m)) UtAssert_True(m.mid[1] == 0 && m.count == 3, "the slot is emptied (slot 1 MID 0x%04X, StreamCount %u)",
                                   (unsigned)m.mid[1], m.count);
    if (get_hk(&h))
        UtAssert_True(h.queued == 2 && drops(&h) == 0 && h.active == 3,
                      "the stream's queued packets are discarded without counting (QueuedPackets %u, drops %u, "
                      "ActiveStreams %u)", h.queued, (unsigned)drops(&h), h.active);
    cmd_budget(16);
    from = t5_nsent;
    wakeup();
    {
        sd_t e[2] = {{V0, 1}, {V0, 2}};
        expect_sent(from, e, 2, "only the remaining stream's packets are sent after REMOVE_STREAM");
    }
    ev0 = t5_nev;
    cmd_remove(VF);
    UtAssert_True(t5_count_ev(EID_REM_ERR, ev0) == 1, "REMOVE_STREAM of an unused stream emits TLMO_REMOVE_STREAM_ERR_EID (23)");
    if (get_hk(&h)) UtAssert_True(h.cec == 1, "the rejected REMOVE_STREAM counts a command error (%u)", h.cec);
    cmd_add(V1, 1);
    if (get_map(&m)) UtAssert_True(m.mid[1] == V1, "a stream can be added back into the freed slot 1");
}

static void Test_TableValidation(void)
{
    ptbl_t t, good;
    int ev0;
    ptbl_quad(&good, 16, 64);
    if (!boot(&good)) return;
    UtAssert_True(t5_tbl_validate(TBL_NAME, good.b) == CFE_SUCCESS, "a valid table is accepted");
    ev0 = t5_nev;
    t = good;
    t5_put16(t.b, 257);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "BudgetPacketsPerCycle 257 is rejected");
    UtAssert_True(t5_count_ev(EID_TBLVAL, ev0) >= 1, "a rejected table emits TLMO_TBL_VAL_ERR_EID (10)");
    UtAssert_True(t5_last_ev_type(EID_TBLVAL) == CFE_EVS_EventType_ERROR, "event 10 is ERROR");
    t = good;
    t5_put16(t.b, 256);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "BudgetPacketsPerCycle 256 is accepted");
    t = good;
    t5_put16(t.b, 0);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "BudgetPacketsPerCycle 0 is accepted");
    t = good;
    t5_put16(t.b + 2, 0);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "QueueCapacity 0 is rejected");
    t = good;
    t5_put16(t.b + 2, 65);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "QueueCapacity 65 is rejected");
    t = good;
    t5_put16(t.b + 2, 1);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "QueueCapacity 1 is accepted");
    t = good;
    ptbl_set(&t, 2, V2, 4);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "class 4 in a used entry is rejected");
    t = good;
    ptbl_set(&t, 10, 0, 4);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "an unused entry is not checked for its class");
    t = good;
    ptbl_set(&t, 40, V1, 2);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "duplicate used MIDs (slots 1 and 40) are rejected");
    ptbl_init(&t, 16, 64);
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "a table with no used stream is accepted");
}

static void Test_TableUpdate(void)
{
    ptbl_t t1, t2, bad;
    cmap_t m;
    hk_t h;
    char why[160];
    int from;
    ptbl_quad(&t1, 0, 16);
    if (!boot(&t1)) return;
    UtAssert_True(t5_count_ev(EID_TBL_UPDATE, 0) == 0, "no TLMO_TBL_UPDATE_INF_EID (19) on the initial load");
    cmd_add(VE, 1);
    cmd_class(V1, 2);
    inj(V3, 3001);
    inj(V3, 3002);
    inj(V1, 1001); /* class 2 at enqueue */
    inj(V1, 1002);
    inj(V2, 2001);
    inj(VE, 5001);
    wakeup();
    /* New table: V3 moves to class 0, V1 stays (class 1), VC is new; V0, V2 and the added VE go away. */
    ptbl_init(&t2, 0, 16);
    ptbl_set(&t2, 0, V3, 0);
    ptbl_set(&t2, 1, V1, 1);
    ptbl_set(&t2, 2, VC, 2);
    UtAssert_True(t5_tbl_submit(TBL_NAME, t2.b) == CFE_SUCCESS, "the new table passes validation");
    wakeups(2);
    t5_send_cmd(MID_SEND_HK, 0, NULL, 0);
    UtAssert_True(t5_count_ev(EID_TBL_UPDATE, 0) == 1, "a table update emits TLMO_TBL_UPDATE_INF_EID (19) once, saw %d",
                  t5_count_ev(EID_TBL_UPDATE, 0));
    if (get_map(&m))
        UtAssert_True(map_equals(&m, &t2, why, sizeof(why)) && m.count == 3,
                      "the runtime list is the new table's (added streams dropped, classes reset): %s (StreamCount %u)",
                      map_equals(&m, &t2, why, sizeof(why)) ? "" : why, m.count);
    UtAssert_True(t5_subscribed(V3) && t5_subscribed(V1) && t5_subscribed(VC) && t5_sub_msglim(VC) == 32,
                  "the streams of the new table are subscribed");
    UtAssert_True(!t5_subscribed(V0) && !t5_subscribed(V2) && !t5_subscribed(VE),
                  "streams no longer listed, including one added by command, are unsubscribed");
    if (get_hk(&h))
        UtAssert_True(h.queued == 4 && drops(&h) == 0 && h.active == 3 && h.budget == 0,
                      "queued packets of removed streams are discarded uncounted (QueuedPackets %u, drops %u, "
                      "ActiveStreams %u, budget %u)", h.queued, (unsigned)drops(&h), h.active, h.budget);
    inj(V3, 3003); /* class 0 now */
    wakeup();
    cmd_budget(10);
    from = t5_nsent;
    wakeup();
    {
        sd_t e[5] = {{V3, 3003}, {V1, 1001}, {V1, 1002}, {V3, 3001}, {V3, 3002}};
        expect_sent(from, e, 5, "queued packets keep their class across a table update; new packets take the new class");
    }
    if (get_hk(&h))
        UtAssert_True(h.sent[0] == 1 && h.sent[1] == 0 && h.sent[2] == 2 && h.sent[3] == 2,
                      "SentByClass %u/%u/%u/%u, expected 1/0/2/2", (unsigned)h.sent[0], (unsigned)h.sent[1],
                      (unsigned)h.sent[2], (unsigned)h.sent[3]);

    /* The runtime budget and classes reset to the table on the next load. */
    cmd_class(V1, 3);
    t5_put16(t2.b, 5);
    UtAssert_True(t5_tbl_submit(TBL_NAME, t2.b) == CFE_SUCCESS, "a second update passes validation");
    wakeups(2);
    t5_send_cmd(MID_SEND_HK, 0, NULL, 0);
    if (get_hk(&h)) UtAssert_True(h.budget == 5, "the runtime budget is reset to the table's 5 (%u)", h.budget);
    if (get_map(&m)) UtAssert_True(m.cls[1] == 1, "the runtime class is reset to the table's 1 (%u)", m.cls[1]);

    /* A rejected table leaves the previous one active. */
    ptbl_init(&bad, 300, 16);
    ptbl_set(&bad, 0, V0, 0);
    UtAssert_True(t5_tbl_submit(TBL_NAME, bad.b) != CFE_SUCCESS, "an invalid table is rejected");
    wakeups(2);
    if (get_map(&m))
        UtAssert_True(map_equals(&m, &t2, why, sizeof(why)) && m.budget == 5, "the previous table stays active: %s",
                      map_equals(&m, &t2, why, sizeof(why)) ? "" : why);
}

static void Test_Heartbeat(void)
{
    ptbl_t t;
    int i, from, at[8] = {0}, nhb = 0, before;
    const t5_tx_t *hb;
    bool fields = true;
    ptbl_quad(&t, 16, 64);
    if (!boot(&t)) return;
    from = t5_ntx;
    for (i = 1; i <= 30; i++)
    {
        before = t5_tx_count(MID_HB, from);
        wakeup();
        if (t5_tx_count(MID_HB, from) > before && nhb < 8) at[nhb++] = i;
    }
    UtAssert_True(t5_tx_count(MID_HB, from) == 3, "30 WAKEUPs send 3 heartbeats on SFI_HEARTBEAT_MID (0x18DF), saw %d",
                  t5_tx_count(MID_HB, from));
    if (nhb >= 3)
        UtAssert_True(at[1] - at[0] == 10 && at[2] - at[1] == 10, "heartbeats come every 10th WAKEUP (%d, %d, %d)", at[0],
                      at[1], at[2]);
    for (i = from, nhb = 0; i < t5_ntx; i++)
    {
        if (t5_tx[i].mid != MID_HB) continue;
        hb = &t5_tx[i];
        nhb++;
        if (hb->size != 32 || strncmp((const char *)hb->bytes + 8, "TLMO", 20) != 0 || t5_msg_fcn(hb->bytes) != 0 ||
            t5_get32(hb->bytes + 28) != (uint32)nhb)
            fields = false;
    }
    UtAssert_True(fields, "each heartbeat is a 32-byte SFI_HeartbeatCmd_t (code 0) naming TLMO with Sequence 1, 2, 3");
}

static void Test_ResetCounters(void)
{
    ptbl_t t;
    hk_t h, pre;
    int ev0;
    ptbl_init(&t, 0, 2);
    ptbl_set(&t, 0, V0, 0);
    ptbl_set(&t, 1, V1, 1);
    if (!boot(&t)) return;
    inj(V1, 101);
    inj(V1, 102);
    inj(V1, 103);
    cmd0(CC_NOOP);
    cmd0(99);
    wakeup();
    inj(V0, 1); /* the queue holds 101, 102: the class 0 arrival evicts 101 */
    wakeup();
    t5_sendto_rc = OS_ERROR;
    cmd_budget(1);
    wakeup(); /* V0 1 fails to send */
    t5_sendto_rc = OS_SUCCESS;
    wakeup(); /* 102 sent */
    cmd_budget(0);
    inj(V0, 2);
    wakeup();
    if (!get_hk(&pre)) return;
    UtAssert_True(pre.cc >= 2 && pre.cec >= 1 && pre.rx == 5 && pre.sent[1] == 1 && pre.drop[1] == 2 && pre.sockerr == 1 &&
                  pre.queued == 1, "set-up: counters before RESET_COUNTERS (cc %u, cec %u, rx %u, sent[1] %u, drop[1] %u, "
                  "sockerr %u, queued %u)", pre.cc, pre.cec, (unsigned)pre.rx, (unsigned)pre.sent[1],
                  (unsigned)pre.drop[1], (unsigned)pre.sockerr, pre.queued);
    ev0 = t5_nev;
    cmd0(CC_RESET);
    UtAssert_True(t5_count_ev(EID_RESET, ev0) == 1, "RESET_COUNTERS emits TLMO_RESET_INF_EID (4)");
    if (get_hk(&h))
    {
        UtAssert_True(h.cc == 0 && h.cec == 0 && h.rx == 0 && h.sockerr == 0, "RESET_COUNTERS zeroes the command counters, "
                      "PacketsReceived and SocketErrors (%u, %u, %u, %u)", h.cc, h.cec, (unsigned)h.rx, (unsigned)h.sockerr);
        UtAssert_True(h.sent[0] + h.sent[1] + h.sent[2] + h.sent[3] == 0 && drops(&h) == 0,
                      "RESET_COUNTERS zeroes SentByClass and DroppedByClass");
        UtAssert_True(h.queued == pre.queued && h.budget == 0 && h.cap == 2 && h.active == 2,
                      "RESET_COUNTERS leaves the queue, budget, capacity and streams (queued %u, budget %u, capacity %u, "
                      "active %u)", h.queued, h.budget, h.cap, h.active);
    }
}

static void Test_CommandErrors(void)
{
    ptbl_t t;
    hk_t h, h0;
    int ev0;
    uint8 m[16];
    size_t n;
    ptbl_quad(&t, 16, 64);
    if (!boot(&t) || !get_hk(&h0)) return;
    ev0 = t5_nev;
    cmd0(99);
    UtAssert_True(t5_count_ev(EID_CC_ERR, ev0) == 1, "an invalid function code emits TLMO_CC_ERR_EID (2)");
    {
        uint8 p[4] = {0, 0, 0, 0};
        t5_send_cmd(MID_CMD, CC_NOOP, p, sizeof(p));
    }
    UtAssert_True(t5_count_ev(EID_LEN_ERR, ev0) == 1, "NOOP with a payload emits TLMO_CMD_LEN_ERR_EID (6)");
    {
        uint8 p[4] = {0, 0, 0, 0};
        t5_put32(p, V1);
        t5_send_cmd(MID_CMD, CC_CLASS, p, sizeof(p));
    }
    UtAssert_True(t5_count_ev(EID_LEN_ERR, ev0) == 2, "SET_CLASS with a wrong length emits TLMO_CMD_LEN_ERR_EID (6)");
    UtAssert_True(t5_last_ev_type(EID_CC_ERR) == CFE_EVS_EventType_ERROR &&
                  t5_last_ev_type(EID_LEN_ERR) == CFE_EVS_EventType_ERROR, "events 2 and 6 are ERROR");
    if (get_hk(&h)) UtAssert_True(h.cec == h0.cec + 3 && h.cc == h0.cc, "three rejected commands count CommandErrorCounter "
                                  "(+%d) and not CommandCounter (+%d)", h.cec - h0.cec, h.cc - h0.cc);
    n = t5_build_cmd(m, MID_UNUSED, 0, NULL, 0);
    ev0 = t5_nev;
    deliver_to_pipe_of(MID_CMD, m, n);
    UtAssert_True(t5_count_ev(EID_MID_ERR, ev0) == 1, "an unexpected message ID on the command pipe emits TLMO_MID_ERR_EID (5)");
    if (get_hk(&h0))
        UtAssert_True(h0.cec == (uint8)(h.cec + 1) && h0.cc == h.cc,
                      "the unexpected message ID counts as one rejected command (CommandErrorCounter %u -> %u, CommandCounter "
                      "%u -> %u)", h.cec, h0.cec, h.cc, h0.cc);
}

static void Test_StartupErrors(void)
{
    ptbl_t t;
    ptbl_quad(&t, 16, 64);

    t5_tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    t5_tbl_set_file(TBL_NAME, t.b, TBL_SIZE);
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_TBLREG, 0) >= 1, "a table registration failure emits TLMO_TBL_REG_ERR_EID (8)");
    t5_app_end();

    Setup();
    t5_tbl_load_rc = CFE_TBL_ERR_NO_ACCESS;
    t5_tbl_set_file(TBL_NAME, t.b, TBL_SIZE);
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_TBLLOAD, 0) >= 1, "a table load failure emits TLMO_TBL_LOAD_ERR_EID (9)");
    t5_app_end();

    Setup();
    fail_sub_mid = V1;
    t5_tbl_set_file(TBL_NAME, t.b, TBL_SIZE);
    if (t5_app_start()) wakeup();
    UtAssert_True(t5_count_ev(EID_SUB_ERR, 0) >= 1, "a failed stream subscription emits TLMO_SUB_ERR_EID (11)");
    t5_app_end();

    Setup();
    t5_sock_open_rc = OS_ERROR;
    t5_tbl_set_file(TBL_NAME, t.b, TBL_SIZE);
    if (t5_app_start())
    {
        wakeup();
        inj(V0, 1);
        wakeups(2);
    }
    UtAssert_True(t5_count_ev(EID_SOCKET_ERR, 0) >= 1, "a socket that cannot be opened emits TLMO_SOCKET_ERR_EID (12)");
    t5_app_end();

    Setup();
    t5_recv_rc = CFE_SB_PIPE_RD_ERR;
    t5_tbl_set_file(TBL_NAME, t.b, TBL_SIZE);
    t5_app_start();
    UtAssert_True(t5_count_ev(EID_PIPE_ERR, 0) >= 1, "a pipe read failure emits TLMO_PIPE_ERR_EID (7)");
}

void UtTest_Setup(void)
{
    UtAssert_True(sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes",
                  (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    UtTest_Add(Test_Startup, Setup, Teardown, "Startup, subscriptions and housekeeping");
    UtTest_Add(Test_Output, Setup, Teardown, "Output without an enable command");
    UtTest_Add(Test_DrainOrder, Setup, Teardown, "Drain order and budget");
    UtTest_Add(Test_Eviction, Setup, Teardown, "Eviction and drop counting");
    UtTest_Add(Test_OversizeAndSocketErrors, Setup, Teardown, "Oversize packets and socket errors");
    UtTest_Add(Test_DropEvents, Setup, Teardown, "Drop events");
    UtTest_Add(Test_SetClass, Setup, Teardown, "SET_CLASS");
    UtTest_Add(Test_SetBudget, Setup, Teardown, "SET_BUDGET");
    UtTest_Add(Test_ClassMap, Setup, Teardown, "SEND_CLASS_MAP");
    UtTest_Add(Test_AddStream, Setup, Teardown, "ADD_STREAM");
    UtTest_Add(Test_AddStreamFull, Setup, Teardown, "ADD_STREAM with a full runtime list");
    UtTest_Add(Test_RemoveStream, Setup, Teardown, "REMOVE_STREAM");
    UtTest_Add(Test_TableValidation, Setup, Teardown, "Table validation");
    UtTest_Add(Test_TableUpdate, Setup, Teardown, "Table update");
    UtTest_Add(Test_Heartbeat, Setup, Teardown, "Heartbeat");
    UtTest_Add(Test_ResetCounters, Setup, Teardown, "Reset counters");
    UtTest_Add(Test_CommandErrors, Setup, Teardown, "Command errors");
    UtTest_Add(Test_StartupErrors, Setup, Teardown, "Startup error events");
}
