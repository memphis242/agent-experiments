/*
 * Behavioural coverage test for the SAMPLE_APP status telemetry feature.
 *
 * Linked against every SAMPLE_APP source file (no module stubs), so the whole
 * command, dispatch and housekeeping path runs for real while the cFE services
 * are UT stubs. Only names promised by the task specification and the
 * pre-existing SAMPLE_APP interface are used; behaviour is observed through
 * the software bus (CFE_SB_TransmitMsg), the event service and the table
 * service stubs.
 */

#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>

#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"

#include "cfe.h"
#include "sample_app_tbl.h"
#include "sample_app.h"
#include "sample_app_dispatch.h"
#include "sample_app_cmds.h"
#include "sample_app_fcncodes.h"
#include "sample_app_eventids.h"
#include "sample_app_internal_cfg.h"
#include "sample_app_topicids.h"
#include "sample_app_msgids.h"
#include "sample_app_msg.h"

#define ADD_TEST(test) UtTest_Add((Test_##test), Hidden_Setup, NULL, #test)

static void Hidden_Setup(void)
{
    UT_ResetState(0);
}

/* ---- wire layout and pinned values (compile time) ---------------------- */

_Static_assert(sizeof(SAMPLE_APP_SetStatusRate_Payload_t) == 4, "SetStatusRate payload is 4 bytes");
_Static_assert(offsetof(SAMPLE_APP_SetStatusRate_Payload_t, Divisor) == 0, "Divisor offset");
_Static_assert(offsetof(SAMPLE_APP_SetStatusRate_Payload_t, Spare) == 2, "Spare offset");
_Static_assert(sizeof(SAMPLE_APP_StatusTlm_Payload_t) == 12, "status payload is 12 bytes");
_Static_assert(offsetof(SAMPLE_APP_StatusTlm_Payload_t, TableCrc) == 0, "TableCrc offset");
_Static_assert(offsetof(SAMPLE_APP_StatusTlm_Payload_t, ProcessCmdCount) == 4, "ProcessCmdCount offset");
_Static_assert(offsetof(SAMPLE_APP_StatusTlm_Payload_t, StatusDivisor) == 8, "StatusDivisor offset");
_Static_assert(offsetof(SAMPLE_APP_StatusTlm_Payload_t, HkCycleCount) == 10, "HkCycleCount offset");
_Static_assert(offsetof(SAMPLE_APP_StatusTlm_t, Payload) == sizeof(CFE_MSG_TelemetryHeader_t),
               "payload follows the telemetry header");
_Static_assert(sizeof(SAMPLE_APP_SetStatusRateCmd_t) ==
                   sizeof(CFE_MSG_CommandHeader_t) + sizeof(SAMPLE_APP_SetStatusRate_Payload_t),
               "command is header plus payload");
_Static_assert(SAMPLE_APP_SET_STATUS_RATE_CC == 4, "function code 4");
_Static_assert(SAMPLE_APP_STATUS_RATE_INF_EID == 13, "event 13");
_Static_assert(SAMPLE_APP_STATUS_RATE_ERR_EID == 14, "event 14");
_Static_assert(SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR == 2, "default divisor 2");
_Static_assert(SAMPLE_APP_MISSION_STATUS_TLM_TOPICID == 0x8F, "status topic 0x8F");

#define TABLE_CRC 0xA1B2C3D4u

/* ---- recorder ----------------------------------------------------------- */

#define REC_MAX_STATUS 512
#define REC_MAX_EVENTS 64
#define REC_TEXT_LEN   160

typedef struct
{
    uint32 Seq; /* position in the combined transmit order */
    SAMPLE_APP_StatusTlm_Payload_t Payload;
    bool TimeStamped; /* the same address was time-stamped before it was sent */
} Rec_Status_t;

typedef struct
{
    uint16 Id;
    uint16 Type;
    char   Text[REC_TEXT_LEN];
} Rec_Event_t;

static struct
{
    uint32       Order;      /* running sequence number across all transmits */
    uint32       HkCount;
    uint32       HkSeq[REC_MAX_STATUS];
    uint32       StatusCount;
    Rec_Status_t Status[REC_MAX_STATUS];
    uint32       EventCount;
    Rec_Event_t  Events[REC_MAX_EVENTS];
    const void  *StampedPtr[REC_MAX_STATUS * 2];
    uint32       StampCount;
} Rec;

