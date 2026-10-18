/* Compact reference SFI used only to exercise the pass path of the hidden test (not shipped in the pack). */
#include "cfe.h"
#include "sfi_cluster.h"
#include <string.h>
#include <stdio.h>

#define M_CMD 0x18DC
#define M_SEND_HK 0x18DD
#define M_WAKEUP 0x18DE
#define M_HB 0x18DF
#define M_HK 0x08DC
#define M_STATUS 0x08DD

typedef struct { char AppName[20]; } AppDef_t;
typedef struct { CFE_SB_MsgId_t MsgId; uint16 ByteOffset; uint8 Width; uint8 Rule; uint32 Limit; } WatchDef_t;
typedef struct { uint8 N; uint8 Spare; uint16 W; uint16 H; uint16 Spare2; AppDef_t Apps[16]; WatchDef_t Watches[16]; } Tbl_t;

typedef struct { uint8 cc, cec, monitored, missing, infault, corr, misslimit, spare;
                 uint16 recent, ccount, largest, window; uint32 total, unknown, missev, stall, rate; } HkPay_t;
typedef struct { CFE_MSG_TelemetryHeader_t H; HkPay_t P; } Hk_t;
typedef struct { char AppName[20]; uint8 State, Missed; uint16 Total; uint32 Sec, Sub; } AppSt_t;
typedef struct { uint8 Idx; uint8 Spare[3]; uint32 Sec, Sub; } Rec_t;
typedef struct { uint8 State; uint8 Spare[3]; uint32 Last; } WSt_t;
typedef struct { uint8 Missing, Corr; uint16 HCount, CCount, Largest; AppSt_t Apps[16]; Rec_t Hist[16]; WSt_t W[16]; } StPay_t;
typedef struct { CFE_MSG_TelemetryHeader_t H; StPay_t P; } St_t;

static struct
{
    CFE_SB_PipeId_t pipe; CFE_TBL_Handle_t th; Tbl_t t; uint8 N; uint16 W;
    uint8 st[16], missed[16]; bool heard[16]; uint16 total[16]; CFE_TIME_SysTime_t last[16];
    uint8 wst[16]; uint32 wlast[16], stall[16]; uint32 subs[16]; int nsubs;
    uint8 ridx[16]; CFE_TIME_SysTime_t rt[16]; int rhead, rcount;
    Hk_t hk; St_t status;
} G;

static int32 Validate(void *p)
{
    const Tbl_t *t = p;
    int i, j;
    if (t->N < 1 || t->N > 60 || t->W < 1 || t->W > 600 || t->H < 1 || t->H > 3600) goto bad;
    for (i = 0; i < 16; i++)
    {
        if (memchr(t->Apps[i].AppName, 0, 20) == NULL) goto bad;
        if (t->Apps[i].AppName[0] == 0) continue;
        for (j = 0; j < i; j++) if (strcmp(t->Apps[i].AppName, t->Apps[j].AppName) == 0) goto bad;
    }
    for (i = 0; i < 16; i++)
    {
        const WatchDef_t *w = &t->Watches[i];
        if (w->Rule == 0) continue;
        if ((w->Width != 1 && w->Width != 2 && w->Width != 4) || w->Limit == 0 || w->ByteOffset + w->Width > 1024) goto bad;
    }
    return CFE_SUCCESS;
bad:
    CFE_EVS_SendEvent(10, CFE_EVS_EventType_ERROR, "SFI: table invalid");
    return -1;
}

static void Apply(void)
{
    void *p = NULL; int32 rc0;
    int i, j;
    rc0 = CFE_TBL_GetAddress(&p, G.th);
    if (rc0 < 0 || p == NULL) return;
    memcpy(&G.t, p, sizeof(G.t));
    CFE_TBL_ReleaseAddress(G.th);
    G.N = G.t.N; G.W = G.t.W;
    memset(G.st, 0, sizeof(G.st)); memset(G.missed, 0, sizeof(G.missed)); memset(G.heard, 0, sizeof(G.heard));
    for (i = 0; i < 16; i++) { G.wst[i] = G.t.Watches[i].Rule ? 1 : 0; G.stall[i] = 0; }
    for (i = 0; i < G.nsubs; i++) CFE_SB_Unsubscribe(CFE_SB_ValueToMsgId(G.subs[i]), G.pipe);
    G.nsubs = 0;
    for (i = 0; i < 16; i++)
    {
        uint32 m = CFE_SB_MsgIdToValue(G.t.Watches[i].MsgId);
        if (G.t.Watches[i].Rule == 0) continue;
        for (j = 0; j < G.nsubs && G.subs[j] != m; j++) {}
        if (j < G.nsubs) continue;
        if (CFE_SB_SubscribeEx(G.t.Watches[i].MsgId, G.pipe, CFE_SB_DEFAULT_QOS, 8) != CFE_SUCCESS)
            CFE_EVS_SendEvent(11, CFE_EVS_EventType_ERROR, "SFI: subscribe 0x%04X failed", (unsigned)m);
        G.subs[G.nsubs++] = m;
    }
}

