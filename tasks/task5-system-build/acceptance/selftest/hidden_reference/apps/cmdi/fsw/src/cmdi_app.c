/* Reference CMDI used only to exercise the pass path of the hidden test (spec S3.3, S3.4). */
#include <stddef.h>
#include <string.h>
#include "cfe.h"
#include "cmdval_lib.h"

#define CMDI_CMD_MID      0x18D0
#define CMDI_SEND_HK_MID  0x18D1
#define CMDI_WAKEUP_MID   0x18D2
#define CMDI_HK_TLM_MID   0x08D0
#define SFI_HEARTBEAT_MID 0x18DF

typedef struct { CMDVAL_Config_t Validation; } CMDI_ValTable_t;

typedef struct
{
    uint8  CommandCounter, CommandErrorCounter, EnableMask, Spare;
    uint32 FramesReceived, FramesAccepted, FormatErrors, CrcErrors, HmacErrors, SourceErrors, AgeErrors, MaxAgeSeconds;
} CMDI_HkTlm_Payload_t;
typedef struct { CFE_MSG_TelemetryHeader_t TelemetryHeader; CMDI_HkTlm_Payload_t Payload; } CMDI_HkTlm_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; char AppName[CFE_MISSION_MAX_API_LEN]; uint32 Sequence; } HB_t;

static struct
{
    CFE_SB_PipeId_t  Pipe;
    CFE_TBL_Handle_t Tbl;
    osal_id_t        Sock;
    bool             SockOk;
    CMDI_HkTlm_t     Hk;
    HB_t             Hb;
    CMDVAL_Config_t  Cfg;
    uint32           Wakeups;
    uint8            Buf[1200];
} G;

static int32 Validate(void *p)
{
    const CMDVAL_Config_t *c = &((const CMDI_ValTable_t *)p)->Validation;
    int  i, j;
    bool any = false, dup = false, key = false;
    for (i = 0; i < 32; i++) key = key || c->HmacKey[i] != 0;
    for (i = 0; i < 8; i++)
    {
        if (c->AllowedSources[i] == 0) continue;
        any = true;
        for (j = i + 1; j < 8; j++) dup = dup || c->AllowedSources[j] == c->AllowedSources[i];
    }
    if (!key || dup || c->MaxAgeSeconds == 0 || c->MaxAgeSeconds > 86400 || (c->EnableMask & ~0x0F) != 0 ||
        ((c->EnableMask & 0x04) && !any))
    {
        CFE_EVS_SendEvent(10, CFE_EVS_EventType_ERROR, "CMDI: ValTable rejected");
        return -1;
    }
    return CFE_SUCCESS;
}

static void Manage(void)
{
    CMDI_ValTable_t *t;
    int32 rc;
    CFE_TBL_Manage(G.Tbl);
    rc = CFE_TBL_GetAddress((void **)&t, G.Tbl);
    if (rc == CFE_TBL_INFO_UPDATED) G.Cfg = t->Validation;
    if (rc == CFE_TBL_INFO_UPDATED || rc == CFE_SUCCESS) CFE_TBL_ReleaseAddress(G.Tbl);
}

static const char *Reason(CMDVAL_Status_t s)
{
    switch (s)
    {
        case CMDVAL_ERR_FORMAT: G.Hk.Payload.FormatErrors++; return "FORMAT";
        case CMDVAL_ERR_CRC: G.Hk.Payload.CrcErrors++; return "CRC";
        case CMDVAL_ERR_HMAC: G.Hk.Payload.HmacErrors++; return "HMAC";
        case CMDVAL_ERR_SOURCE: G.Hk.Payload.SourceErrors++; return "SOURCE";
        default: G.Hk.Payload.AgeErrors++; return "AGE";
    }
}

