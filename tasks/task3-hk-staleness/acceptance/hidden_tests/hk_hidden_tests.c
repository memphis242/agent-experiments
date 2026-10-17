/*
 * Behavioral unit tests for HK input staleness tracking.
 *
 * Built against the real HK sources (hk_app.c, hk_cmds.c, hk_dispatch.c and
 * hk_utils.c) with only the cFE core API stubs from the UT framework. All
 * state is observed through the interfaces named by the task: the pipe
 * dispatcher, the combined send, the incoming-data handler, the copy table
 * status check, the housekeeping packet and the transmitted packets.
 *
 * Test case names are at most 30 characters because the UT framework
 * truncates them in its log; the driver maps the leading "Tnn" tag to a
 * requirement area.
 */

#include "hk_app.h"
#include "hk_cmds.h"
#include "hk_dispatch.h"
#include "hk_msg.h"
#include "hk_events.h"
#include "hk_msgids.h"
#include "hk_utils.h"

#include "uttest.h"
#include "utassert.h"
#include "utstubs.h"

#include "cfe.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Fixture constants                                                  */
/* ------------------------------------------------------------------ */

#define T_IN_A  0x0A31u
#define T_IN_B  0x0A32u
#define T_IN_C  0x0A33u
#define T_OUT1  0x0A41u
#define T_OUT2  0x0A42u
#define T_IN_X  0x0A50u /* not in any table */

#define T_BULK_IN_BASE 0x0B00u
#define T_BULK_ENTRIES 20u

#define T_MAX_EVENTS   512u
#define T_MAX_REGISTRY 256u
#define T_MAX_TX       256u
#define T_MAX_PKT      2048u
#define T_POOL_BUFFERS 64u

#define T_COPY_HANDLE    ((CFE_TBL_Handle_t)11)
#define T_RUNTIME_HANDLE ((CFE_TBL_Handle_t)12)

#define T_UNKNOWN_MID 0xFFFFFFFFu

/* ------------------------------------------------------------------ */
/* Fake message registry (replaces the CFE_MSG stubs)                  */
/* ------------------------------------------------------------------ */

typedef struct
{
    const void *Ptr;
    uint32      Mid;
    size_t      Size;
    uint32      Cc;
} T_RegistryEntry_t;

static T_RegistryEntry_t T_Registry[T_MAX_REGISTRY];
static uint32            T_RegistryCount;

static T_RegistryEntry_t *T_Registry_Find(const void *Ptr)
{
    uint32 i;

    for (i = 0; i < T_RegistryCount; i++)
    {
        if (T_Registry[i].Ptr == Ptr)
        {
            return &T_Registry[i];
        }
    }
    return NULL;
}

static void T_Registry_Set(const void *Ptr, uint32 Mid, size_t Size, uint32 Cc)
{
    T_RegistryEntry_t *Entry = T_Registry_Find(Ptr);

    if (Entry == NULL)
    {
        UtAssert_True(T_RegistryCount < T_MAX_REGISTRY, "message registry has room");
        if (T_RegistryCount >= T_MAX_REGISTRY)
        {
            return;
        }
        Entry = &T_Registry[T_RegistryCount++];
    }
    Entry->Ptr  = Ptr;
    Entry->Mid  = Mid;
    Entry->Size = Size;
    Entry->Cc   = Cc;
}

static void T_Handler_MSG_GetMsgId(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_SB_MsgId_t          *Out = UT_Hook_GetArgValueByName(Context, "MsgId", CFE_SB_MsgId_t *);
    T_RegistryEntry_t       *E   = T_Registry_Find(Msg);

    *Out = CFE_SB_ValueToMsgId((E != NULL) ? E->Mid : 0);
}

static void T_Handler_MSG_GetFcnCode(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t       *Out = UT_Hook_GetArgValueByName(Context, "FcnCode", CFE_MSG_FcnCode_t *);
    T_RegistryEntry_t       *E   = T_Registry_Find(Msg);

    *Out = (CFE_MSG_FcnCode_t)((E != NULL) ? E->Cc : 0);
}

static void T_Handler_MSG_GetSize(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Size_t          *Out = UT_Hook_GetArgValueByName(Context, "Size", CFE_MSG_Size_t *);
    T_RegistryEntry_t       *E   = T_Registry_Find(Msg);

    *Out = (CFE_MSG_Size_t)((E != NULL) ? E->Size : 0);
}