static void Rec_Reset(void)
{
    memset(&Rec, 0, sizeof(Rec));
}

static int32 Hook_TimeStamp(void *UserObj, int32 StubRetcode, uint32 CallCount, const UT_StubContext_t *Context)
{
    const void *Ptr = UT_Hook_GetArgValueByName(Context, "MsgPtr", CFE_MSG_Message_t *);

    if (Rec.StampCount < (sizeof(Rec.StampedPtr) / sizeof(Rec.StampedPtr[0])))
    {
        Rec.StampedPtr[Rec.StampCount++] = Ptr;
    }
    return StubRetcode;
}

static int32 Hook_Transmit(void *UserObj, int32 StubRetcode, uint32 CallCount, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Ptr = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    uint32                   i;

    if (Ptr == CFE_MSG_PTR(SAMPLE_APP_Data.HkTlm.TelemetryHeader))
    {
        if (Rec.HkCount < REC_MAX_STATUS)
        {
            Rec.HkSeq[Rec.HkCount] = Rec.Order;
        }
        ++Rec.HkCount;
    }
    else if (Rec.StatusCount < REC_MAX_STATUS)
    {
        Rec_Status_t *S = &Rec.Status[Rec.StatusCount];

        S->Seq = Rec.Order;
        memcpy(&S->Payload, &((const SAMPLE_APP_StatusTlm_t *)Ptr)->Payload, sizeof(S->Payload));
        for (i = 0; i < Rec.StampCount; i++)
        {
            if (Rec.StampedPtr[i] == Ptr)
            {
                S->TimeStamped = true;
            }
        }
        ++Rec.StatusCount;
    }
    ++Rec.Order;
    return StubRetcode;
}

static int32 Hook_Event(void *UserObj, int32 StubRetcode, uint32 CallCount, const UT_StubContext_t *Context, va_list va)
{
    if (Rec.EventCount < REC_MAX_EVENTS)
    {
        Rec_Event_t *E    = &Rec.Events[Rec.EventCount++];
        const char  *Spec = UT_Hook_GetArgValueByName(Context, "Spec", const char *);

        E->Id   = UT_Hook_GetArgValueByName(Context, "EventID", uint16);
        E->Type = (uint16)UT_Hook_GetArgValueByName(Context, "EventType", CFE_EVS_EventType_Enum_t);
        if (Spec != NULL)
        {
            vsnprintf(E->Text, sizeof(E->Text), Spec, va);
        }
    }
    return 0;
}

/* Replaces the CFE_TBL_GetInfo handler: success reports Rec.TblCrc, failure
 * leaves recognisable garbage in the output structure. */
static void Handler_GetInfo(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    CFE_TBL_Info_t *Info = UT_Hook_GetArgValueByName(Context, "TblInfoPtr", CFE_TBL_Info_t *);
    int32           Status;

    UT_Stub_GetInt32StatusCode(Context, &Status);
    memset(Info, 0, sizeof(*Info));
    Info->Crc = (Status >= 0) ? TABLE_CRC : 0xBADBAD01u;
}

static SAMPLE_APP_ExampleTable_t Example_Table;

static void Handler_GetAddress(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    void **Out = UT_Hook_GetArgValueByName(Context, "TblPtr", void **);

    *Out = &Example_Table;
}

/* ---- helpers ------------------------------------------------------------ */

typedef union
{
    CFE_SB_Buffer_t               SBBuf;
    SAMPLE_APP_NoopCmd_t          Noop;
    SAMPLE_APP_ResetCountersCmd_t Reset;
    SAMPLE_APP_ProcessCmd_t       Process;
    SAMPLE_APP_DisplayParamCmd_t  Display;
    SAMPLE_APP_SetStatusRateCmd_t Rate;
    SAMPLE_APP_SendHkCmd_t        SendHk;
} Cmd_Buf_t;

/* The message being delivered; the message-service stubs answer from it by pointer,
 * so the number of times the unit under test queries a message does not matter. */
