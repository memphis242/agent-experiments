#include "stor.h"
#include "stor_fcncodes.h"
#include "stor_dispatch.h"

static bool STOR_VerifyLen(const CFE_MSG_Message_t *MsgPtr, size_t Expected)
{
    size_t            Actual  = 0;
    CFE_SB_MsgId_t    MsgId   = CFE_SB_INVALID_MSG_ID;
    CFE_MSG_FcnCode_t FcnCode = 0;

    CFE_MSG_GetSize(MsgPtr, &Actual);
    if (Actual != Expected)
    {
        CFE_MSG_GetMsgId(MsgPtr, &MsgId);
        CFE_MSG_GetFcnCode(MsgPtr, &FcnCode);
        CFE_EVS_SendEvent(STOR_CMD_LEN_ERR_EID, CFE_EVS_EventType_ERROR,
                          "Invalid Msg length: ID = 0x%X, CC = %u, Len = %u, Expected = %u",
                          (unsigned int)CFE_SB_MsgIdToValue(MsgId), (unsigned int)FcnCode, (unsigned int)Actual,
                          (unsigned int)Expected);
        STOR_Data.CommandErrorCounter++;
        return false;
    }
    return true;
}

static void STOR_Noop(void)
{
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_NOOP_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: NOOP command");
}

static void STOR_Reset(void)
{
    uint32 i;
    STOR_Data.CommandCounter      = 0;
    STOR_Data.CommandErrorCounter = 0;
    STOR_Data.PacketsStored       = 0;
    STOR_Data.PacketsDecimated    = 0;
    STOR_Data.PacketsDropped      = 0;
    STOR_Data.FilesPurged         = 0;
    STOR_Data.PurgeCycles         = 0;
    STOR_Data.FileAddsRejected    = 0;
    STOR_Data.Xfer.XfersCompleted = 0;
    STOR_Data.Xfer.XfersFailed    = 0;
    STOR_Data.Xfer.SegsSent       = 0;
    STOR_Data.Xfer.SegsReceived   = 0;
    STOR_Data.Xfer.FilesReceived  = 0;
    STOR_Data.Xfer.RxErrors       = 0;
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        STOR_Data.Entries[i].Received  = 0;
        STOR_Data.Entries[i].Stored    = 0;
        STOR_Data.Entries[i].Decimated = 0;
        STOR_Data.Entries[i].Dropped   = 0;
    }
    CFE_EVS_SendEvent(STOR_RESET_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: RESET command");
}

static void STOR_SetParam(uint32 MsgIdValue, uint8 Value, bool IsPriority)
{
    STOR_Entry_t *E = STOR_FindEntry(MsgIdValue);
    if (E == NULL || Value == 0)
    {
        CFE_EVS_SendEvent(STOR_SET_PARAM_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: bad parameter, MID 0x%X value %u",
                          (unsigned int)MsgIdValue, (unsigned int)Value);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    if (IsPriority)
    {
        E->Priority = Value;
    }
    else
    {
        E->Decimation = Value;
    }
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_SET_PARAM_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: MID 0x%X %s set to %u",
                      (unsigned int)MsgIdValue, IsPriority ? "priority" : "decimation", (unsigned int)Value);
    if (IsPriority)
    {
        STOR_Purge();
    }
}

