/* Compact reference TLMO used only to exercise the pass path of the hidden test (scratch tree). */
#include "cfe.h"
#include <string.h>

#define TLMO_CMD_MID       0x18D4u
#define TLMO_SEND_HK_MID   0x18D5u
#define TLMO_WAKEUP_MID    0x18D6u
#define TLMO_HK_TLM_MID    0x08D4u
#define TLMO_CLASS_TLM_MID 0x08D5u
#define SFI_HEARTBEAT_MID  0x18DFu
#define MAX_STREAMS 64
#define NUM_CLASSES 4
#define MAX_QUEUE   64
#define MAX_BUDGET  256
#define MAX_PACKET  1024

typedef struct { CFE_SB_MsgId_t MsgId; uint8 Class; uint8 Spare[3]; } StreamDef_t;
typedef struct { uint16 Budget; uint16 Cap; StreamDef_t Streams[MAX_STREAMS]; } Table_t;
typedef struct { bool used; uint8 cls; uint32 mid; uint32 seq; size_t size; uint8 data[MAX_PACKET]; } Entry_t;

static struct
{
    CFE_SB_PipeId_t  CmdPipe, TlmPipe;
    CFE_TBL_Handle_t Tbl;
    bool             TblOk, Initialized, SockOk;
    osal_id_t        Sock;
    StreamDef_t      Streams[MAX_STREAMS];
    uint16           Budget, Cap, Count;
    Entry_t          Q[MAX_QUEUE];
    uint32           Seq, Wakeups, HbSeq;
    uint8            Cc, Cec;
    uint32           Rx, Sent[NUM_CLASSES], Dropped[NUM_CLASSES], SockErr;
    bool             DropReported[NUM_CLASSES];
} G;

static uint32 V(CFE_SB_MsgId_t m) { return CFE_SB_MsgIdToValue(m); }

static int32 Validate(void *p)
{
    const Table_t *t = p;
    int i, j;
    bool ok = t->Budget <= MAX_BUDGET && t->Cap >= 1 && t->Cap <= MAX_QUEUE;
    for (i = 0; ok && i < MAX_STREAMS; i++)
    {
        if (V(t->Streams[i].MsgId) == 0) continue;
        if (t->Streams[i].Class >= NUM_CLASSES) ok = false;
        for (j = i + 1; ok && j < MAX_STREAMS; j++)
            if (V(t->Streams[j].MsgId) == V(t->Streams[i].MsgId)) ok = false;
    }
    if (!ok)
    {
        CFE_EVS_SendEvent(10, CFE_EVS_EventType_ERROR, "TLMO: PriorityTable rejected");
        return -1;
    }
    return CFE_SUCCESS;
}

static int FindSlot(uint32 mid)
{
    int i;
    for (i = 0; i < MAX_STREAMS; i++) if (mid != 0 && V(G.Streams[i].MsgId) == mid) return i;
    return -1;
}

static void Drop(uint8 cls)
{
    G.Dropped[cls]++;
    if (!G.DropReported[cls])
    {
        G.DropReported[cls] = true;
        CFE_EVS_SendEvent(17, CFE_EVS_EventType_INFORMATION, "TLMO: first drop in class %u", (unsigned)cls);
    }
}

static int Oldest(int cls)
{
    int i, best = -1;
    for (i = 0; i < MAX_QUEUE; i++)
        if (G.Q[i].used && G.Q[i].cls == cls && (best < 0 || G.Q[i].seq < G.Q[best].seq)) best = i;
    return best;
}

static void Discard(uint32 mid)
{
    int i;
    for (i = 0; i < MAX_QUEUE; i++)
        if (G.Q[i].used && (mid == 0 ? FindSlot(G.Q[i].mid) < 0 : G.Q[i].mid == mid)) { G.Q[i].used = false; G.Count--; }
}

static void Insert(const uint8 *p, size_t n, uint8 cls)
{
    int i;
    for (i = 0; i < MAX_QUEUE && G.Q[i].used; i++) {}
    G.Q[i].used = true;
    G.Q[i].cls = cls;
    G.Q[i].mid = (uint32)((p[0] << 8) | p[1]);
    G.Q[i].seq = ++G.Seq;
    G.Q[i].size = n;
    memcpy(G.Q[i].data, p, n);
    G.Count++;
}