static void T_Handler_MSG_Init(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Msg  = UT_Hook_GetArgValueByName(Context, "MsgPtr", CFE_MSG_Message_t *);
    CFE_SB_MsgId_t           Mid  = UT_Hook_GetArgValueByName(Context, "MsgId", CFE_SB_MsgId_t);
    CFE_MSG_Size_t           Size = UT_Hook_GetArgValueByName(Context, "Size", CFE_MSG_Size_t);

    T_Registry_Set(Msg, CFE_SB_MsgIdToValue(Mid), Size, 0);
}

/* ------------------------------------------------------------------ */
/* Event capture                                                       */
/* ------------------------------------------------------------------ */

typedef struct
{
    uint16 EventID;
    uint16 EventType;
    char   Text[CFE_MISSION_EVS_MAX_MESSAGE_LENGTH];
} T_Event_t;

static T_Event_t T_Events[T_MAX_EVENTS];
static uint32    T_EventCount;

static void T_Handler_EVS_SendEvent(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context, va_list va)
{
    T_Event_t  *E;
    const char *Spec = UT_Hook_GetArgValueByName(Context, "Spec", const char *);

    if (T_EventCount >= T_MAX_EVENTS)
    {
        return;
    }
    E            = &T_Events[T_EventCount++];
    E->EventID   = UT_Hook_GetArgValueByName(Context, "EventID", uint16);
    E->EventType = UT_Hook_GetArgValueByName(Context, "EventType", uint16);
    vsnprintf(E->Text, sizeof(E->Text), Spec, va);
}

static uint32 T_CountEvents(uint16 EventID)
{
    uint32 i;
    uint32 N = 0;

    for (i = 0; i < T_EventCount; i++)
    {
        if (T_Events[i].EventID == EventID)
        {
            N++;
        }
    }
    return N;
}

static const T_Event_t *T_LastEvent(uint16 EventID)
{
    uint32 i;

    for (i = T_EventCount; i > 0; i--)
    {
        if (T_Events[i - 1].EventID == EventID)
        {
            return &T_Events[i - 1];
        }
    }
    return NULL;
}