static void STOR_Ground(const CFE_SB_Buffer_t *Buf)
{
    CFE_MSG_FcnCode_t cc = 0;
    uint32            n;

    CFE_MSG_GetFcnCode(&Buf->Msg, &cc);
    switch (cc)
    {
        case STOR_NOOP_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_NoopCmd_t))) STOR_Noop();
            break;
        case STOR_RESET_COUNTERS_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_ResetCountersCmd_t))) STOR_Reset();
            break;
        case STOR_SET_PRIORITY_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SetPriorityCmd_t)))
            {
                const STOR_SetPriorityCmd_t *C = (const STOR_SetPriorityCmd_t *)Buf;
                STOR_SetParam(CFE_SB_MsgIdToValue(C->Payload.MsgId), C->Payload.Priority, true);
            }
            break;
        case STOR_SET_DECIMATION_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SetDecimationCmd_t)))
            {
                const STOR_SetDecimationCmd_t *C = (const STOR_SetDecimationCmd_t *)Buf;
                STOR_SetParam(CFE_SB_MsgIdToValue(C->Payload.MsgId), C->Payload.Decimation, false);
            }
            break;
        case STOR_SET_PURGE_THRESHOLD_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SetPurgeThresholdCmd_t)))
            {
                const STOR_SetPurgeThresholdCmd_t *C = (const STOR_SetPurgeThresholdCmd_t *)Buf;
                if (C->Payload.Percent < 1 || C->Payload.Percent > 100)
                {
                    CFE_EVS_SendEvent(STOR_SET_PARAM_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: bad threshold %u",
                                      (unsigned int)C->Payload.Percent);
                    STOR_Data.CommandErrorCounter++;
                }
                else
                {
                    STOR_Data.PurgeThresholdPercent = C->Payload.Percent;
                    STOR_Data.CommandCounter++;
                    CFE_EVS_SendEvent(STOR_SET_PARAM_INF_EID, CFE_EVS_EventType_INFORMATION,
                                      "STOR: purge threshold set to %u", (unsigned int)C->Payload.Percent);
                    STOR_Purge();
                }
            }
            break;
        case STOR_ADD_FILE_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_AddFileCmd_t))) STOR_AddFile((const STOR_AddFileCmd_t *)Buf);
            break;
        case STOR_DELETE_FILE_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_DeleteFileCmd_t)))
                STOR_DeleteFile((const STOR_DeleteFileCmd_t *)Buf);
            break;
        case STOR_LIST_DIR_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_ListDirCmd_t))) STOR_ListDir((const STOR_ListDirCmd_t *)Buf);
            break;
        case STOR_LIST_ALL_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_ListAllCmd_t))) STOR_ListAll();
            break;
        case STOR_SEND_STATS_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SendStatsCmd_t)))
            {
                STOR_Data.CommandCounter++;
                STOR_SendStats();
            }
            break;
        case STOR_CLOSE_FILES_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_CloseFilesCmd_t)))
            {
                n = STOR_CloseAll();
                STOR_Data.CommandCounter++;
                CFE_EVS_SendEvent(STOR_CLOSE_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: closed %u files",
                                  (unsigned int)n);
                STOR_Purge();
            }
            break;
        case STOR_TRANSFER_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_TransferCmd_t))) STOR_Transfer((const STOR_TransferCmd_t *)Buf);
            break;
        case STOR_ABORT_TRANSFER_CC:
            if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_AbortTransferCmd_t))) STOR_AbortTransfer();
            break;
        default:
            CFE_EVS_SendEvent(STOR_CC_ERR_EID, CFE_EVS_EventType_ERROR, "Invalid ground command code: CC = %d",
                              (int)cc);
            STOR_Data.CommandErrorCounter++;
            break;
    }
}

void STOR_TaskPipe(const CFE_SB_Buffer_t *Buf)
{
    CFE_SB_MsgId_t MsgId = CFE_SB_INVALID_MSG_ID;
    uint32         v;

    CFE_MSG_GetMsgId(&Buf->Msg, &MsgId);
    v = CFE_SB_MsgIdToValue(MsgId);

    if (v == STOR_CMD_MID)
    {
        STOR_Ground(Buf);
    }
    else if (v == STOR_SEND_HK_MID)
    {
        if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SendHkCmd_t))) STOR_SendHk();
    }
    else if (v == STOR_XFER_SEG_MID)
    {
        size_t sz = 0;
        CFE_MSG_GetSize(&Buf->Msg, &sz);
        if (sz == sizeof(STOR_XferSegCmd_t)) STOR_RxSegment((const STOR_XferSegCmd_t *)Buf);
    }
    else if (v == STOR_XFER_ACK_MID)
    {
        size_t sz = 0;
        CFE_MSG_GetSize(&Buf->Msg, &sz);
        if (sz == sizeof(STOR_XferAckCmd_t)) STOR_RxAck((const STOR_XferAckCmd_t *)Buf);
    }
    else if (v == STOR_WAKEUP_MID)
    {
        if (STOR_VerifyLen(&Buf->Msg, sizeof(STOR_SendHkCmd_t))) STOR_Wakeup();
    }
    else
    {
        STOR_Entry_t *E = STOR_FindEntry(v);
        if (E != NULL)
        {
            STOR_StorePacket(E, Buf);
        }
        else
        {
            STOR_Data.CommandErrorCounter++;
            CFE_EVS_SendEvent(STOR_MID_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: invalid packet, MID = 0x%x",
                              (unsigned int)v);
        }
    }
}
