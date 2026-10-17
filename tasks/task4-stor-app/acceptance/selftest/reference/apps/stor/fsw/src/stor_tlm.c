#include "stor.h"
#include "stor_fcncodes.h"

static size_t STOR_Strnlen(const char *s, size_t max)
{
    const char *p = memchr(s, 0, max);
    return p ? (size_t)(p - s) : max;
}

static uint32 STOR_Now(void)
{
    return CFE_TIME_GetTime().Seconds;
}

int32 STOR_TableValidate(void *TblData)
{
    const STOR_StorageTable_t *T = TblData;
    uint32                     i;
    uint32                     j;
    int32                      rc = CFE_SUCCESS;

    if (STOR_Strnlen(T->TelemetryRoot, STOR_PATH_LEN) == 0 || STOR_Strnlen(T->TelemetryRoot, STOR_PATH_LEN) == STOR_PATH_LEN ||
        STOR_Strnlen(T->FileRoot, STOR_PATH_LEN) == 0 || STOR_Strnlen(T->FileRoot, STOR_PATH_LEN) == STOR_PATH_LEN)
    {
        rc = -1;
    }
    else if (T->TelemetryQuotaBytes == 0 || T->FileQuotaBytes == 0)
    {
        rc = -2;
    }
    else if (T->MaxPacketFileBytes < 1024 || T->MaxPacketFileBytes > T->TelemetryQuotaBytes)
    {
        rc = -3;
    }
    else if (T->PurgeThresholdPercent < 1 || T->PurgeThresholdPercent > 100)
    {
        rc = -4;
    }
    else
    {
        for (i = 0; i < STOR_MAX_ENTRIES && rc == CFE_SUCCESS; i++)
        {
            if (!CFE_SB_IsValidMsgId(T->Entries[i].MsgId))
            {
                continue;
            }
            if (T->Entries[i].Priority == 0 || T->Entries[i].Decimation == 0)
            {
                rc = -5;
            }
            for (j = i + 1; j < STOR_MAX_ENTRIES && rc == CFE_SUCCESS; j++)
            {
                if (CFE_SB_MsgId_Equal(T->Entries[i].MsgId, T->Entries[j].MsgId))
                {
                    rc = -6;
                }
            }
        }
    }

    if (rc != CFE_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_TBL_VAL_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: table validation failed, code %d",
                          (int)rc);
    }
    return rc;
}

STOR_Entry_t *STOR_FindEntry(uint32 MsgIdValue)
{
    uint32 i;
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        if (STOR_Data.Entries[i].Used && STOR_Data.Entries[i].MsgIdValue == MsgIdValue)
        {
            return &STOR_Data.Entries[i];
        }
    }
    return NULL;
}

static void STOR_CloseEntry(STOR_Entry_t *E)
{
    if (E->Open)
    {
        OS_close(E->Fd);
        if (STOR_Data.NumClosed < STOR_MAX_FILES)
        {
            STOR_ClosedFile_t *C = &STOR_Data.Closed[STOR_Data.NumClosed++];
            C->MsgIdValue        = E->MsgIdValue;
            C->Seq               = E->OpenSeq;
            C->Size              = E->OpenBytes;
        }
        E->Open = false;
    }
}

uint32 STOR_CloseAll(void)
{
    uint32 i;
    uint32 n = 0;
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        if (STOR_Data.Entries[i].Used && STOR_Data.Entries[i].Open)
        {
            STOR_CloseEntry(&STOR_Data.Entries[i]);
            n++;
        }
    }
    return n;
}