/* True when the text holds a maximal run of hex digits whose value is Value. */
static bool T_TextHasHexValue(const char *Text, uint32 Value)
{
    const char *p = Text;

    while (*p != '\0')
    {
        if ((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f') || (*p >= 'A' && *p <= 'F'))
        {
            unsigned long V = 0;

            while ((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f') || (*p >= 'A' && *p <= 'F'))
            {
                unsigned int D = (*p <= '9') ? (unsigned int)(*p - '0') : (unsigned int)((*p | 0x20) - 'a' + 10);

                V = (V << 4) | D;
                p++;
            }
            if (V == (unsigned long)Value)
            {
                return true;
            }
        }
        else
        {
            p++;
        }
    }
    return false;
}

/* True when the event text has a standalone decimal number equal to Value. */
static bool T_TextHasDecimal(const char *Text, unsigned long Value)
{
    const char *p = Text;

    while (*p != '\0')
    {
        if (*p >= '0' && *p <= '9')
        {
            unsigned long V = 0;

            while (*p >= '0' && *p <= '9')
            {
                V = V * 10 + (unsigned long)(*p - '0');
                p++;
            }
            if (V == Value)
            {
                return true;
            }
        }
        else
        {
            p++;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* Transmit capture                                                    */
/* ------------------------------------------------------------------ */

typedef struct
{
    uint32 Mid;
    size_t Size;
    uint8  Bytes[T_MAX_PKT];
} T_Tx_t;

static T_Tx_t T_Tx[T_MAX_TX];
static uint32 T_TxCount;

static void T_Handler_SB_TransmitMsg(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    const CFE_MSG_Message_t *Msg = UT_Hook_GetArgValueByName(Context, "MsgPtr", const CFE_MSG_Message_t *);
    T_RegistryEntry_t       *E   = T_Registry_Find(Msg);
    T_Tx_t                  *Tx;
    size_t                   N;

    if (T_TxCount >= T_MAX_TX)
    {
        return;
    }
    Tx       = &T_Tx[T_TxCount++];
    Tx->Mid  = (E != NULL) ? E->Mid : T_UNKNOWN_MID;
    Tx->Size = (E != NULL) ? E->Size : sizeof(HK_StaleReportTlm_t);
    N        = Tx->Size;
    if (N > T_MAX_PKT)
    {
        N = T_MAX_PKT;
    }
    memcpy(Tx->Bytes, Msg, N);
}

/* ------------------------------------------------------------------ */
/* Memory pool and table fakes                                         */
/* ------------------------------------------------------------------ */

typedef union
{
    CFE_SB_Buffer_t Buf;
    uint8           Bytes[T_MAX_PKT];
    uint64          Align;
} T_PoolBuffer_t;

static T_PoolBuffer_t T_Pool[T_POOL_BUFFERS];
static uint32         T_PoolUsed;

static uint32                  T_RegisterCalls;
static HK_CopyTableEntry_t    T_CopyTable[HK_COPY_TABLE_ENTRIES];
static HK_RuntimeTableEntry_t T_RuntimeTable[HK_COPY_TABLE_ENTRIES];

static void T_Handler_ES_GetPoolBuf(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    void **BufPtr = UT_Hook_GetArgValueByName(Context, "BufPtr", void **);
    int32  Status;

    if (T_PoolUsed >= T_POOL_BUFFERS)
    {
        Status = -1;
    }
    else
    {
        memset(&T_Pool[T_PoolUsed], 0, sizeof(T_Pool[T_PoolUsed]));
        *BufPtr = &T_Pool[T_PoolUsed++];
        Status  = 64;
    }
    UT_Stub_SetReturnValue(FuncKey, Status);
}

static void T_Handler_TBL_Register(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    CFE_TBL_Handle_t *Out = UT_Hook_GetArgValueByName(Context, "TblHandlePtr", CFE_TBL_Handle_t *);

    int32             Status = CFE_SUCCESS;

    *Out = ((T_RegisterCalls++ % 2) == 0) ? T_COPY_HANDLE : T_RUNTIME_HANDLE;
    UT_Stub_SetReturnValue(FuncKey, Status);
}

static void T_Handler_TBL_GetAddress(void *UserObj, UT_EntryKey_t FuncKey, const UT_StubContext_t *Context)
{
    void           **Out    = UT_Hook_GetArgValueByName(Context, "TblPtr", void **);
    CFE_TBL_Handle_t Handle = UT_Hook_GetArgValueByName(Context, "TblHandle", CFE_TBL_Handle_t);
    int32            Status;

    if (Handle == T_COPY_HANDLE)
    {
        *Out   = T_CopyTable;
        Status = CFE_TBL_INFO_UPDATED;
    }
    else
    {
        *Out   = T_RuntimeTable;
        Status = CFE_SUCCESS;
    }
    UT_Stub_SetReturnValue(FuncKey, Status);
}

/* ------------------------------------------------------------------ */
/* Copy table builders                                                 */
/* ------------------------------------------------------------------ */

static void T_Table_Entry(uint32 Index, uint32 In, uint16 InOff, uint32 Out, uint16 OutOff, uint16 N)
{
    T_CopyTable[Index].InputMid     = CFE_SB_ValueToMsgId(In);
    T_CopyTable[Index].InputOffset  = InOff;
    T_CopyTable[Index].OutputMid    = CFE_SB_ValueToMsgId(Out);
    T_CopyTable[Index].OutputOffset = OutOff;
    T_CopyTable[Index].NumBytes     = N;
}

static void T_Table_Clear(void)
{
    uint32 i;

    for (i = 0; i < HK_COPY_TABLE_ENTRIES; i++)
    {
        T_Table_Entry(i, 0, 0, 0, 0, 0);
    }
}

/* A and B feed OUT1, C feeds OUT2. */
static void T_Table_Standard(void)
{
    T_Table_Clear();
    T_Table_Entry(0, T_IN_A, 12, T_OUT1, 12, 4);
    T_Table_Entry(1, T_IN_B, 12, T_OUT1, 16, 4);
    T_Table_Entry(2, T_IN_C, 12, T_OUT2, 12, 4);
}

/* Many inputs feeding one output. */
static void T_Table_Bulk(void)
{
    uint32 i;

    T_Table_Clear();
    for (i = 0; i < T_BULK_ENTRIES; i++)
    {
        T_Table_Entry(i, T_BULK_IN_BASE + i, 12, T_OUT1, (uint16)(12 + 2 * i), 2);
    }
}

/* ------------------------------------------------------------------ */
/* Driver helpers                                                      */
/* ------------------------------------------------------------------ */

static CFE_SB_Buffer_t T_CmdBuf;
static CFE_SB_Buffer_t T_InBuf[8];
static uint32          T_InBufNext;

static void T_Dispatch(uint32 Mid, uint32 Cc, const void *Cmd, size_t CmdSize)
{
    memset(&T_CmdBuf, 0, sizeof(T_CmdBuf));
    if (Cmd != NULL)
    {
        memcpy(&T_CmdBuf, Cmd, CmdSize);
    }
    T_Registry_Set(&T_CmdBuf, Mid, CmdSize, Cc);
    HK_AppPipe(&T_CmdBuf);
}

static void T_SetStaleCycles(uint8 Value)
{
    HK_SetStaleCyclesCmd_t Cmd;

    memset(&Cmd, 0, sizeof(Cmd));
    Cmd.Payload.StaleCycles = Value;
    T_Dispatch(HK_CMD_MID, HK_SET_STALE_CYCLES_CC, &Cmd, sizeof(Cmd));
}

static void T_ReportStale(void)
{
    HK_ReportStaleCmd_t Cmd;

    memset(&Cmd, 0, sizeof(Cmd));
    T_Dispatch(HK_CMD_MID, HK_REPORT_STALE_CC, &Cmd, sizeof(Cmd));
}

static void T_ResetCounters(void)
{
    HK_ResetCountersCmd_t Cmd;

    memset(&Cmd, 0, sizeof(Cmd));
    T_Dispatch(HK_CMD_MID, HK_RESET_COUNTERS_CC, &Cmd, sizeof(Cmd));
}

/* Requests housekeeping through the dispatcher and returns the payload sent. */
static HK_HkTlm_Payload_t T_RequestHk(void)
{
    HK_SendHkCmd_t Cmd;
    uint32         Before = T_TxCount;

    memset(&Cmd, 0, sizeof(Cmd));
    T_Dispatch(HK_SEND_HK_MID, 0, &Cmd, sizeof(Cmd));
    UtAssert_True(T_TxCount == Before + 1, "housekeeping request transmits exactly one packet");
    return HK_AppData.HkPacket.Payload;
}

static void T_Arrive(uint32 Mid)
{
    CFE_SB_Buffer_t *Buf = &T_InBuf[T_InBufNext++ % 8];

    memset(Buf, 0xA5, sizeof(*Buf));
    T_Registry_Set(Buf, Mid, 64, 0);
    HK_ProcessIncomingHkData(Buf);
}

static void T_Send(uint32 OutMid)
{
    HK_SendCombinedHkPacket(CFE_SB_ValueToMsgId(OutMid));
}

static void T_ClearObservations(void)
{
    T_EventCount = 0;
    T_TxCount    = 0;
}

static void T_Check_ListedEvent(uint16 EventID, uint32 In, uint32 Out, const char *What)
{
    const T_Event_t *E = T_LastEvent(EventID);

    UtAssert_True(E != NULL, "%s: event %u present", What, (unsigned int)EventID);
    if (E != NULL)
    {
        UtAssert_True(T_TextHasHexValue(E->Text, In), "%s: text names input MID in hex: '%s'", What, E->Text);
        UtAssert_True(T_TextHasHexValue(E->Text, Out), "%s: text names output MID in hex: '%s'", What, E->Text);
        UtAssert_INT32_EQ(E->EventType, CFE_EVS_EventType_INFORMATION);
    }
}

/* ------------------------------------------------------------------ */
/* Setup and teardown                                                  */
/* ------------------------------------------------------------------ */

static void Hidden_Setup(void)
{
    CFE_Status_t Status;

    UT_ResetState(0);

    memset(&HK_AppData, 0, sizeof(HK_AppData));
    memset(T_Registry, 0, sizeof(T_Registry));
    memset(T_Events, 0, sizeof(T_Events));
    memset(T_RuntimeTable, 0, sizeof(T_RuntimeTable));
    T_RegistryCount = 0;
    T_EventCount    = 0;
    T_TxCount       = 0;
    T_PoolUsed      = 0;
    T_RegisterCalls = 0;
    T_InBufNext     = 0;

    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgId), T_Handler_MSG_GetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetFcnCode), T_Handler_MSG_GetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetSize), T_Handler_MSG_GetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_Init), T_Handler_MSG_Init, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg), T_Handler_SB_TransmitMsg, NULL);
    UT_SetVaHandlerFunction(UT_KEY(CFE_EVS_SendEvent), T_Handler_EVS_SendEvent, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_ES_GetPoolBuf), T_Handler_ES_GetPoolBuf, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Register), T_Handler_TBL_Register, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetAddress), T_Handler_TBL_GetAddress, NULL);

    T_Table_Standard();
    Status = HK_AppInit();
    UtAssert_INT32_EQ(Status, CFE_SUCCESS);
    UtAssert_True(HK_AppData.CopyTablePtr == T_CopyTable, "copy table installed by init");
    UtAssert_True(HK_AppData.RuntimeTablePtr == T_RuntimeTable, "runtime table installed by init");

    T_ClearObservations();
}