static void Enqueue(const CFE_SB_Buffer_t *b)
{
    CFE_SB_MsgId_t mid;
    CFE_MSG_Size_t n;
    int s, c, low = -1;
    uint8 cls;
    CFE_MSG_GetMsgId(&b->Msg, &mid);
    CFE_MSG_GetSize(&b->Msg, &n);
    s = FindSlot(V(mid));
    if (s < 0) return;
    G.Rx++;
    cls = G.Streams[s].Class;
    if (n > MAX_PACKET) { Drop(cls); return; }
    if (G.Count >= G.Cap)
    {
        for (c = 0; c < NUM_CLASSES; c++) if (Oldest(c) >= 0) low = c;
        if (low > cls)
        {
            int e = Oldest(low);
            G.Q[e].used = false;
            G.Count--;
            Drop((uint8)low);
        }
        else
        {
            Drop(cls);
            return;
        }
    }
    Insert((const uint8 *)b, n, cls);
}

static void Apply(const Table_t *t)
{
    int i, k;
    for (i = 0; i < MAX_STREAMS; i++)
    {
        uint32 m = V(G.Streams[i].MsgId);
        int found = 0;
        for (k = 0; k < MAX_STREAMS; k++) if (m != 0 && V(t->Streams[k].MsgId) == m) found = 1;
        if (m != 0 && !found) CFE_SB_Unsubscribe(G.Streams[i].MsgId, G.TlmPipe);
    }
    for (i = 0; i < MAX_STREAMS; i++)
    {
        uint32 m = V(t->Streams[i].MsgId);
        if (m != 0 && FindSlot(m) < 0 && CFE_SB_SubscribeEx(t->Streams[i].MsgId, G.TlmPipe, CFE_SB_DEFAULT_QOS, 32) != CFE_SUCCESS)
            CFE_EVS_SendEvent(11, CFE_EVS_EventType_ERROR, "TLMO: cannot subscribe to 0x%04X", (unsigned)m);
    }
    memcpy(G.Streams, t->Streams, sizeof(G.Streams));
    G.Budget = t->Budget;
    G.Cap = t->Cap;
    Discard(0);
    memset(G.DropReported, 0, sizeof(G.DropReported));
    if (G.Initialized) CFE_EVS_SendEvent(19, CFE_EVS_EventType_INFORMATION, "TLMO: table updated");
    G.Initialized = true;
}

static void Manage(void)
{
    void *p = NULL;
    if (!G.TblOk) return;
    CFE_TBL_Manage(G.Tbl);
    if (CFE_TBL_GetAddress(&p, G.Tbl) == CFE_TBL_INFO_UPDATED && p != NULL) Apply(p);
    CFE_TBL_ReleaseAddress(G.Tbl);
}

static void Drain(void)
{
    OS_SockAddr_t a;
    int n, c, e;
    OS_SocketAddrInit(&a, OS_SocketDomain_INET);
    OS_SocketAddrSetPort(&a, (uint16)(2234 + CFE_PSP_GetProcessorId() - 1));
    OS_SocketAddrFromString(&a, "127.0.0.1");
    for (n = 0; n < G.Budget && G.Count > 0; n++)
    {
        for (c = 0, e = -1; c < NUM_CLASSES && e < 0; c++) e = Oldest(c);
        if (G.SockOk && OS_SocketSendTo(G.Sock, G.Q[e].data, G.Q[e].size, &a) >= 0) G.Sent[G.Q[e].cls]++;
        else G.SockErr++;
        G.Q[e].used = false;
        G.Count--;
    }
}

static void Init(uint32 *w, uint32 mid, size_t n)
{
    CFE_MSG_Init((CFE_MSG_Message_t *)w, CFE_SB_ValueToMsgId(mid), n);
}

static void Hk(void)
{
    uint32 w[17];
    uint8 *p = (uint8 *)w + 16;
    uint16 a = 0;
    int i;
    Init(w, TLMO_HK_TLM_MID, 68);
    p[0] = G.Cc;
    p[1] = G.Cec;
    memcpy(p + 2, &G.Budget, 2);
    memcpy(p + 4, &G.Count, 2);
    memcpy(p + 6, &G.Cap, 2);
    for (i = 0; i < MAX_STREAMS; i++) if (V(G.Streams[i].MsgId) != 0) a++;
    memcpy(p + 8, &a, 2);
    memcpy(p + 12, &G.Rx, 4);
    memcpy(p + 16, G.Sent, 16);
    memcpy(p + 32, G.Dropped, 16);
    memcpy(p + 48, &G.SockErr, 4);
    CFE_SB_TimeStampMsg((CFE_MSG_Message_t *)w);
    CFE_SB_TransmitMsg((CFE_MSG_Message_t *)w, true);
}