static struct
{
    const void       *Ptr;
    CFE_SB_MsgId_t    MsgId;
    CFE_MSG_FcnCode_t Fc;
    size_t            Size;
} Cur;

static void Handler_GetMsgId(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const void     *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_SB_MsgId_t *Out = UT_Hook_GetArgValueByName(Context, "MsgId", CFE_SB_MsgId_t *);

    *Out = (Msg == Cur.Ptr) ? Cur.MsgId : CFE_SB_INVALID_MSG_ID;
}

static void Handler_GetFcnCode(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const void        *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t *Out = UT_Hook_GetArgValueByName(Context, "FcnCode", CFE_MSG_FcnCode_t *);

    *Out = (Msg == Cur.Ptr) ? Cur.Fc : 0;
}

static void Handler_GetSize(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const void *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Size_t *Out = UT_Hook_GetArgValueByName(Context, "Size", CFE_MSG_Size_t *);

    *Out = (Msg == Cur.Ptr) ? Cur.Size : 0;
}

static void Pipe_Deliver(CFE_SB_MsgId_t MsgId, CFE_MSG_FcnCode_t Fc, size_t Size, Cmd_Buf_t *Buf, bool LengthIsWrong)
{
    (void)LengthIsWrong;
    Cur.Ptr   = &Buf->SBBuf;
    Cur.MsgId = MsgId;
    Cur.Fc    = Fc;
    Cur.Size  = Size;
    SAMPLE_APP_TaskPipe(&Buf->SBBuf);
}

static void Pipe_Deliver_Hk(Cmd_Buf_t *Buf)
{
    Pipe_Deliver(CFE_SB_ValueToMsgId(SAMPLE_APP_SEND_HK_MID), 0, sizeof(Buf->SendHk), Buf, false);
}

static void Send_Hk(uint32 Count)
{
    Cmd_Buf_t Buf;

    while (Count-- > 0)
    {
        memset(&Buf, 0, sizeof(Buf));
        Pipe_Deliver_Hk(&Buf);
    }
}

static void Send_Rate(uint16 Divisor, int SizeAdjust)
{
    Cmd_Buf_t Buf;

    memset(&Buf, 0, sizeof(Buf));
    Buf.Rate.Payload.Divisor = Divisor;
    Pipe_Deliver(CFE_SB_ValueToMsgId(SAMPLE_APP_CMD_MID),
                 SAMPLE_APP_SET_STATUS_RATE_CC,
                 (size_t)((int)sizeof(Buf.Rate) + SizeAdjust),
                 &Buf,
                 SizeAdjust != 0);
}

static void Send_Simple_Sized(CFE_MSG_FcnCode_t Fc, size_t Size, bool LengthIsWrong)
{
    Cmd_Buf_t Buf;

    memset(&Buf, 0, sizeof(Buf));
    Pipe_Deliver(CFE_SB_ValueToMsgId(SAMPLE_APP_CMD_MID), Fc, Size, &Buf, LengthIsWrong);
}

static void Send_Simple(CFE_MSG_FcnCode_t Fc, size_t Size)
{
    Send_Simple_Sized(Fc, Size, false);
}

static void Send_Reset(void)
{
    Send_Simple(SAMPLE_APP_RESET_COUNTERS_CC, sizeof(SAMPLE_APP_ResetCountersCmd_t));
}

static void Send_Noop(void)
{
    Send_Simple(SAMPLE_APP_NOOP_CC, sizeof(SAMPLE_APP_NoopCmd_t));
}

/* PROCESS whose table access succeeds. */
static void Send_Process_Ok(void)
{
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_GetAddress), CFE_SUCCESS);
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_ReleaseAddress), CFE_SUCCESS);
    Send_Simple(SAMPLE_APP_PROCESS_CC, sizeof(SAMPLE_APP_ProcessCmd_t));
}

static void Send_Process_GetAddressFails(void)
{
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_GetAddress), CFE_TBL_ERR_UNREGISTERED);
    Send_Simple(SAMPLE_APP_PROCESS_CC, sizeof(SAMPLE_APP_ProcessCmd_t));
    UT_ClearDefaultReturnValue(UT_KEY(CFE_TBL_GetAddress));
}