static void Hidden_Teardown(void)
{
}

/* Reloads the copy table the way HK handles a pending table update. */
static void T_ReloadCopyTable(void)
{
    UT_SetDeferredRetcode(UT_KEY(CFE_TBL_GetStatus), 1, CFE_TBL_INFO_UPDATE_PENDING);
    UtAssert_INT32_EQ(HK_CheckStatusOfCopyTable(), HK_SUCCESS);
}

/* ------------------------------------------------------------------ */
/* T01: default threshold                                              */
/* ------------------------------------------------------------------ */

static void Test_T01_Default(void)
{
    HK_HkTlm_Payload_t Hk = T_RequestHk();

    UtAssert_UINT32_EQ(Hk.StaleCycles, 3);
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 0);
    UtAssert_UINT32_EQ(Hk.CommandCounter, 0);
    UtAssert_UINT32_EQ(Hk.CommandErrorCounter, 0);
}

/* ------------------------------------------------------------------ */
/* T02: valid thresholds                                               */
/* ------------------------------------------------------------------ */

static void Test_T02_SetValid(void)
{
    static const uint8 Values[] = {1, 2, 17, 254, 255};
    uint32             i;

    for (i = 0; i < sizeof(Values) / sizeof(Values[0]); i++)
    {
        HK_HkTlm_Payload_t Hk;
        const T_Event_t   *E;

        T_ClearObservations();
        T_SetStaleCycles(Values[i]);

        UtAssert_UINT32_EQ(T_CountEvents(HK_STALE_CYCLES_INF_EID), 1);
        UtAssert_UINT32_EQ(T_CountEvents(HK_STALE_CYCLES_ERR_EID), 0);
        E = T_LastEvent(HK_STALE_CYCLES_INF_EID);
        UtAssert_True(E != NULL, "threshold accepted event present");
        if (E != NULL)
        {
            UtAssert_INT32_EQ(E->EventType, CFE_EVS_EventType_INFORMATION);
            UtAssert_True(T_TextHasDecimal(E->Text, Values[i]), "event text contains the new value: '%s'", E->Text);
        }

        Hk = T_RequestHk();
        UtAssert_UINT32_EQ(Hk.StaleCycles, Values[i]);
        UtAssert_UINT32_EQ(Hk.CommandCounter, i + 1);
        UtAssert_UINT32_EQ(Hk.CommandErrorCounter, 0);
    }
}