void STOR_PollTable(void)
{
    void                *Addr = NULL;
    CFE_Status_t         status;
    uint32               i;
    const STOR_StorageTable_t *T;
    STOR_Entry_t         Old[STOR_MAX_ENTRIES];

    CFE_TBL_Manage(STOR_Data.TblHandle);
    status = CFE_TBL_GetAddress(&Addr, STOR_Data.TblHandle);
    if (status != CFE_TBL_INFO_UPDATED)
    {
        if (status == CFE_SUCCESS || status > 0)
        {
            CFE_TBL_ReleaseAddress(STOR_Data.TblHandle);
        }
        return;
    }

    T = Addr;
    memcpy(&STOR_Data.Tbl, T, sizeof(STOR_Data.Tbl));
    CFE_TBL_ReleaseAddress(STOR_Data.TblHandle);

    memcpy(Old, STOR_Data.Entries, sizeof(Old));
    memset(STOR_Data.Entries, 0, sizeof(STOR_Data.Entries));

    /* unsubscribe entries no longer present */
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        uint32 j;
        bool   keep = false;
        if (!Old[i].Used)
        {
            continue;
        }
        for (j = 0; j < STOR_MAX_ENTRIES; j++)
        {
            if (CFE_SB_IsValidMsgId(STOR_Data.Tbl.Entries[j].MsgId) &&
                CFE_SB_MsgIdToValue(STOR_Data.Tbl.Entries[j].MsgId) == Old[i].MsgIdValue)
            {
                keep = true;
            }
        }
        if (!keep)
        {
            CFE_SB_Unsubscribe(CFE_SB_ValueToMsgId(Old[i].MsgIdValue), STOR_Data.CommandPipe);
            if (Old[i].Open)
            {
                STOR_Data.Entries[i] = Old[i];
                STOR_CloseEntry(&STOR_Data.Entries[i]);
                memset(&STOR_Data.Entries[i], 0, sizeof(STOR_Data.Entries[i]));
            }
        }
    }

    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        const STOR_EntryDef_t *D = &STOR_Data.Tbl.Entries[i];
        STOR_Entry_t          *E = &STOR_Data.Entries[i];
        uint32                 j;
        if (!CFE_SB_IsValidMsgId(D->MsgId))
        {
            continue;
        }
        for (j = 0; j < STOR_MAX_ENTRIES; j++)
        {
            if (Old[j].Used && Old[j].MsgIdValue == CFE_SB_MsgIdToValue(D->MsgId))
            {
                *E = Old[j];
                break;
            }
        }
        if (j == STOR_MAX_ENTRIES)
        {
            E->MsgIdValue = CFE_SB_MsgIdToValue(D->MsgId);
            if (CFE_SB_Subscribe(D->MsgId, STOR_Data.CommandPipe) != CFE_SUCCESS)
            {
                CFE_EVS_SendEvent(STOR_SUB_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: subscribe failed for 0x%X",
                                  (unsigned int)E->MsgIdValue);
                continue;
            }
        }
        E->Used       = true;
        E->Priority   = D->Priority;
        E->Decimation = D->Decimation;
    }
    STOR_Data.PurgeThresholdPercent = STOR_Data.Tbl.PurgeThresholdPercent;
}

static uint32 STOR_PriorityOf(uint32 MsgIdValue)
{
    const STOR_Entry_t *E = STOR_FindEntry(MsgIdValue);
    return (E == NULL) ? 0 : E->Priority;
}

void STOR_Purge(void)
{
    uint64 Limit = ((uint64)STOR_Data.Tbl.TelemetryQuotaBytes * STOR_Data.PurgeThresholdPercent) / 100;
    uint32 count = 0;

    while (STOR_Data.TelemetryBytesUsed > Limit && STOR_Data.NumClosed > 0)
    {
        uint32 i;
        uint32 best = 0;
        char   Path[OS_MAX_PATH_LEN];
        for (i = 1; i < STOR_Data.NumClosed; i++)
        {
            uint32 pa = STOR_PriorityOf(STOR_Data.Closed[i].MsgIdValue);
            uint32 pb = STOR_PriorityOf(STOR_Data.Closed[best].MsgIdValue);
            if (pa < pb || (pa == pb && STOR_Data.Closed[i].Seq < STOR_Data.Closed[best].Seq))
            {
                best = i;
            }
        }
        snprintf(Path, sizeof(Path), "%s/tlm_%04X_%08u.bin", STOR_Data.Tbl.TelemetryRoot,
                 (unsigned int)STOR_Data.Closed[best].MsgIdValue, (unsigned int)STOR_Data.Closed[best].Seq);
        OS_remove(Path);
        STOR_Data.TelemetryBytesUsed -= STOR_Data.Closed[best].Size;
        STOR_Data.Closed[best] = STOR_Data.Closed[--STOR_Data.NumClosed];
        STOR_Data.FilesPurged++;
        count++;
    }
    if (count > 0)
    {
        STOR_Data.PurgeCycles++;
        CFE_EVS_SendEvent(STOR_PURGE_INF_EID, CFE_EVS_EventType_DEBUG, "STOR: purged %u files", (unsigned int)count);
    }
}