static void Send_Process_ReleaseFails(void)
{
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_GetAddress), CFE_SUCCESS);
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_ReleaseAddress), CFE_TBL_ERR_NO_ACCESS);
    Send_Simple(SAMPLE_APP_PROCESS_CC, sizeof(SAMPLE_APP_ProcessCmd_t));
    UT_ClearDefaultReturnValue(UT_KEY(CFE_TBL_ReleaseAddress));
}

/* Fresh application state with the recording hooks installed. */
static void Start_App_With_TableStatus(int32 TableInfoStatus)
{
    Rec_Reset();
    UT_SetDefaultReturnValue(UT_KEY(CFE_TBL_GetInfo), TableInfoStatus);
    UT_SetHookFunction(UT_KEY(CFE_SB_TimeStampMsg), Hook_TimeStamp, NULL);
    UT_SetHookFunction(UT_KEY(CFE_SB_TransmitMsg), Hook_Transmit, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEvent), Hook_Event, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetInfo), Handler_GetInfo, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetAddress), Handler_GetAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgId), Handler_GetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetFcnCode), Handler_GetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetSize), Handler_GetSize, NULL);
    memset(&Cur, 0, sizeof(Cur));
    UtAssert_INT32_EQ(SAMPLE_APP_Init(), CFE_SUCCESS);
    Rec_Reset();
}

static void Start_App(void)
{
    Start_App_With_TableStatus(CFE_SUCCESS);
}

static uint32 Count_Events(uint16 Id)
{
    uint32 n = 0;
    uint32 i;

    for (i = 0; i < Rec.EventCount; i++)
    {
        if (Rec.Events[i].Id == Id)
        {
            ++n;
        }
    }
    return n;
}

static const Rec_Event_t *Last_Event(uint16 Id)
{
    uint32 i;

    for (i = Rec.EventCount; i > 0; i--)
    {
        if (Rec.Events[i - 1].Id == Id)
        {
            return &Rec.Events[i - 1];
        }
    }
    return NULL;
}

static const Rec_Status_t *Last_Status(void)
{
    return &Rec.Status[(Rec.StatusCount > 0) ? (Rec.StatusCount - 1) : 0];
}

static uint32 Last_Event_Type(uint16 Id)
{
    const Rec_Event_t *E = Last_Event(Id);

    return (E != NULL) ? E->Type : 0xFFFFu;
}

static bool Text_Has_Number(const Rec_Event_t *E, uint32 Value)
{
    char needle[16];

    snprintf(needle, sizeof(needle), "%lu", (unsigned long)Value);
    return E != NULL && strstr(E->Text, needle) != NULL;
}

/* Cycle numbers (HkCycleCount) of the recorded status packets, in order. */
static bool Status_Cycles_Are(const uint16 *Expected, uint32 N)
{
    uint32 i;

    if (Rec.StatusCount != N)
    {
        UtPrintf("status packets: got %u, wanted %u", (unsigned int)Rec.StatusCount, (unsigned int)N);
        return false;
    }
    for (i = 0; i < N; i++)
    {
        if (Rec.Status[i].Payload.HkCycleCount != Expected[i])
        {
            UtPrintf("status packet %u: HkCycleCount %u, wanted %u",
                     (unsigned int)i,
                     (unsigned int)Rec.Status[i].Payload.HkCycleCount,
                     (unsigned int)Expected[i]);
            return false;
        }
    }
    return true;
}

/* ---- tests --------------------------------------------------------------- */

void Test_Hidden_DefaultDivisor(void)
{
    static const uint16 Expect[] = {2, 4, 6};
    uint32              i;

    Start_App();
    Send_Hk(6);

    UtAssert_UINT32_EQ(Rec.HkCount, 6);
    UtAssert_True(Status_Cycles_Are(Expect, 3), "status packets only on cycles 2, 4, 6 with the default divisor");
    for (i = 0; i < Rec.StatusCount; i++)
    {
        UtAssert_UINT32_EQ(Rec.Status[i].Payload.StatusDivisor, SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR);
        UtAssert_UINT32_EQ(Rec.Status[i].Payload.ProcessCmdCount, 0);
        UtAssert_True(Rec.Status[i].TimeStamped, "status packet is time-stamped before transmission");
        /* The status packet follows the housekeeping packet of the same cycle. */
        UtAssert_True(Rec.Status[i].Seq > Rec.HkSeq[2 * i + 1], "status packet is sent after that cycle's housekeeping");
        if (i + 1 < Rec.StatusCount)
        {
            UtAssert_True(Rec.Status[i].Seq < Rec.HkSeq[2 * i + 2], "status packet precedes the next cycle's housekeeping");
        }
    }
    UtAssert_UINT32_EQ(Rec.StampCount, Rec.HkCount + Rec.StatusCount);
}