/* ------------------------------------------------------------------ */
/* T03: invalid threshold                                              */
/* ------------------------------------------------------------------ */

static void Test_T03_SetZero(void)
{
    HK_HkTlm_Payload_t Hk;
    const T_Event_t   *E;

    T_SetStaleCycles(5);
    T_ClearObservations();

    T_SetStaleCycles(0);

    UtAssert_UINT32_EQ(T_CountEvents(HK_STALE_CYCLES_ERR_EID), 1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_STALE_CYCLES_INF_EID), 0);
    E = T_LastEvent(HK_STALE_CYCLES_ERR_EID);
    UtAssert_True(E != NULL, "threshold rejected event present");
    if (E != NULL)
    {
        UtAssert_INT32_EQ(E->EventType, CFE_EVS_EventType_ERROR);
    }

    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleCycles, 5);
    UtAssert_UINT32_EQ(Hk.CommandCounter, 1);
    UtAssert_UINT32_EQ(Hk.CommandErrorCounter, 1);
}

/* ------------------------------------------------------------------ */
/* T04: stale transition exactly once, independent per entry            */
/* ------------------------------------------------------------------ */

static void Test_T04_StaleOnce(void)
{
    HK_HkTlm_Payload_t Hk;
    uint32             i;

    /* A is delivered before every send, B never, C belongs to another output. */
    for (i = 1; i <= 2; i++)
    {
        T_Arrive(T_IN_A);
        T_Send(T_OUT1);
        UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
    }
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 0);

    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* third consecutive miss of B */

    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    T_Check_ListedEvent(HK_INPUT_STALE_EID, T_IN_B, T_OUT1, "stale transition");
    {
        const T_Event_t *E = T_LastEvent(HK_INPUT_STALE_EID);

        if (E != NULL)
        {
            UtAssert_True(!T_TextHasHexValue(E->Text, T_IN_A), "stale event does not name the fresh input");
        }
    }

    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 1);

    /* No repeat while it stays stale. */
    for (i = 0; i < 3; i++)
    {
        T_Arrive(T_IN_A);
        T_Send(T_OUT1);
    }
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 0);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 1);
}