static void Wakeup(void)
{
    CFE_SB_Buffer_t *b;
    (void)b;
    Manage();
    Drain();
    if (++G.Wakeups % 10 == 0)
    {
        uint32 w[8];
        uint8 *p = (uint8 *)w;
        Init(w, SFI_HEARTBEAT_MID, 32);
        CFE_MSG_SetFcnCode((CFE_MSG_Message_t *)w, 0);
        strncpy((char *)p + 8, "TLMO", 20);
        G.HbSeq++;
        memcpy(p + 28, &G.HbSeq, 4);
        CFE_MSG_GenerateChecksum((CFE_MSG_Message_t *)w);
        CFE_SB_TransmitMsg((CFE_MSG_Message_t *)w, true);
    }
}

static bool Len(const CFE_SB_Buffer_t *b, size_t want)
{
    CFE_MSG_Size_t n;
    CFE_MSG_GetSize(&b->Msg, &n);
    if (n != want)
    {
        CFE_EVS_SendEvent(6, CFE_EVS_EventType_ERROR, "TLMO: bad length %u", (unsigned)n);
        G.Cec++;
        return false;
    }
    return true;
}

static void Cmd(const CFE_SB_Buffer_t *b)
{
    CFE_MSG_FcnCode_t cc;
    const uint8 *p = (const uint8 *)b + 8;
    uint32 mid;
    int s, i, slot;
    CFE_MSG_GetFcnCode(&b->Msg, &cc);
    memcpy(&mid, p, 4);
    switch (cc)
    {
        case 0:
            if (!Len(b, 8)) return;
            G.Cc++;
            CFE_EVS_SendEvent(3, CFE_EVS_EventType_INFORMATION, "TLMO: NOOP");
            break;
        case 1:
            if (!Len(b, 8)) return;
            G.Cc = G.Cec = 0;
            G.Rx = G.SockErr = 0;
            memset(G.Sent, 0, sizeof(G.Sent));
            memset(G.Dropped, 0, sizeof(G.Dropped));
            memset(G.DropReported, 0, sizeof(G.DropReported));
            CFE_EVS_SendEvent(4, CFE_EVS_EventType_INFORMATION, "TLMO: reset");
            break;
        case 2:
            if (!Len(b, 16)) return;
            s = FindSlot(mid);
            if (s < 0 || p[4] >= NUM_CLASSES)
            {
                G.Cec++;
                CFE_EVS_SendEvent(14, CFE_EVS_EventType_ERROR, "TLMO: SET_CLASS rejected");
                return;
            }
            G.Streams[s].Class = p[4];
            G.Cc++;
            CFE_EVS_SendEvent(13, CFE_EVS_EventType_INFORMATION, "TLMO: class set");
            break;
        case 3:
        {
            uint16 n;
            if (!Len(b, 12)) return;
            memcpy(&n, p, 2);
            if (n > MAX_BUDGET)
            {
                G.Cec++;
                CFE_EVS_SendEvent(16, CFE_EVS_EventType_ERROR, "TLMO: budget %u rejected", (unsigned)n);
                return;
            }
            G.Budget = n;
            G.Cc++;
            CFE_EVS_SendEvent(15, CFE_EVS_EventType_INFORMATION, "TLMO: budget %u", (unsigned)n);
            break;
        }
        case 4:
        {
            uint32 w[133];
            uint8 *q = (uint8 *)w + 16;
            uint16 cnt = 0;
            if (!Len(b, 8)) return;
            Init(w, TLMO_CLASS_TLM_MID, 532);
            memcpy(q, &G.Budget, 2);
            for (i = 0; i < MAX_STREAMS; i++)
            {
                uint32 m = V(G.Streams[i].MsgId);
                if (m != 0) cnt++;
                memcpy(q + 4 + 8 * i, &m, 4);
                q[8 + 8 * i] = G.Streams[i].Class;
            }
            memcpy(q + 2, &cnt, 2);
            CFE_SB_TransmitMsg((CFE_MSG_Message_t *)w, true);
            G.Cc++;
            CFE_EVS_SendEvent(18, CFE_EVS_EventType_INFORMATION, "TLMO: class map");
            break;
        }
        case 5:
            if (!Len(b, 16)) return;
            for (i = 0, slot = -1; i < MAX_STREAMS && slot < 0; i++) if (V(G.Streams[i].MsgId) == 0) slot = i;
            if (mid == 0 || !CFE_SB_IsValidMsgId(CFE_SB_ValueToMsgId(mid)) || FindSlot(mid) >= 0 || p[4] >= NUM_CLASSES ||
                slot < 0 || CFE_SB_SubscribeEx(CFE_SB_ValueToMsgId(mid), G.TlmPipe, CFE_SB_DEFAULT_QOS, 32) != CFE_SUCCESS)
            {
                G.Cec++;
                CFE_EVS_SendEvent(21, CFE_EVS_EventType_ERROR, "TLMO: ADD_STREAM 0x%04X rejected", (unsigned)mid);
                return;
            }
            G.Streams[slot].MsgId = CFE_SB_ValueToMsgId(mid);
            G.Streams[slot].Class = p[4];
            G.Cc++;
            CFE_EVS_SendEvent(20, CFE_EVS_EventType_INFORMATION, "TLMO: added 0x%04X class %u", (unsigned)mid, p[4]);
            break;
        case 6:
            if (!Len(b, 12)) return;
            s = FindSlot(mid);
            if (s < 0)
            {
                G.Cec++;
                CFE_EVS_SendEvent(23, CFE_EVS_EventType_ERROR, "TLMO: REMOVE_STREAM 0x%04X rejected", (unsigned)mid);
                return;
            }
            CFE_SB_Unsubscribe(G.Streams[s].MsgId, G.TlmPipe);
            G.Streams[s].MsgId = CFE_SB_ValueToMsgId(0);
            Discard(mid);
            G.Cc++;
            CFE_EVS_SendEvent(22, CFE_EVS_EventType_INFORMATION, "TLMO: removed 0x%04X", (unsigned)mid);
            break;
        default:
            G.Cec++;
            CFE_EVS_SendEvent(2, CFE_EVS_EventType_ERROR, "TLMO: bad code %u", (unsigned)cc);
            break;
    }
}