void Test_Hidden_DivisorBoundaries(void)
{
    static const uint16 Rejected[] = {0, 101, 200, 65535};
    uint32              i;
    uint8               Errors;
    uint8               Good;

    Start_App();

    /* Rejected values: error counter and event 14 carrying the value; divisor unchanged. */
    for (i = 0; i < sizeof(Rejected) / sizeof(Rejected[0]); i++)
    {
        Errors = SAMPLE_APP_Data.CommandErrorCounter;
        Good   = SAMPLE_APP_Data.CommandCounter;
        Rec.EventCount = 0;

        Send_Rate(Rejected[i], 0);

        UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandErrorCounter, (uint8)(Errors + 1));
        UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandCounter, Good);
        UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_ERR_EID), 1);
        UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_INF_EID), 0);
        UtAssert_UINT32_EQ(Last_Event_Type(SAMPLE_APP_STATUS_RATE_ERR_EID), CFE_EVS_EventType_ERROR);
        UtAssert_True(Text_Has_Number(Last_Event(SAMPLE_APP_STATUS_RATE_ERR_EID), Rejected[i]),
                      "error event text contains the rejected value");
    }

    /* Divisor unchanged: still the default, so cycle 2 emits with divisor 2. */
    Send_Hk(2);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.HkCycleCount, 2);
}

void Test_Hidden_AcceptedDivisors(void)
{
    uint8 Good;

    /* Divisor 1: every cycle. */
    Start_App();
    Good = SAMPLE_APP_Data.CommandCounter;
    Send_Rate(1, 0);
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandCounter, (uint8)(Good + 1));
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandErrorCounter, 0);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_INF_EID), 1);
    UtAssert_UINT32_EQ(Last_Event_Type(SAMPLE_APP_STATUS_RATE_INF_EID), CFE_EVS_EventType_INFORMATION);
    UtAssert_True(Text_Has_Number(Last_Event(SAMPLE_APP_STATUS_RATE_INF_EID), 1), "information event text contains the divisor");
    Send_Hk(3);
    UtAssert_UINT32_EQ(Rec.StatusCount, 3);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, 1);
    UtAssert_UINT32_EQ(Rec.Status[2].Payload.HkCycleCount, 3);

    /* Divisor 100: exactly one packet, on the 100th cycle. */
    Start_App();
    Send_Rate(100, 0);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_INF_EID), 1);
    UtAssert_True(Text_Has_Number(Last_Event(SAMPLE_APP_STATUS_RATE_INF_EID), 100), "information event text contains 100");
    Send_Hk(99);
    UtAssert_UINT32_EQ(Rec.StatusCount, 0);
    Send_Hk(1);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, 100);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.HkCycleCount, 100);
}

void Test_Hidden_EmissionIsMultipleOfDivisor(void)
{
    static const uint16 Expect[] = {2, 3, 6};

    /* Two cycles at divisor 2 (one packet), then divisor 3: emission follows
     * "HkCycleCount is a multiple of the current divisor". */
    Start_App();
    Send_Hk(2);
    Send_Rate(3, 0);
    Send_Hk(4);
    UtAssert_True(Status_Cycles_Are(Expect, 3), "packets on cycles 2 (divisor 2), then 3 and 6 (divisor 3)");
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, 2);
    UtAssert_UINT32_EQ(Rec.Status[1].Payload.StatusDivisor, 3);
    UtAssert_UINT32_EQ(Rec.Status[2].Payload.StatusDivisor, 3);
}