static void Manage(void) { if (CFE_TBL_Manage(G.th) == CFE_TBL_INFO_UPDATED) Apply(); }

static uint64 Full(CFE_TIME_SysTime_t t) { return ((uint64)t.Seconds << 32) | t.Subseconds; }

static void Clusters(CFE_TIME_SysTime_t now)
{
    CFE_TIME_SysTime_t on[16];
    SFI_ClusterResult_t r;
    uint16 n = 0;
    int i;
    for (i = 0; i < G.rcount; i++)
    {
        CFE_TIME_SysTime_t t = G.rt[(G.rhead + i) % 16];
        if (Full(now) - Full(t) <= ((uint64)G.t.H << 32)) on[n++] = t;
    }
    SFI_ComputeClusters(on, n, G.W, &r);
    if (r.Correlation == 2 && (G.hk.P.corr != 2 || r.LargestClusterSize > G.hk.P.largest))
        CFE_EVS_SendEvent(18, CFE_EVS_EventType_ERROR, "SFI: cluster of %u misses within W=%u s", r.LargestClusterSize, G.W);
    G.hk.P.recent = n; G.hk.P.ccount = r.ClusterCount; G.hk.P.largest = r.LargestClusterSize; G.hk.P.corr = r.Correlation;
}

static void Wakeup(void)
{
    CFE_TIME_SysTime_t now = CFE_TIME_GetTime();
    int i;
    Manage();
    for (i = 0; i < 16; i++)
    {
        if (G.t.Apps[i].AppName[0] == 0 || G.st[i] == 0) continue;
        if (G.heard[i]) G.missed[i] = 0; else if (G.missed[i] < 255) G.missed[i]++;
        G.heard[i] = false;
        if (G.st[i] == 1 && G.missed[i] >= G.N)
        {
            G.st[i] = 2; G.total[i]++; G.hk.P.missev++; G.last[i] = now;
            if (G.rcount == 16) G.rhead = (G.rhead + 1) % 16; else G.rcount++;
            G.ridx[(G.rhead + G.rcount - 1) % 16] = (uint8)i;
            G.rt[(G.rhead + G.rcount - 1) % 16] = now;
            CFE_EVS_SendEvent(12, CFE_EVS_EventType_ERROR, "SFI: %s missing, onset %lu", G.t.Apps[i].AppName, (unsigned long)now.Seconds);
        }
    }
    Clusters(now);
}

static void Heartbeat(const uint8 *m, size_t sz)
{
    char name[21];
    int i;
    if (sz != 32) return;
    memcpy(name, m + 8, 20); name[20] = 0;
    for (i = 0; i < 16; i++)
    {
        if (G.t.Apps[i].AppName[0] == 0 || strcmp(G.t.Apps[i].AppName, name) != 0) continue;
        G.hk.P.total++; G.heard[i] = true;
        if (G.st[i] == 0) { G.st[i] = 1; G.missed[i] = 0; }
        else if (G.st[i] == 2) { G.st[i] = 1; CFE_EVS_SendEvent(13, CFE_EVS_EventType_INFORMATION, "SFI: %s restored", name); }
        return;
    }
    G.hk.P.unknown++;
    CFE_EVS_SendEvent(14, CFE_EVS_EventType_DEBUG, "SFI: heartbeat from unknown %s", name);
}