static void ReadUplink(void)
{
    int i;
    for (i = 0; i < 16 && G.SockOk; i++)
    {
        OS_SockAddr_t      a;
        CMDVAL_FrameInfo_t info;
        CMDVAL_Status_t    st;
        int32              n = OS_SocketRecvFrom(G.Sock, G.Buf, sizeof(G.Buf), &a, OS_CHECK);
        if (n < 0) break;
        G.Hk.Payload.FramesReceived++;
        st = n > 1074 ? CMDVAL_ERR_FORMAT : CMDVAL_ValidateFrame(G.Buf, (size_t)n, &G.Cfg, CFE_TIME_GetTime().Seconds, &info);
        if (st == CMDVAL_OK)
        {
            G.Hk.Payload.FramesAccepted++;
            CFE_EVS_SendEvent(13, CFE_EVS_EventType_DEBUG, "CMDI: accepted MID 0x%04X",
                              (unsigned)((info.Command[0] << 8) | info.Command[1]));
            CFE_SB_TransmitMsg((const CFE_MSG_Message_t *)info.Command, false);
        }
        else
        {
            const char *r = Reason(st);
            if (st == CMDVAL_ERR_FORMAT)
                CFE_EVS_SendEvent(12, CFE_EVS_EventType_ERROR, "CMDI: frame rejected: %s", r);
            else
                CFE_EVS_SendEvent(12, CFE_EVS_EventType_ERROR, "CMDI: frame rejected: %s, source %u", r,
                                  (unsigned)((G.Buf[4] << 8) | G.Buf[5]));
        }
    }
}

static bool Len(const CFE_SB_Buffer_t *b, size_t want)
{
    size_t n = 0;
    CFE_MSG_GetSize(&b->Msg, &n);
    if (n == want) return true;
    CFE_EVS_SendEvent(6, CFE_EVS_EventType_ERROR, "CMDI: bad length %u", (unsigned)n);
    G.Hk.Payload.CommandErrorCounter++;
    return false;
}

static void Command(const CFE_SB_Buffer_t *b)
{
    CFE_MSG_FcnCode_t cc = 0;
    const uint8      *p  = (const uint8 *)b + sizeof(CFE_MSG_CommandHeader_t);
    uint32            v;
    CFE_MSG_GetFcnCode(&b->Msg, &cc);
    switch (cc)
    {
        case 0:
            if (!Len(b, 8)) return;
            G.Hk.Payload.CommandCounter++;
            CFE_EVS_SendEvent(3, CFE_EVS_EventType_INFORMATION, "CMDI: NOOP");
            break;
        case 1:
            if (!Len(b, 8)) return;
            memset(&G.Hk.Payload, 0, offsetof(CMDI_HkTlm_Payload_t, MaxAgeSeconds));
            CFE_EVS_SendEvent(4, CFE_EVS_EventType_INFORMATION, "CMDI: reset");
            break;
        case 2:
            if (!Len(b, 12)) return;
            memcpy(&v, p, 4);
            if (v < 1 || v > 86400)
            {
                G.Hk.Payload.CommandErrorCounter++;
                CFE_EVS_SendEvent(15, CFE_EVS_EventType_ERROR, "CMDI: bad age %u", (unsigned)v);
                return;
            }
            G.Cfg.MaxAgeSeconds = v;
            G.Hk.Payload.CommandCounter++;
            CFE_EVS_SendEvent(14, CFE_EVS_EventType_INFORMATION, "CMDI: max age %u", (unsigned)v);
            break;
        case 3:
            if (!Len(b, 12)) return;
            if (p[0] > 0x0F)
            {
                G.Hk.Payload.CommandErrorCounter++;
                CFE_EVS_SendEvent(15, CFE_EVS_EventType_ERROR, "CMDI: bad mask %u", p[0]);
                return;
            }
            G.Cfg.EnableMask = p[0];
            G.Hk.Payload.CommandCounter++;
            CFE_EVS_SendEvent(14, CFE_EVS_EventType_INFORMATION, "CMDI: mask %u", p[0]);
            break;
        default:
            G.Hk.Payload.CommandErrorCounter++;
            CFE_EVS_SendEvent(2, CFE_EVS_EventType_ERROR, "CMDI: bad CC %u", (unsigned)cc);
            break;
    }
}

static void Wakeup(void)
{
    Manage();
    ReadUplink();
    if (++G.Wakeups % 10 == 0)
    {
        G.Hb.Sequence++;
        CFE_SB_TransmitMsg(CFE_MSG_PTR(G.Hb.CommandHeader), true);
    }
}