static void Test_T04b_TwoMissing(void)
{
    HK_HkTlm_Payload_t Hk;
    uint32             i;

    /* A and B both missing in the same output: each entry is judged on its own. */
    for (i = 0; i < 3; i++)
    {
        UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
        T_Send(T_OUT1);
    }
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 2);

    {
        bool SawA = false;
        bool SawB = false;

        for (i = 0; i < T_EventCount; i++)
        {
            if (T_Events[i].EventID == HK_INPUT_STALE_EID)
            {
                SawA = SawA || T_TextHasHexValue(T_Events[i].Text, T_IN_A);
                SawB = SawB || T_TextHasHexValue(T_Events[i].Text, T_IN_B);
            }
        }
        UtAssert_True(SawA, "stale event for input A");
        UtAssert_True(SawB, "stale event for input B");
    }

    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 2);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 2);

    /* Sends of the other output never advance entries of this one. */
    T_ClearObservations();
    T_Send(T_OUT2);
    T_Send(T_OUT2);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 2);
}

static void Test_T04c_Consecutive(void)
{
    HK_HkTlm_Payload_t Hk;

    /* Misses must be consecutive: an arrival restarts the count. */
    T_SetStaleCycles(3);
    T_Arrive(T_IN_A);
    T_Arrive(T_IN_B);
    T_Send(T_OUT1); /* both present */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B miss 1 */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B miss 2 */
    T_Arrive(T_IN_A);
    T_Arrive(T_IN_B);
    T_Send(T_OUT1); /* B present: restart */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B miss 1 */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B miss 2 */
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);

    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B miss 3 */
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
}

static void Test_T04d_ThresholdOne(void)
{
    HK_HkTlm_Payload_t Hk;

    T_SetStaleCycles(1);
    T_Arrive(T_IN_A);
    T_Arrive(T_IN_B);
    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);

    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* first miss of B reaches the threshold */
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    T_Check_ListedEvent(HK_INPUT_STALE_EID, T_IN_B, T_OUT1, "threshold one");
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
}

/* ------------------------------------------------------------------ */
/* T05: threshold change applies at the next combined send             */
/* ------------------------------------------------------------------ */

static void Test_T05_LazyThreshold(void)
{
    HK_HkTlm_Payload_t Hk;

    T_SetStaleCycles(5);
    T_Send(T_OUT1);
    T_Send(T_OUT1);
    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);

    T_SetStaleCycles(2);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleCycles, 2);
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);

    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 2);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 2);
}

/* ------------------------------------------------------------------ */
/* T06: fresh transition exactly once                                  */
/* ------------------------------------------------------------------ */

static void T_MakeBStale(void)
{
    uint32 i;

    for (i = 0; i < 3; i++)
    {
        T_Arrive(T_IN_A);
        T_Send(T_OUT1);
    }
}

static void Test_T06_FreshOnce(void)
{
    HK_HkTlm_Payload_t Hk;

    T_MakeBStale();
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);

    /* Unrelated and fresh inputs do not produce fresh events. */
    T_Arrive(T_IN_X);
    T_Arrive(T_IN_A);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 0);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);

    T_Arrive(T_IN_B);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 1);
    T_Check_ListedEvent(HK_INPUT_FRESH_EID, T_IN_B, T_OUT1, "fresh transition");
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 1);

    /* A second arrival of an already fresh input is silent. */
    T_Arrive(T_IN_B);
    T_Arrive(T_IN_B);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 1);

    /* Sending after the fresh arrival does not re-trigger anything. */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 1);
}

static void Test_T06b_StaleAgain(void)
{
    HK_HkTlm_Payload_t Hk;

    T_MakeBStale();
    T_Arrive(T_IN_B);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 1);

    /* After going fresh, a full threshold of new misses is needed. */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* B present */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* miss 1 */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* miss 2 */
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);

    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* miss 3 */
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 2);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 2);
}

/* ------------------------------------------------------------------ */
/* T07: stale report contents                                          */
/* ------------------------------------------------------------------ */

static const HK_StaleReportTlm_t *T_Report(void)
{
    UtAssert_INT32_EQ(T_TxCount, 1);
    if (T_TxCount != 1)
    {
        return NULL;
    }
    UtAssert_True(T_Tx[0].Size >= sizeof(HK_StaleReportTlm_t), "report packet has the declared size");
    if (T_Tx[0].Mid != T_UNKNOWN_MID)
    {
        UtAssert_UINT32_EQ(T_Tx[0].Mid, HK_STALE_RPT_TLM_MID);
    }
    return (const HK_StaleReportTlm_t *)(const void *)T_Tx[0].Bytes;
}

static void T_Check_Unused(const HK_StaleReport_Payload_t *P, uint32 From)
{
    uint32 i;

    for (i = From; i < HK_STALE_REPORT_MAX_ENTRIES; i++)
    {
        UtAssert_True(CFE_SB_MsgIdToValue(P->InputMid[i]) == CFE_SB_MsgIdToValue(CFE_SB_INVALID_MSG_ID),
                      "unused input slot %u invalid",
                      (unsigned int)i);
        UtAssert_True(CFE_SB_MsgIdToValue(P->OutputMid[i]) == CFE_SB_MsgIdToValue(CFE_SB_INVALID_MSG_ID),
                      "unused output slot %u invalid",
                      (unsigned int)i);
    }
}