static void Watch(uint32 mid, const uint8 *m, size_t sz)
{
    int i;
    for (i = 0; i < 16; i++)
    {
        const WatchDef_t *w = &G.t.Watches[i];
        uint32 v = 0, d;
        if (w->Rule == 0 || CFE_SB_MsgIdToValue(w->MsgId) != mid || (size_t)w->ByteOffset + w->Width > sz) continue;
        if (w->Width == 1) v = m[w->ByteOffset];
        else if (w->Width == 2) { uint16 x; memcpy(&x, m + w->ByteOffset, 2); v = x; }
        else memcpy(&v, m + w->ByteOffset, 4);
        if (G.wst[i] == 1) { G.wst[i] = 2; G.wlast[i] = v; G.stall[i] = 0; continue; }
        if (w->Rule == 1)
        {
            if (v == G.wlast[i])
            {
                G.stall[i]++;
                if (G.stall[i] >= w->Limit && G.wst[i] != 3)
                {
                    G.wst[i] = 3; G.hk.P.stall++;
                    CFE_EVS_SendEvent(15, CFE_EVS_EventType_ERROR, "SFI: MID 0x%04X offset %u stalled", (unsigned)mid, w->ByteOffset);
                }
            }
            else
            {
                G.stall[i] = 0;
                if (G.wst[i] == 3) { G.wst[i] = 2; CFE_EVS_SendEvent(17, CFE_EVS_EventType_INFORMATION, "SFI: watch %d clear", i); }
            }
        }
        else
        {
            d = v >= G.wlast[i] ? v - G.wlast[i] : v;
            if (d > w->Limit)
            {
                if (G.wst[i] != 3)
                {
                    G.wst[i] = 3; G.hk.P.rate++;
                    CFE_EVS_SendEvent(16, CFE_EVS_EventType_ERROR, "SFI: MID 0x%04X offset %u delta %lu", (unsigned)mid,
                                      w->ByteOffset, (unsigned long)d);
                }
            }
            else if (G.wst[i] == 3) { G.wst[i] = 2; CFE_EVS_SendEvent(17, CFE_EVS_EventType_INFORMATION, "SFI: watch %d clear", i); }
        }
        G.wlast[i] = v;
    }
}

static void SendHk(void)
{
    int i;
    Manage();
    G.hk.P.monitored = 0; G.hk.P.missing = 0; G.hk.P.infault = 0;
    for (i = 0; i < 16; i++)
    {
        if (G.t.Apps[i].AppName[0]) G.hk.P.monitored++;
        if (G.st[i] == 2) G.hk.P.missing++;
        if (G.wst[i] == 3) G.hk.P.infault++;
    }
    G.hk.P.misslimit = G.N; G.hk.P.window = G.W;
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(G.hk.H));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(G.hk.H), true);
}

static void SendStatus(void)
{
    int i;
    StPay_t *p = &G.status.P;
    memset(&G.status, 0, sizeof(G.status));
    CFE_MSG_Init(CFE_MSG_PTR(G.status.H), CFE_SB_ValueToMsgId(M_STATUS), sizeof(G.status));
    for (i = 0; i < 16; i++)
    {
        memcpy(p->Apps[i].AppName, G.t.Apps[i].AppName, 20);
        p->Apps[i].State = G.st[i]; p->Apps[i].Missed = G.missed[i]; p->Apps[i].Total = G.total[i];
        p->Apps[i].Sec = G.last[i].Seconds; p->Apps[i].Sub = G.last[i].Subseconds;
        if (G.st[i] == 2) p->Missing++;
        p->W[i].State = G.wst[i]; p->W[i].Last = G.wlast[i];
    }
    for (i = 0; i < G.rcount; i++)
    {
        int k = (G.rhead + i) % 16;
        p->Hist[i].Idx = G.ridx[k]; p->Hist[i].Sec = G.rt[k].Seconds; p->Hist[i].Sub = G.rt[k].Subseconds;
    }
    p->HCount = (uint16)G.rcount; p->Corr = G.hk.P.corr; p->CCount = G.hk.P.ccount; p->Largest = G.hk.P.largest;
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(G.status.H));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(G.status.H), true);
    CFE_EVS_SendEvent(21, CFE_EVS_EventType_INFORMATION, "SFI: status sent");
}

static void Command(const uint8 *m, size_t sz)
{
    CFE_MSG_FcnCode_t cc = 0;
    size_t want;
    CFE_MSG_GetFcnCode((const CFE_MSG_Message_t *)m, &cc);
    want = (cc == 2 || cc == 3) ? 12 : 8;
    if (cc > 5) { G.hk.P.cec++; CFE_EVS_SendEvent(2, CFE_EVS_EventType_ERROR, "SFI: bad code %u", cc); return; }
    if (sz != want) { G.hk.P.cec++; CFE_EVS_SendEvent(6, CFE_EVS_EventType_ERROR, "SFI: bad length %u", (unsigned)sz); return; }
    switch (cc)
    {
        case 0: G.hk.P.cc++; CFE_EVS_SendEvent(3, CFE_EVS_EventType_INFORMATION, "SFI: NOOP"); break;
        case 1:
            G.hk.P.cc = G.hk.P.cec = 0; G.hk.P.total = G.hk.P.unknown = G.hk.P.missev = G.hk.P.stall = G.hk.P.rate = 0;
            CFE_EVS_SendEvent(4, CFE_EVS_EventType_INFORMATION, "SFI: reset"); break;
        case 2:
            if (m[8] < 1 || m[8] > 60) { G.hk.P.cec++; CFE_EVS_SendEvent(20, CFE_EVS_EventType_ERROR, "SFI: bad limit"); break; }
            G.N = m[8]; G.hk.P.cc++; CFE_EVS_SendEvent(19, CFE_EVS_EventType_INFORMATION, "SFI: limit %u", G.N); break;
        case 3:
        {
            uint16 w;
            memcpy(&w, m + 8, 2);
            if (w < 1 || w > 600) { G.hk.P.cec++; CFE_EVS_SendEvent(20, CFE_EVS_EventType_ERROR, "SFI: bad window"); break; }
            G.W = w; G.hk.P.cc++; CFE_EVS_SendEvent(19, CFE_EVS_EventType_INFORMATION, "SFI: window %u", w); break;
        }
        case 4: G.hk.P.cc++; SendStatus(); break;
        default:
            G.rcount = G.rhead = 0; G.hk.P.recent = G.hk.P.ccount = G.hk.P.largest = 0; G.hk.P.corr = 0; G.hk.P.cc++;
            CFE_EVS_SendEvent(22, CFE_EVS_EventType_INFORMATION, "SFI: history cleared"); break;
    }
}