void TLMO_AppMain(void)
{
    uint32 run = CFE_ES_RunStatus_APP_RUN;
    CFE_SB_Buffer_t *b;
    CFE_SB_MsgId_t mid;
    void *p = NULL;
    memset(&G, 0, sizeof(G));
    CFE_EVS_Register(NULL, 0, CFE_EVS_EventFilter_BINARY);
    CFE_SB_CreatePipe(&G.CmdPipe, 16, "TLMO_CMD");
    CFE_SB_CreatePipe(&G.TlmPipe, 48, "TLMO_TLM");
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(TLMO_CMD_MID), G.CmdPipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(TLMO_SEND_HK_MID), G.CmdPipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(TLMO_WAKEUP_MID), G.CmdPipe);
    if (CFE_TBL_Register(&G.Tbl, "PriorityTable", sizeof(Table_t), CFE_TBL_OPT_DEFAULT, Validate) != CFE_SUCCESS)
        CFE_EVS_SendEvent(8, CFE_EVS_EventType_ERROR, "TLMO: table registration failed");
    else if (CFE_TBL_Load(G.Tbl, CFE_TBL_SRC_FILE, "/cf/tlmo_tbl.tbl") != CFE_SUCCESS)
        CFE_EVS_SendEvent(9, CFE_EVS_EventType_ERROR, "TLMO: table load failed");
    else
    {
        G.TblOk = true;
        if (CFE_TBL_GetAddress(&p, G.Tbl) >= CFE_SUCCESS && p != NULL) Apply(p);
        CFE_TBL_ReleaseAddress(G.Tbl);
    }
    G.SockOk = OS_SocketOpen(&G.Sock, OS_SocketDomain_INET, OS_SocketType_DATAGRAM) == OS_SUCCESS;
    if (!G.SockOk) CFE_EVS_SendEvent(12, CFE_EVS_EventType_ERROR, "TLMO: socket error");
    CFE_EVS_SendEvent(1, CFE_EVS_EventType_INFORMATION, "TLMO initialized");
    while (CFE_ES_RunLoop(&run))
    {
        int32 st = CFE_SB_ReceiveBuffer(&b, G.TlmPipe, 500);
        if (st == CFE_SUCCESS) Enqueue(b);
        else if (st != CFE_SB_TIME_OUT && st != CFE_SB_NO_MESSAGE)
        {
            CFE_EVS_SendEvent(7, CFE_EVS_EventType_ERROR, "TLMO: pipe read error");
            run = CFE_ES_RunStatus_APP_ERROR;
            break;
        }
        while (CFE_SB_ReceiveBuffer(&b, G.CmdPipe, CFE_SB_POLL) == CFE_SUCCESS)
        {
            CFE_MSG_GetMsgId(&b->Msg, &mid);
            switch (V(mid))
            {
                case TLMO_CMD_MID: Cmd(b); break;
                case TLMO_SEND_HK_MID: Hk(); break;
                case TLMO_WAKEUP_MID: Wakeup(); break;
                default:
                    G.Cec++;
                    CFE_EVS_SendEvent(5, CFE_EVS_EventType_ERROR, "TLMO: bad MID 0x%04X", (unsigned)V(mid));
                    break;
            }
        }
    }
    CFE_ES_ExitApp(run);
}