static void Test_T07_ReportNone(void)
{
    const HK_StaleReportTlm_t *R;
    HK_HkTlm_Payload_t         Hk;

    T_ReportStale();
    R = T_Report();
    if (R != NULL)
    {
        UtAssert_UINT32_EQ(R->Payload.StaleCount, 0);
        UtAssert_UINT32_EQ(R->Payload.ReportedCount, 0);
        T_Check_Unused(&R->Payload, 0);
    }
    T_ClearObservations();
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.CommandCounter, 1);
    UtAssert_UINT32_EQ(Hk.CommandErrorCounter, 0);
}

static void Test_T07b_ReportTwo(void)
{
    const HK_StaleReportTlm_t *R;
    uint32                     i;
    HK_HkTlm_Payload_t         Hk;

    /* B (table index 1) and C (index 2) go stale, A stays fresh. */
    for (i = 0; i < 3; i++)
    {
        T_Arrive(T_IN_A);
        T_Send(T_OUT1);
        T_Send(T_OUT2);
    }
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 2);

    T_ClearObservations();
    T_ReportStale();
    R = T_Report();
    if (R != NULL)
    {
        UtAssert_UINT32_EQ(R->Payload.StaleCount, 2);
        UtAssert_UINT32_EQ(R->Payload.ReportedCount, 2);
        UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.InputMid[0]), T_IN_B);
        UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.OutputMid[0]), T_OUT1);
        UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.InputMid[1]), T_IN_C);
        UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.OutputMid[1]), T_OUT2);
        T_Check_Unused(&R->Payload, 2);
    }

    T_ClearObservations();
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.CommandCounter, 1);
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 2);
}

static void Test_T07c_ReportCapped(void)
{
    const HK_StaleReportTlm_t *R;
    uint32                     i;
    HK_HkTlm_Payload_t         Hk;

    T_Table_Bulk();
    T_ReloadCopyTable();
    T_ClearObservations();

    for (i = 0; i < 3; i++)
    {
        T_Send(T_OUT1);
    }
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), T_BULK_ENTRIES);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, T_BULK_ENTRIES);

    T_ClearObservations();
    T_ReportStale();
    R = T_Report();
    if (R != NULL)
    {
        UtAssert_UINT32_EQ(R->Payload.StaleCount, T_BULK_ENTRIES);
        UtAssert_UINT32_EQ(R->Payload.ReportedCount, HK_STALE_REPORT_MAX_ENTRIES);
        for (i = 0; i < HK_STALE_REPORT_MAX_ENTRIES; i++)
        {
            UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.InputMid[i]), T_BULK_IN_BASE + i);
            UtAssert_UINT32_EQ(CFE_SB_MsgIdToValue(R->Payload.OutputMid[i]), T_OUT1);
        }
    }
}

/* ------------------------------------------------------------------ */
/* T08: reset counters                                                 */
/* ------------------------------------------------------------------ */

static void Test_T08_ResetCounters(void)
{
    HK_HkTlm_Payload_t Hk;

    T_SetStaleCycles(3);
    T_MakeBStale();
    T_Arrive(T_IN_B);
    T_MakeBStale();
    T_Arrive(T_IN_A);
    T_Send(T_OUT1); /* make sure B still counts as stale-capable */

    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 2);
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
    UtAssert_UINT32_EQ(Hk.StaleCycles, 3);
    UtAssert_UINT32_EQ(Hk.CommandCounter, 1);

    T_ClearObservations();
    T_ResetCounters();
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 0);
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);
    UtAssert_UINT32_EQ(Hk.StaleCycles, 3);
    UtAssert_UINT32_EQ(Hk.CommandCounter, 0);
    UtAssert_UINT32_EQ(Hk.CommandErrorCounter, 0);

    /* The stale state itself survived the reset: no new transition, and the fresh transition still fires. */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
    T_Arrive(T_IN_B);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 1);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 0);
}

static void Test_T08b_ResetFunction(void)
{
    HK_HkTlm_Payload_t Hk;

    T_SetStaleCycles(7);
    T_MakeBStale();
    T_SetStaleCycles(2);
    T_Send(T_OUT1);

    HK_ResetHkData();
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleTransitionCtr, 0);
    UtAssert_UINT32_EQ(Hk.StaleCycles, 2);
}