void SFI_AppMain(void)
{
    uint32 run = CFE_ES_RunStatus_APP_RUN;
    CFE_SB_Buffer_t *b;
    CFE_SB_MsgId_t id;
    CFE_MSG_Size_t sz;
    int32 rc;
    memset(&G, 0, sizeof(G));
    CFE_EVS_Register(NULL, 0, CFE_EVS_EventFilter_BINARY);
    CFE_MSG_Init(CFE_MSG_PTR(G.hk.H), CFE_SB_ValueToMsgId(M_HK), sizeof(G.hk));
    CFE_SB_CreatePipe(&G.pipe, 64, "SFI_PIPE");
    if (CFE_SB_Subscribe(CFE_SB_ValueToMsgId(M_CMD), G.pipe) != CFE_SUCCESS ||
        CFE_SB_Subscribe(CFE_SB_ValueToMsgId(M_SEND_HK), G.pipe) != CFE_SUCCESS ||
        CFE_SB_Subscribe(CFE_SB_ValueToMsgId(M_WAKEUP), G.pipe) != CFE_SUCCESS ||
        CFE_SB_SubscribeEx(CFE_SB_ValueToMsgId(M_HB), G.pipe, CFE_SB_DEFAULT_QOS, 32) != CFE_SUCCESS)
        CFE_EVS_SendEvent(11, CFE_EVS_EventType_ERROR, "SFI: subscribe failed");
    rc = CFE_TBL_Register(&G.th, "SfiTable", sizeof(Tbl_t), CFE_TBL_OPT_DEFAULT, Validate);
    if (rc != CFE_SUCCESS) CFE_EVS_SendEvent(8, CFE_EVS_EventType_ERROR, "SFI: table register %d", (int)rc);
    else if ((rc = CFE_TBL_Load(G.th, CFE_TBL_SRC_FILE, "/cf/sfi_tbl.tbl")) != CFE_SUCCESS)
        CFE_EVS_SendEvent(9, CFE_EVS_EventType_ERROR, "SFI: table load %d", (int)rc);
    else Apply();
    CFE_EVS_SendEvent(1, CFE_EVS_EventType_INFORMATION, "SFI: initialized");
    while (CFE_ES_RunLoop(&run))
    {
        rc = CFE_SB_ReceiveBuffer(&b, G.pipe, CFE_SB_PEND_FOREVER);
        if (rc != CFE_SUCCESS) { CFE_EVS_SendEvent(7, CFE_EVS_EventType_ERROR, "SFI: pipe %d", (int)rc); run = CFE_ES_RunStatus_APP_ERROR; break; }
        CFE_MSG_GetMsgId(&b->Msg, &id);
        CFE_MSG_GetSize(&b->Msg, &sz);
        switch (CFE_SB_MsgIdToValue(id))
        {
            case M_CMD: Command((const uint8 *)b, sz); break;
            case M_SEND_HK: SendHk(); break;
            case M_WAKEUP: Wakeup(); break;
            case M_HB: Heartbeat((const uint8 *)b, sz); break;
            default:
            {
                int i, hit = 0;
                for (i = 0; i < G.nsubs; i++) if (G.subs[i] == CFE_SB_MsgIdToValue(id)) hit = 1;
                if (hit) Watch(CFE_SB_MsgIdToValue(id), (const uint8 *)b, sz);
                else CFE_EVS_SendEvent(5, CFE_EVS_EventType_ERROR, "SFI: bad MID 0x%04X", (unsigned)CFE_SB_MsgIdToValue(id));
            }
        }
    }
    CFE_ES_ExitApp(run);
}