void STOR_StorePacket(STOR_Entry_t *E, const CFE_SB_Buffer_t *SBBufPtr)
{
    CFE_MSG_Size_t Size = 0;
    uint32         cost;
    bool           newfile;

    E->Received++;
    if (((E->Received - 1) % E->Decimation) != 0)
    {
        E->Decimated++;
        STOR_Data.PacketsDecimated++;
        return;
    }

    CFE_MSG_GetSize(&SBBufPtr->Msg, &Size);

    newfile = !E->Open;
    if (E->Open && E->OpenBytes + Size > STOR_Data.Tbl.MaxPacketFileBytes)
    {
        STOR_CloseEntry(E);
        newfile = true;
    }
    cost = Size + (newfile ? sizeof(CFE_FS_Header_t) : 0);

    if ((uint64)STOR_Data.TelemetryBytesUsed + cost > STOR_Data.Tbl.TelemetryQuotaBytes)
    {
        E->Dropped++;
        STOR_Data.PacketsDropped++;
        if (!E->DropReported)
        {
            E->DropReported = true;
            CFE_EVS_SendEvent(STOR_TLM_QUOTA_ERR_EID, CFE_EVS_EventType_ERROR,
                              "STOR: telemetry quota reached, dropping MID 0x%X", (unsigned int)E->MsgIdValue);
        }
        return;
    }

    if (newfile)
    {
        char            Path[OS_MAX_PATH_LEN];
        CFE_FS_Header_t Hdr;
        if (!E->SeqInit)
        {
            uint32 k;
            E->SeqInit = true;
            E->NextSeq = 0;
            for (k = 0; k < STOR_Data.NumClosed; k++)
            {
                if (STOR_Data.Closed[k].MsgIdValue == E->MsgIdValue && STOR_Data.Closed[k].Seq >= E->NextSeq)
                {
                    E->NextSeq = STOR_Data.Closed[k].Seq + 1;
                }
            }
        }
        snprintf(Path, sizeof(Path), "%s/tlm_%04X_%08u.bin", STOR_Data.Tbl.TelemetryRoot, (unsigned int)E->MsgIdValue,
                 (unsigned int)E->NextSeq);
        if (OS_OpenCreate(&E->Fd, Path, OS_FILE_FLAG_CREATE | OS_FILE_FLAG_TRUNCATE, OS_READ_WRITE) != OS_SUCCESS)
        {
            CFE_EVS_SendEvent(STOR_FILE_WRITE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot create %s", Path);
            return;
        }
        CFE_FS_InitHeader(&Hdr, "STOR packet file", STOR_FILE_SUBTYPE);
        if (CFE_FS_WriteHeader(E->Fd, &Hdr) != (int32)sizeof(Hdr))
        {
            CFE_EVS_SendEvent(STOR_FILE_WRITE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: header write failed %s", Path);
            OS_close(E->Fd);
            OS_remove(Path);
            return;
        }
        E->Open      = true;
        E->OpenSeq   = E->NextSeq++;
        E->OpenBytes = sizeof(Hdr);
        STOR_Data.TelemetryBytesUsed += sizeof(Hdr);
    }

    if (OS_write(E->Fd, SBBufPtr, Size) != (int32)Size)
    {
        CFE_EVS_SendEvent(STOR_FILE_WRITE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: packet write failed");
        return;
    }
    E->OpenBytes += Size;
    STOR_Data.TelemetryBytesUsed += Size;
    E->Stored++;
    E->DropReported = false;
    E->LastWrite    = STOR_Now();
    STOR_Data.PacketsStored++;
    STOR_Purge();
}

void STOR_Wakeup(void)
{
    uint32 i;
    uint32 now = STOR_Now();
    STOR_PollTable();
    STOR_Purge();
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        STOR_Entry_t *E = &STOR_Data.Entries[i];
        if (E->Used && E->Open && now - E->LastWrite >= STOR_IDLE_CLOSE_SEC)
        {
            STOR_CloseEntry(E);
        }
    }
}

void STOR_ScanTelemetry(void)
{
    osal_id_t     Dir;
    os_dirent_t   Ent;
    char          Path[OS_MAX_PATH_LEN];
    os_fstat_t    St;
    unsigned int  mid;
    unsigned int  seq;
    uint32        maxseq = 0;
    bool          any    = false;

    if (OS_DirectoryOpen(&Dir, STOR_Data.Tbl.TelemetryRoot) != OS_SUCCESS)
    {
        return;
    }
    while (OS_DirectoryRead(Dir, &Ent) == OS_SUCCESS)
    {
        if (sscanf(OS_DIRENTRY_NAME(Ent), "tlm_%4X_%8u.bin", &mid, &seq) != 2)
        {
            continue;
        }
        snprintf(Path, sizeof(Path), "%s/%s", STOR_Data.Tbl.TelemetryRoot, OS_DIRENTRY_NAME(Ent));
        if (OS_stat(Path, &St) != OS_SUCCESS || OS_FILESTAT_ISDIR(St))
        {
            continue;
        }
        STOR_Data.TelemetryBytesUsed += (uint32)OS_FILESTAT_SIZE(St);
        if (STOR_Data.NumClosed < STOR_MAX_FILES)
        {
            STOR_Data.Closed[STOR_Data.NumClosed].MsgIdValue = mid;
            STOR_Data.Closed[STOR_Data.NumClosed].Seq        = seq;
            STOR_Data.Closed[STOR_Data.NumClosed].Size       = (uint32)OS_FILESTAT_SIZE(St);
            STOR_Data.NumClosed++;
        }
        if (!any || seq > maxseq)
        {
            maxseq = seq;
            any    = true;
        }
    }
    OS_DirectoryClose(Dir);
    STOR_Data.NextSeq = any ? maxseq + 1 : 0;
}

void STOR_SendHk(void)
{
    STOR_HkTlm_Payload_t *P = &STOR_Data.HkTlm.Payload;
    uint32                i;

    STOR_PollTable();
    P->CommandCounter        = STOR_Data.CommandCounter;
    P->CommandErrorCounter   = STOR_Data.CommandErrorCounter;
    P->PurgeThresholdPercent = STOR_Data.PurgeThresholdPercent;
    P->TelemetryBytesUsed    = STOR_Data.TelemetryBytesUsed;
    P->TelemetryQuotaBytes   = STOR_Data.Tbl.TelemetryQuotaBytes;
    P->FileBytesUsed         = STOR_Data.FileBytesUsed;
    P->FileQuotaBytes        = STOR_Data.Tbl.FileQuotaBytes;
    P->PacketsStored         = STOR_Data.PacketsStored;
    P->PacketsDecimated      = STOR_Data.PacketsDecimated;
    P->PacketsDropped        = STOR_Data.PacketsDropped;
    P->FilesPurged           = STOR_Data.FilesPurged;
    P->PurgeCycles           = STOR_Data.PurgeCycles;
    P->FileAddsRejected      = STOR_Data.FileAddsRejected;
    P->FileCount             = STOR_Data.FileCount;
    P->ActiveEntries         = 0;
    P->OpenPacketFiles       = 0;
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        P->ActiveEntries += STOR_Data.Entries[i].Used ? 1 : 0;
        P->OpenPacketFiles += (STOR_Data.Entries[i].Used && STOR_Data.Entries[i].Open) ? 1 : 0;
    }
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(STOR_Data.HkTlm.TelemetryHeader));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.HkTlm.TelemetryHeader), true);
}

void STOR_SendStats(void)
{
    uint32 i;
    uint32 n = 0;
    memset(&STOR_Data.StatsTlm.Payload, 0, sizeof(STOR_Data.StatsTlm.Payload));
    for (i = 0; i < STOR_MAX_ENTRIES; i++)
    {
        const STOR_Entry_t *E = &STOR_Data.Entries[i];
        STOR_EntryStats_t  *S = &STOR_Data.StatsTlm.Payload.Entries[n];
        if (!E->Used)
        {
            continue;
        }
        S->MsgId      = CFE_SB_ValueToMsgId(E->MsgIdValue);
        S->Priority   = E->Priority;
        S->Decimation = E->Decimation;
        S->Received   = E->Received;
        S->Stored     = E->Stored;
        S->Decimated  = E->Decimated;
        S->Dropped    = E->Dropped;
        n++;
    }
    for (; n < STOR_MAX_ENTRIES; n++)
    {
        STOR_Data.StatsTlm.Payload.Entries[n].MsgId = CFE_SB_INVALID_MSG_ID;
    }
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(STOR_Data.StatsTlm.TelemetryHeader));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.StatsTlm.TelemetryHeader), true);
    CFE_EVS_SendEvent(STOR_STATS_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: statistics sent");
}