/* ------------------------------------------------------------------ */
/* T09: copy table reload                                              */
/* ------------------------------------------------------------------ */

static void Test_T09_TableReload(void)
{
    HK_HkTlm_Payload_t Hk;

    T_MakeBStale();
    T_Send(T_OUT2);
    T_Send(T_OUT2);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 1);

    T_ClearObservations();
    T_ReloadCopyTable();

    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_FRESH_EID), 0);

    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleInputCount, 0);

    /* Missed-cycle counts restarted: two sends are not enough, the third is. */
    T_Arrive(T_IN_A);
    T_Send(T_OUT1);
    T_Arrive(T_IN_A);
    T_Send(T_OUT1);
    T_Arrive(T_IN_A);
    T_Send(T_OUT2);
    T_Send(T_OUT2);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 0);

    T_Arrive(T_IN_A);
    T_Send(T_OUT1);
    UtAssert_UINT32_EQ(T_CountEvents(HK_INPUT_STALE_EID), 1);
    T_Check_ListedEvent(HK_INPUT_STALE_EID, T_IN_B, T_OUT1, "stale after reload");
}

/* ------------------------------------------------------------------ */
/* T10: command validation                                             */
/* ------------------------------------------------------------------ */

static void Test_T10_BadLength(void)
{
    HK_HkTlm_Payload_t Hk;
    uint8              Raw[64];

    memset(Raw, 0, sizeof(Raw));
    Raw[8] = 9;

    /* One byte too long for the threshold command: rejected, threshold unchanged. */
    T_Dispatch(HK_CMD_MID, HK_SET_STALE_CYCLES_CC, Raw, sizeof(HK_SetStaleCyclesCmd_t) + 1);
    /* Report command with the wrong length: nothing transmitted. */
    T_Dispatch(HK_CMD_MID, HK_REPORT_STALE_CC, Raw, sizeof(HK_ReportStaleCmd_t) + 4);

    UtAssert_UINT32_EQ(T_CountEvents(HK_STALE_CYCLES_INF_EID), 0);
    UtAssert_UINT32_EQ(T_TxCount, 0);
    Hk = T_RequestHk();
    UtAssert_UINT32_EQ(Hk.StaleCycles, 3);
    UtAssert_UINT32_EQ(Hk.CommandCounter, 0);
}

/* ------------------------------------------------------------------ */

void UtTest_Setup(void)
{
    UtTest_Add(Test_T01_Default, Hidden_Setup, Hidden_Teardown, "T01_Default");
    UtTest_Add(Test_T02_SetValid, Hidden_Setup, Hidden_Teardown, "T02_SetValid");
    UtTest_Add(Test_T03_SetZero, Hidden_Setup, Hidden_Teardown, "T03_SetZero");
    UtTest_Add(Test_T04_StaleOnce, Hidden_Setup, Hidden_Teardown, "T04_StaleOnce");
    UtTest_Add(Test_T04b_TwoMissing, Hidden_Setup, Hidden_Teardown, "T04b_TwoMissing");
    UtTest_Add(Test_T04c_Consecutive, Hidden_Setup, Hidden_Teardown, "T04c_Consecutive");
    UtTest_Add(Test_T04d_ThresholdOne, Hidden_Setup, Hidden_Teardown, "T04d_ThresholdOne");
    UtTest_Add(Test_T05_LazyThreshold, Hidden_Setup, Hidden_Teardown, "T05_LazyThreshold");
    UtTest_Add(Test_T06_FreshOnce, Hidden_Setup, Hidden_Teardown, "T06_FreshOnce");
    UtTest_Add(Test_T06b_StaleAgain, Hidden_Setup, Hidden_Teardown, "T06b_StaleAgain");
    UtTest_Add(Test_T07_ReportNone, Hidden_Setup, Hidden_Teardown, "T07_ReportNone");
    UtTest_Add(Test_T07b_ReportTwo, Hidden_Setup, Hidden_Teardown, "T07b_ReportTwo");
    UtTest_Add(Test_T07c_ReportCapped, Hidden_Setup, Hidden_Teardown, "T07c_ReportCapped");
    UtTest_Add(Test_T08_ResetCounters, Hidden_Setup, Hidden_Teardown, "T08_ResetCounters");
    UtTest_Add(Test_T08b_ResetFunction, Hidden_Setup, Hidden_Teardown, "T08b_ResetFunction");
    UtTest_Add(Test_T09_TableReload, Hidden_Setup, Hidden_Teardown, "T09_TableReload");
    UtTest_Add(Test_T10_BadLength, Hidden_Setup, Hidden_Teardown, "T10_BadLength");
}