void CMDI_AppMain(void)
{
    uint32           run = CFE_ES_RunStatus_APP_RUN;
    CFE_SB_Buffer_t *b;
    OS_SockAddr_t    a;
    int32            rc;

    memset(&G, 0, sizeof(G));
    CFE_EVS_Register(NULL, 0, CFE_EVS_EventFilter_BINARY);
    CFE_MSG_Init(CFE_MSG_PTR(G.Hk.TelemetryHeader), CFE_SB_ValueToMsgId(CMDI_HK_TLM_MID), sizeof(G.Hk));
    CFE_MSG_Init(CFE_MSG_PTR(G.Hb.CommandHeader), CFE_SB_ValueToMsgId(SFI_HEARTBEAT_MID), sizeof(G.Hb));
    strncpy(G.Hb.AppName, "CMDI", sizeof(G.Hb.AppName) - 1);
    if (CFE_SB_CreatePipe(&G.Pipe, 32, "CMDI_PIPE") != CFE_SUCCESS) run = CFE_ES_RunStatus_APP_ERROR;
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(CMDI_CMD_MID), G.Pipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(CMDI_SEND_HK_MID), G.Pipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(CMDI_WAKEUP_MID), G.Pipe);
    rc = CFE_TBL_Register(&G.Tbl, "ValTable", sizeof(CMDI_ValTable_t), CFE_TBL_OPT_DEFAULT, Validate);
    if (rc != CFE_SUCCESS)
        CFE_EVS_SendEvent(8, CFE_EVS_EventType_ERROR, "CMDI: table register failed");
    else if (CFE_TBL_Load(G.Tbl, CFE_TBL_SRC_FILE, "/cf/cmdi_tbl.tbl") != CFE_SUCCESS)
        CFE_EVS_SendEvent(9, CFE_EVS_EventType_ERROR, "CMDI: table load failed");
    else
        Manage();
    if (OS_SocketOpen(&G.Sock, OS_SocketDomain_INET, OS_SocketType_DATAGRAM) == OS_SUCCESS)
    {
        OS_SocketAddrInit(&a, OS_SocketDomain_INET);
        OS_SocketAddrFromString(&a, "127.0.0.1");
        OS_SocketAddrSetPort(&a, (uint16)(1234 + CFE_PSP_GetProcessorId() - 1));
        G.SockOk = OS_SocketBind(G.Sock, &a) == OS_SUCCESS;
    }
    if (!G.SockOk) CFE_EVS_SendEvent(11, CFE_EVS_EventType_ERROR, "CMDI: socket error");
    CFE_EVS_SendEvent(1, CFE_EVS_EventType_INFORMATION, "CMDI initialized");

    while (CFE_ES_RunLoop(&run))
    {
        CFE_SB_MsgId_t mid;
        rc = CFE_SB_ReceiveBuffer(&b, G.Pipe, CFE_SB_PEND_FOREVER);
        if (rc != CFE_SUCCESS)
        {
            CFE_EVS_SendEvent(7, CFE_EVS_EventType_ERROR, "CMDI: pipe read error");
            run = CFE_ES_RunStatus_APP_ERROR;
            break;
        }
        CFE_MSG_GetMsgId(&b->Msg, &mid);
        switch (CFE_SB_MsgIdToValue(mid))
        {
            case CMDI_CMD_MID:
                Command(b);
                break;
            case CMDI_WAKEUP_MID:
                Wakeup();
                break;
            case CMDI_SEND_HK_MID:
                Manage();
                G.Hk.Payload.EnableMask    = G.Cfg.EnableMask;
                G.Hk.Payload.MaxAgeSeconds = G.Cfg.MaxAgeSeconds;
                CFE_SB_TimeStampMsg(CFE_MSG_PTR(G.Hk.TelemetryHeader));
                CFE_SB_TransmitMsg(CFE_MSG_PTR(G.Hk.TelemetryHeader), true);
                break;
            default:
                G.Hk.Payload.CommandErrorCounter++;
                CFE_EVS_SendEvent(5, CFE_EVS_EventType_ERROR, "CMDI: bad MID 0x%04X", (unsigned)CFE_SB_MsgIdToValue(mid));
                break;
        }
    }
    CFE_ES_ExitApp(run);
}