void Test_Hidden_WrongLengthRate(void)
{
    Start_App();
    Send_Rate(7, -1);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_CMD_LEN_ERR_EID), 1);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_INF_EID), 0);
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandErrorCounter, 1);
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandCounter, 0);
    Send_Rate(7, 1);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_CMD_LEN_ERR_EID), 2);
    UtAssert_UINT32_EQ(Count_Events(SAMPLE_APP_STATUS_RATE_INF_EID), 0);

    /* Divisor still the default. */
    Send_Hk(2);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, SAMPLE_APP_PLATFORM_DEFAULT_STATUS_DIVISOR);
}

void Test_Hidden_ResetCounters(void)
{
    /* Divisor 1: the packet right after RESET shows both counters at zero. */
    Start_App();
    Send_Rate(1, 0);
    Send_Process_Ok();
    Send_Process_Ok();
    Send_Hk(3);
    UtAssert_UINT32_EQ(Rec.StatusCount, 3);
    UtAssert_UINT32_EQ(Rec.Status[2].Payload.ProcessCmdCount, 2);
    UtAssert_UINT32_EQ(Rec.Status[2].Payload.HkCycleCount, 3);

    Send_Reset();
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandCounter, 0);
    UtAssert_UINT32_EQ(SAMPLE_APP_Data.CommandErrorCounter, 0);
    Rec.StatusCount = 0;
    Send_Hk(1);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.ProcessCmdCount, 0);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.HkCycleCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, 1);

    /* Divisor 5: reset restarts the cycle count and keeps the divisor. */
    Start_App();
    Send_Rate(5, 0);
    Send_Hk(5);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    Send_Hk(2);
    Send_Reset();
    Rec.StatusCount = 0;
    Send_Hk(4);
    UtAssert_UINT32_EQ(Rec.StatusCount, 0);
    Send_Hk(1);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.StatusDivisor, 5);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.HkCycleCount, 5);
}

void Test_Hidden_ProcessCmdCount(void)
{
    Start_App();
    Send_Rate(1, 0);

    Send_Process_Ok();
    Send_Hk(1);
    UtAssert_UINT32_EQ(Last_Status()->Payload.ProcessCmdCount, 1);

    Send_Process_Ok();
    Send_Hk(1);
    UtAssert_UINT32_EQ(Last_Status()->Payload.ProcessCmdCount, 2);

    /* Commands that do not complete successfully, or are not PROCESS, do not count. */
    Send_Process_GetAddressFails();
    Send_Process_ReleaseFails();
    Send_Simple_Sized(SAMPLE_APP_PROCESS_CC, sizeof(SAMPLE_APP_ProcessCmd_t) + 1, true); /* wrong length */
    Send_Noop();
    Send_Simple(SAMPLE_APP_DISPLAY_PARAM_CC, sizeof(SAMPLE_APP_DisplayParamCmd_t));
    Send_Rate(1, 0);
    Send_Rate(0, 0);
    Send_Hk(1);
    UtAssert_UINT32_EQ(Last_Status()->Payload.ProcessCmdCount, 2);

    Send_Process_Ok();
    Send_Hk(1);
    UtAssert_UINT32_EQ(Last_Status()->Payload.ProcessCmdCount, 3);
}

void Test_Hidden_TableCrc(void)
{
    /* The CRC comes from CFE_TBL_GetInfo; 0 when the table information is unavailable. */
    Start_App();
    Send_Rate(1, 0);
    Send_Hk(1);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.TableCrc, TABLE_CRC);

    Start_App_With_TableStatus(CFE_TBL_ERR_INVALID_NAME);
    Send_Rate(1, 0);
    Send_Hk(1);
    UtAssert_UINT32_EQ(Rec.StatusCount, 1);
    UtAssert_UINT32_EQ(Rec.Status[0].Payload.TableCrc, 0);
}

/* ---- registration --------------------------------------------------------- */

void UtTest_Setup(void)
{
    ADD_TEST(Hidden_DefaultDivisor);
    ADD_TEST(Hidden_DivisorBoundaries);
    ADD_TEST(Hidden_AcceptedDivisors);
    ADD_TEST(Hidden_EmissionIsMultipleOfDivisor);
    ADD_TEST(Hidden_WrongLengthRate);
    ADD_TEST(Hidden_ResetCounters);
    ADD_TEST(Hidden_ProcessCmdCount);
    ADD_TEST(Hidden_TableCrc);
}
