#include "stor.h"

#define STOR_ABS_MAX 160

static bool STOR_PathOk(const char *Rel, bool AllowEmpty)
{
    if (memchr(Rel, '\0', STOR_PATH_LEN) == NULL)
    {
        return false;
    }
    if (Rel[0] == '/' || strstr(Rel, "..") != NULL)
    {
        return false;
    }
    return AllowEmpty || Rel[0] != '\0';
}

static void STOR_PathErr(const char *What)
{
    CFE_EVS_SendEvent(STOR_PATH_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: %s path rejected", What);
    STOR_Data.CommandErrorCounter++;
}

static void STOR_Abs(char *Out, size_t Len, const char *Rel)
{
    if (Rel[0] == '\0')
        snprintf(Out, Len, "%s", STOR_Data.Tbl.FileRoot);
    else
        snprintf(Out, Len, "%s/%s", STOR_Data.Tbl.FileRoot, Rel);
}

bool STOR_MakeDirs(const char *Path, bool IncludeLast)
{
    char       Work[STOR_ABS_MAX];
    size_t     i;
    size_t     len;
    os_fstat_t St;

    snprintf(Work, sizeof(Work), "%s", Path);
    len = strlen(Work);
    for (i = 1; i <= len; i++)
    {
        if (Work[i] == '/' || (Work[i] == '\0' && IncludeLast))
        {
            char saved = Work[i];
            Work[i]    = '\0';
            if (OS_stat(Work, &St) != OS_SUCCESS)
            {
                if (OS_mkdir(Work, 0) != OS_SUCCESS)
                {
                    CFE_EVS_SendEvent(STOR_DIR_CREATE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot create %s", Work);
                    return false;
                }
            }
            Work[i] = saved;
        }
    }
    return true;
}

static void STOR_Walk(const char *Rel, bool Recurse, uint32 *Total, STOR_ListEntry_t *Out, uint32 Max, bool *Err)
{
    osal_id_t   Dir;
    os_dirent_t Ent;
    char        Abs[STOR_ABS_MAX];

    STOR_Abs(Abs, sizeof(Abs), Rel);
    if (OS_DirectoryOpen(&Dir, Abs) != OS_SUCCESS)
    {
        *Err = true;
        return;
    }
    while (OS_DirectoryRead(Dir, &Ent) == OS_SUCCESS)
    {
        const char *Name = OS_DIRENTRY_NAME(Ent);
        char        ChildRel[STOR_ABS_MAX];
        char        ChildAbs[STOR_ABS_MAX];
        os_fstat_t  St;
        bool        isdir;

        if (strcmp(Name, ".") == 0 || strcmp(Name, "..") == 0)
        {
            continue;
        }
        if (Rel[0] == '\0')
            snprintf(ChildRel, sizeof(ChildRel), "%s", Name);
        else
            snprintf(ChildRel, sizeof(ChildRel), "%s/%s", Rel, Name);
        STOR_Abs(ChildAbs, sizeof(ChildAbs), ChildRel);
        if (OS_stat(ChildAbs, &St) != OS_SUCCESS)
        {
            continue;
        }
        isdir = OS_FILESTAT_ISDIR(St);
        if (isdir && Recurse)
        {
            STOR_Walk(ChildRel, true, Total, Out, Max, Err);
            continue;
        }
        if (*Total < Max)
        {
            STOR_ListEntry_t *L = &Out[*Total];
            memset(L, 0, sizeof(*L));
            snprintf(L->Name, sizeof(L->Name), "%s%s", Recurse ? ChildRel : Name, isdir ? "/" : "");
            L->Size = isdir ? 0 : (uint32)OS_FILESTAT_SIZE(St);
        }
        (*Total)++;
    }
    OS_DirectoryClose(Dir);
}

static STOR_ListEntry_t STOR_ListBuf[STOR_MAX_LIST];

static void STOR_Emit(uint32 Total)
{
    uint32 sent = 0;
    uint16 idx  = 0;
    uint32 n    = (Total > STOR_MAX_LIST) ? STOR_MAX_LIST : Total;

    do
    {
        uint32 k = n - sent;
        if (k > STOR_LIST_ENTRIES_PER_PKT) k = STOR_LIST_ENTRIES_PER_PKT;
        memset(&STOR_Data.ListTlm.Payload, 0, sizeof(STOR_Data.ListTlm.Payload));
        STOR_Data.ListTlm.Payload.TotalEntries    = (uint16)Total;
        STOR_Data.ListTlm.Payload.PacketIndex     = idx++;
        STOR_Data.ListTlm.Payload.EntriesInPacket = (uint16)k;
        memcpy(STOR_Data.ListTlm.Payload.Entries, &STOR_ListBuf[sent], k * sizeof(STOR_ListEntry_t));
        CFE_SB_TimeStampMsg(CFE_MSG_PTR(STOR_Data.ListTlm.TelemetryHeader));
        CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.ListTlm.TelemetryHeader), true);
        sent += k;
    } while (sent < n);
}

static void STOR_List(const char *Rel, bool Recurse)
{
    uint32 Total = 0;
    bool   Err   = false;

    STOR_Walk(Rel, Recurse, &Total, STOR_ListBuf, STOR_MAX_LIST, &Err);
    if (Err)
    {
        CFE_EVS_SendEvent(STOR_LIST_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot list %s", Rel);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    STOR_Emit(Total);
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_LIST_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: listed %u entries", (unsigned int)Total);
}

void STOR_ListDir(const STOR_ListDirCmd_t *Cmd)
{
    if (!STOR_PathOk(Cmd->Payload.Path, true))
    {
        STOR_PathErr("list");
        return;
    }
    STOR_List(Cmd->Payload.Path, false);
}

void STOR_ListAll(void)
{
    STOR_List("", true);
}

static void STOR_ScanDir(const char *Rel)
{
    osal_id_t   Dir;
    os_dirent_t Ent;
    char        Abs[STOR_ABS_MAX];

    STOR_Abs(Abs, sizeof(Abs), Rel);
    if (OS_DirectoryOpen(&Dir, Abs) != OS_SUCCESS)
    {
        return;
    }
    while (OS_DirectoryRead(Dir, &Ent) == OS_SUCCESS)
    {
        const char *Name = OS_DIRENTRY_NAME(Ent);
        char        ChildRel[STOR_ABS_MAX];
        char        ChildAbs[STOR_ABS_MAX];
        os_fstat_t  St;
        if (strcmp(Name, ".") == 0 || strcmp(Name, "..") == 0) continue;
        if (Rel[0] == '\0')
            snprintf(ChildRel, sizeof(ChildRel), "%s", Name);
        else
            snprintf(ChildRel, sizeof(ChildRel), "%s/%s", Rel, Name);
        STOR_Abs(ChildAbs, sizeof(ChildAbs), ChildRel);
        if (OS_stat(ChildAbs, &St) != OS_SUCCESS) continue;
        if (OS_FILESTAT_ISDIR(St))
        {
            STOR_ScanDir(ChildRel);
        }
        else if (strlen(Name) > 5 && strcmp(Name + strlen(Name) - 5, ".part") == 0)
        {
            OS_remove(ChildAbs);
        }
        else
        {
            STOR_Data.FileBytesUsed += (uint32)OS_FILESTAT_SIZE(St);
            STOR_Data.FileCount++;
        }
    }
    OS_DirectoryClose(Dir);
}

void STOR_ScanFiles(void)
{
    STOR_Data.FileBytesUsed = 0;
    STOR_Data.FileCount     = 0;
    STOR_ScanDir("");
}

void STOR_AddFile(const STOR_AddFileCmd_t *Cmd)
{
    char        Dst[STOR_ABS_MAX];
    char        Src[STOR_PATH_LEN];
    osal_id_t   In;
    osal_id_t   Out;
    os_fstat_t  St;
    uint32      size;
    uint8       buf[512];
    int32       got;
    bool        ok = true;

    if (!STOR_PathOk(Cmd->Payload.DestPath, false))
    {
        STOR_PathErr("destination");
        return;
    }
    if (memchr(Cmd->Payload.SourcePath, '\0', STOR_PATH_LEN) == NULL)
    {
        STOR_PathErr("source");
        return;
    }
    snprintf(Src, sizeof(Src), "%s", Cmd->Payload.SourcePath);
    if (OS_stat(Src, &St) != OS_SUCCESS || OS_FILESTAT_ISDIR(St) ||
        OS_OpenCreate(&In, Src, OS_FILE_FLAG_NONE, OS_READ_ONLY) != OS_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_FILE_SRC_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot open source %s", Src);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    size = (uint32)OS_FILESTAT_SIZE(St);
    STOR_Abs(Dst, sizeof(Dst), Cmd->Payload.DestPath);

    if (OS_stat(Dst, &St) == OS_SUCCESS)
    {
        OS_close(In);
        CFE_EVS_SendEvent(STOR_FILE_EXISTS_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: destination exists %s", Dst);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    if ((uint64)STOR_Data.FileBytesUsed + size > STOR_Data.Tbl.FileQuotaBytes)
    {
        OS_close(In);
        CFE_EVS_SendEvent(STOR_FILE_QUOTA_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: file quota exceeded");
        STOR_Data.FileAddsRejected++;
        STOR_Data.CommandErrorCounter++;
        return;
    }
    if (!STOR_MakeDirs(Dst, false) ||
        OS_OpenCreate(&Out, Dst, OS_FILE_FLAG_CREATE | OS_FILE_FLAG_TRUNCATE, OS_READ_WRITE) != OS_SUCCESS)
    {
        OS_close(In);
        CFE_EVS_SendEvent(STOR_FILE_WRITE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot create %s", Dst);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    while ((got = OS_read(In, buf, sizeof(buf))) > 0)
    {
        if (OS_write(Out, buf, got) != got)
        {
            ok = false;
            break;
        }
    }
    if (got < 0) ok = false;
    OS_close(In);
    OS_close(Out);
    if (!ok)
    {
        OS_remove(Dst);
        CFE_EVS_SendEvent(STOR_FILE_WRITE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: copy failed %s", Dst);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    STOR_Data.FileBytesUsed += size;
    STOR_Data.FileCount++;
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_FILE_ADD_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: added %s (%u bytes)", Dst,
                      (unsigned int)size);
}

void STOR_DeleteFile(const STOR_DeleteFileCmd_t *Cmd)
{
    char       Abs[STOR_ABS_MAX];
    os_fstat_t St;

    if (!STOR_PathOk(Cmd->Payload.Path, false))
    {
        STOR_PathErr("delete");
        return;
    }
    STOR_Abs(Abs, sizeof(Abs), Cmd->Payload.Path);
    if (OS_stat(Abs, &St) != OS_SUCCESS || OS_FILESTAT_ISDIR(St) || OS_remove(Abs) != OS_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_FILE_DEL_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: cannot delete %s", Abs);
        STOR_Data.CommandErrorCounter++;
        return;
    }
    STOR_Data.FileBytesUsed -= (uint32)OS_FILESTAT_SIZE(St);
    STOR_Data.FileCount--;
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_FILE_DEL_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: deleted %s", Abs);
}

/* ======================================================================== transfer (reference, S2) */

#define STOR_ST_OK 0
#define STOR_ST_EXISTS 1
#define STOR_ST_QUOTA 2
#define STOR_ST_SEQUENCE 3
#define STOR_ST_CRC 4
#define STOR_ST_IO 5
#define STOR_ST_PATH 6
#define STOR_ST_TIMEOUT 7
#define STOR_ST_ABORTED 8
#define STOR_ST_ACTION_FAILED 9

uint32 STOR_Crc32(uint32 Crc, const uint8 *Data, size_t Len)
{
    size_t i;
    int    b;
    Crc = ~Crc;
    for (i = 0; i < Len; i++)
    {
        Crc ^= Data[i];
        for (b = 0; b < 8; b++) Crc = (Crc & 1u) ? (Crc >> 1) ^ 0xEDB88320u : (Crc >> 1);
    }
    return ~Crc;
}

bool STOR_PacketName(const char *Name, uint32 *Mid, uint32 *Seq, bool *Arc)
{
    unsigned int m = 0, s = 0;
    int          i;
    if (strlen(Name) != 21 || strncmp(Name, "tlm_", 4) != 0 || Name[8] != '_' || Name[17] != '.') return false;
    for (i = 4; i < 8; i++)
        if (!((Name[i] >= '0' && Name[i] <= '9') || (Name[i] >= 'A' && Name[i] <= 'F'))) return false;
    for (i = 9; i < 17; i++)
        if (Name[i] < '0' || Name[i] > '9') return false;
    if (strcmp(Name + 18, "bin") != 0 && strcmp(Name + 18, "arc") != 0) return false;
    sscanf(Name + 4, "%4X", &m);
    sscanf(Name + 9, "%8u", &s);
    *Mid = m;
    *Seq = s;
    *Arc = strcmp(Name + 18, "arc") == 0;
    return true;
}

/* Remove (Archive false) or mark archived (Archive true) a closed packet file record. */
void STOR_ForgetClosed(uint32 Mid, uint32 Seq, bool Arc, bool Archive)
{
    uint32 i;
    for (i = 0; i < STOR_Data.NumClosed; i++)
    {
        STOR_ClosedFile_t *C = &STOR_Data.Closed[i];
        if (C->MsgIdValue != Mid || C->Seq != Seq || C->Archived != Arc) continue;
        if (Archive)
        {
            C->Archived = true;
        }
        else
        {
            STOR_Data.TelemetryBytesUsed -= C->Size;
            *C = STOR_Data.Closed[--STOR_Data.NumClosed];
        }
        return;
    }
}

static uint8 STOR_Node(void)
{
    return (uint8)CFE_PSP_GetProcessorId();
}

static void STOR_XferErr(const char *Why)
{
    CFE_EVS_SendEvent(STOR_XFER_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: transfer rejected: %s", Why);
    STOR_Data.CommandErrorCounter++;
}

void STOR_Transfer(const STOR_TransferCmd_t *Cmd)
{
    STOR_Xfer_t                   *X = &STOR_Data.Xfer;
    const STOR_Transfer_Payload_t *P = &Cmd->Payload;
    os_fstat_t                     St;
    char                           Abs[192];
    osal_id_t                      Fd;
    uint8                          buf[512];
    int32                          got;
    uint32                         crc = 0;

    if (X->State != 0) { STOR_XferErr("a transfer is active"); return; }
    if (P->Area > 1 || P->SuccessAction > 2) { STOR_XferErr("Area or SuccessAction out of range"); return; }
    if (P->Destination == STOR_Node()) { STOR_XferErr("Destination is this node"); return; }
    if (!STOR_PathOk(P->Path, false)) { STOR_XferErr("Path"); return; }
    if (P->Area == 1)
    {
        uint32 m, s, i;
        bool   arc;
        if (!STOR_PacketName(P->Path, &m, &s, &arc)) { STOR_XferErr("not a packet file name"); return; }
        snprintf(Abs, sizeof(Abs), "%s/%s", STOR_Data.Tbl.TelemetryRoot, P->Path);
        for (i = 0; i < STOR_MAX_ENTRIES; i++)
        {
            const STOR_Entry_t *E = &STOR_Data.Entries[i];
            if (!arc && E->Used && E->Open && E->MsgIdValue == m && E->OpenSeq == s) { STOR_XferErr("open packet file"); return; }
        }
    }
    else
    {
        STOR_Abs(Abs, sizeof(Abs), P->Path);
    }
    if (OS_stat(Abs, &St) != OS_SUCCESS || OS_FILESTAT_ISDIR(St)) { STOR_XferErr("source missing or a directory"); return; }
    if (P->Destination != 0 && (memchr(P->DestPath, 0, STOR_PATH_LEN) == NULL || !STOR_PathOk(P->DestPath, false)))
    {
        STOR_XferErr("DestPath");
        return;
    }
    if (OS_OpenCreate(&Fd, Abs, OS_FILE_FLAG_NONE, OS_READ_ONLY) != OS_SUCCESS) { STOR_XferErr("cannot open source"); return; }
    while ((got = OS_read(Fd, buf, sizeof(buf))) > 0) crc = STOR_Crc32(crc, buf, (size_t)got);
    OS_close(Fd);
    if (got < 0) { STOR_XferErr("cannot read source"); return; }

    memset(X->Path, 0, sizeof(X->Path));
    memset(X->DestPath, 0, sizeof(X->DestPath));
    snprintf(X->Path, sizeof(X->Path), "%s", P->Path);
    if (P->Destination != 0) snprintf(X->DestPath, sizeof(X->DestPath), "%s", P->DestPath);
    else snprintf(X->DestPath, sizeof(X->DestPath), "%s", P->Path);
    snprintf(X->Abs, sizeof(X->Abs), "%s", Abs);
    X->Area     = P->Area;
    X->Dest     = P->Destination;
    X->Action   = P->SuccessAction;
    X->FileSize = (uint32)OS_FILESTAT_SIZE(St);
    X->FileCrc  = crc;
    X->SegCount = (uint16)(X->FileSize == 0 ? 1 : (X->FileSize + STOR_XFER_DATA_LEN - 1) / STOR_XFER_DATA_LEN);
    X->NextSeg  = 0;
    X->AckWait  = 0;
    X->ActiveId = ++X->NextId;
    X->State    = 1;
    STOR_Data.CommandCounter++;
    CFE_EVS_SendEvent(STOR_XFER_START_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: transfer %u of %s to node %u started",
                      (unsigned int)X->ActiveId, X->Path, (unsigned int)X->Dest);
}

static void STOR_XferFail(uint8 Status, bool Abort)
{
    STOR_Xfer_t *X = &STOR_Data.Xfer;
    X->XfersFailed++;
    X->LastStatus = Status;
    X->State      = 0;
    if (Abort)
        CFE_EVS_SendEvent(STOR_XFER_ABORT_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: transfer %u aborted", (unsigned int)X->ActiveId);
    else
        CFE_EVS_SendEvent(STOR_XFER_FAIL_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: transfer %u failed, status %u",
                          (unsigned int)X->ActiveId, (unsigned int)Status);
}

static bool STOR_XferAction(void)
{
    STOR_Xfer_t *X = &STOR_Data.Xfer;
    char         To[200];
    os_fstat_t   St;
    if (X->Action == 0) return true;
    if (X->Area == 1)
    {
        uint32 m, s;
        bool   arc;
        STOR_PacketName(X->Path, &m, &s, &arc);
        if (X->Action == 1)
        {
            if (OS_remove(X->Abs) != OS_SUCCESS) return false;
            STOR_ForgetClosed(m, s, arc, false);
            return true;
        }
        if (arc) return true;
        snprintf(To, sizeof(To), "%s/tlm_%04X_%08u.arc", STOR_Data.Tbl.TelemetryRoot, (unsigned int)m, (unsigned int)s);
        if (OS_mv(X->Abs, To) != OS_SUCCESS) return false;
        STOR_ForgetClosed(m, s, false, true);
        return true;
    }
    if (X->Action == 1)
    {
        if (OS_stat(X->Abs, &St) != OS_SUCCESS || OS_remove(X->Abs) != OS_SUCCESS) return false;
        STOR_Data.FileBytesUsed -= (uint32)OS_FILESTAT_SIZE(St);
        STOR_Data.FileCount--;
        return true;
    }
    snprintf(To, sizeof(To), "%s.arc", X->Abs);
    if (OS_stat(To, &St) == OS_SUCCESS) return false;
    return OS_mv(X->Abs, To) == OS_SUCCESS;
}

static void STOR_XferComplete(void)
{
    STOR_Xfer_t *X = &STOR_Data.Xfer;
    bool         ok = STOR_XferAction();
    X->XfersCompleted++;
    X->State = 0;
    if (ok)
    {
        X->LastStatus = STOR_ST_OK;
        CFE_EVS_SendEvent(STOR_XFER_DONE_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: transfer %u done", (unsigned int)X->ActiveId);
    }
    else
    {
        X->LastStatus = STOR_ST_ACTION_FAILED;
        CFE_EVS_SendEvent(STOR_XFER_ACTION_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: transfer %u success action failed",
                          (unsigned int)X->ActiveId);
    }
}

void STOR_AbortTransfer(void)
{
    if (STOR_Data.Xfer.State == 0) { STOR_XferErr("no transfer active"); return; }
    STOR_Data.CommandCounter++;
    STOR_XferFail(STOR_ST_ABORTED, true);
}

static void STOR_SendAck(uint32 Tid, uint8 Dst, uint8 Status, uint32 Bytes)
{
    STOR_XferAck_Payload_t *A = &STOR_Data.XferAck.Payload;
    A->TransferId    = Tid;
    A->SrcNode       = STOR_Node();
    A->DstNode       = Dst;
    A->Status        = Status;
    A->Spare         = 0;
    A->BytesReceived = Bytes;
    CFE_MSG_SetFcnCode(CFE_MSG_PTR(STOR_Data.XferAck.CommandHeader), 0);
    CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.XferAck.CommandHeader), true);
}

static void STOR_RxEnd(uint8 Status, bool Ack)
{
    STOR_Xfer_t *X = &STOR_Data.Xfer;
    if (X->RxState)
    {
        OS_close(X->RxFd);
        OS_remove(X->RxPart);
    }
    X->RxState = 0;
    X->RxErrors++;
    CFE_EVS_SendEvent(STOR_XFER_RX_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: inbound transfer %u failed, status %u",
                      (unsigned int)X->RxTid, (unsigned int)Status);
    if (Ack) STOR_SendAck(X->RxTid, X->RxSrc, Status, 0);
}

void STOR_XferWakeup(void)
{
    STOR_Xfer_t *X = &STOR_Data.Xfer;
    if (X->RxState && ++X->RxIdle >= 10) STOR_RxEnd(STOR_ST_TIMEOUT, false);
    if (X->State == 2)
    {
        if (++X->AckWait >= 10) STOR_XferFail(STOR_ST_TIMEOUT, false);
        return;
    }
    if (X->State == 1)
    {
        osal_id_t Fd;
        int       n = 0;
        if (OS_OpenCreate(&Fd, X->Abs, OS_FILE_FLAG_NONE, OS_READ_ONLY) != OS_SUCCESS) { STOR_XferFail(STOR_ST_IO, false); return; }
        OS_lseek(Fd, (osal_offset_t)X->NextSeg * STOR_XFER_DATA_LEN, OS_SEEK_SET);
        while (n < 8 && X->NextSeg < X->SegCount)
        {
            STOR_XferSeg_Payload_t *S = X->Dest == 0 ? &STOR_Data.XferTlm.Payload : &STOR_Data.XferSeg.Payload;
            uint32 off = (uint32)X->NextSeg * STOR_XFER_DATA_LEN;
            uint16 len = (uint16)(X->FileSize - off < STOR_XFER_DATA_LEN ? X->FileSize - off : STOR_XFER_DATA_LEN);
            int32  got = 0;
            memset(S, 0, sizeof(*S));
            if (len > 0) got = OS_read(Fd, S->Data, len);
            if (got != (int32)len) { OS_close(Fd); STOR_XferFail(STOR_ST_IO, false); return; }
            S->TransferId = X->ActiveId;
            S->SrcNode = STOR_Node();
            S->DstNode = X->Dest;
            S->Area = X->Area;
            S->SegmentIndex = X->NextSeg;
            S->SegmentCount = X->SegCount;
            S->FileSize = X->FileSize;
            S->FileCrc = X->FileCrc;
            S->Offset = off;
            S->DataLength = len;
            snprintf(S->DestPath, sizeof(S->DestPath), "%s", X->DestPath);
            if (X->Dest == 0)
            {
                CFE_SB_TimeStampMsg(CFE_MSG_PTR(STOR_Data.XferTlm.TelemetryHeader));
                CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.XferTlm.TelemetryHeader), true);
            }
            else
            {
                CFE_MSG_SetFcnCode(CFE_MSG_PTR(STOR_Data.XferSeg.CommandHeader), 0);
                CFE_SB_TransmitMsg(CFE_MSG_PTR(STOR_Data.XferSeg.CommandHeader), true);
            }
            X->SegsSent++;
            X->NextSeg++;
            n++;
        }
        OS_close(Fd);
        if (X->NextSeg >= X->SegCount)
        {
            if (X->Dest == 0) STOR_XferComplete();
            else { X->State = 2; X->AckWait = 0; }
        }
    }
}

void STOR_RxAck(const STOR_XferAckCmd_t *Ack)
{
    STOR_Xfer_t                  *X = &STOR_Data.Xfer;
    const STOR_XferAck_Payload_t *A = &Ack->Payload;
    if (A->DstNode != STOR_Node() || X->State == 0 || A->TransferId != X->ActiveId || A->SrcNode != X->Dest) return;
    if (A->Status == STOR_ST_OK) STOR_XferComplete();
    else STOR_XferFail(A->Status, false);
}

void STOR_RxSegment(const STOR_XferSegCmd_t *Seg)
{
    STOR_Xfer_t                  *X = &STOR_Data.Xfer;
    const STOR_XferSeg_Payload_t *S = &Seg->Payload;
    if (S->DstNode != STOR_Node()) return;
    if (S->SegmentIndex == 0)
    {
        const char *last;
        os_fstat_t  St;
        if (X->RxState)
        {
            OS_close(X->RxFd);
            OS_remove(X->RxPart);
            X->RxState = 0;
        }
        X->RxSrc = S->SrcNode;
        X->RxTid = S->TransferId;
        if (memchr(S->DestPath, 0, STOR_PATH_LEN) == NULL || !STOR_PathOk(S->DestPath, false)) { STOR_RxEnd(STOR_ST_PATH, true); return; }
        last = strrchr(S->DestPath, '/');
        last = last ? last + 1 : S->DestPath;
        if (strlen(last) + 5 > 31) { STOR_RxEnd(STOR_ST_PATH, true); return; }
        STOR_Abs(X->RxFinal, sizeof(X->RxFinal), S->DestPath);
        if (OS_stat(X->RxFinal, &St) == OS_SUCCESS) { STOR_RxEnd(STOR_ST_EXISTS, true); return; }
        if ((uint64)STOR_Data.FileBytesUsed + S->FileSize > STOR_Data.Tbl.FileQuotaBytes) { STOR_RxEnd(STOR_ST_QUOTA, true); return; }
        snprintf(X->RxPart, sizeof(X->RxPart), "%s.part", X->RxFinal);
        if (!STOR_MakeDirs(X->RxFinal, false) ||
            OS_OpenCreate(&X->RxFd, X->RxPart, OS_FILE_FLAG_CREATE | OS_FILE_FLAG_TRUNCATE, OS_READ_WRITE) != OS_SUCCESS)
        {
            STOR_RxEnd(STOR_ST_IO, true);
            return;
        }
        X->RxState = 1;
        X->RxNext  = 0;
        X->RxCount = S->SegmentCount;
        X->RxSize  = S->FileSize;
        X->RxCrc   = S->FileCrc;
        X->RxRun   = 0;
    }
    else if (!X->RxState || S->SrcNode != X->RxSrc || S->TransferId != X->RxTid)
    {
        return;
    }
    X->RxIdle = 0;
    X->SegsReceived++;
    if (S->SegmentIndex != X->RxNext) { STOR_RxEnd(STOR_ST_SEQUENCE, true); return; }
    if (S->DataLength > 0 && OS_write(X->RxFd, S->Data, S->DataLength) != (int32)S->DataLength) { STOR_RxEnd(STOR_ST_IO, true); return; }
    X->RxRun = STOR_Crc32(X->RxRun, S->Data, S->DataLength);
    X->RxNext++;
    if (X->RxNext < X->RxCount) return;
    if (X->RxRun != X->RxCrc) { STOR_RxEnd(STOR_ST_CRC, true); return; }
    OS_close(X->RxFd);
    if (OS_mv(X->RxPart, X->RxFinal) != OS_SUCCESS)
    {
        OS_remove(X->RxPart);
        X->RxState = 0;
        X->RxErrors++;
        CFE_EVS_SendEvent(STOR_XFER_RX_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: inbound rename failed");
        STOR_SendAck(X->RxTid, X->RxSrc, STOR_ST_IO, 0);
        return;
    }
    X->RxState = 0;
    STOR_Data.FileCount++;
    STOR_Data.FileBytesUsed += X->RxSize;
    X->FilesReceived++;
    STOR_SendAck(X->RxTid, X->RxSrc, STOR_ST_OK, X->RxSize);
    CFE_EVS_SendEvent(STOR_XFER_RX_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR: received %s (%u bytes)", X->RxFinal,
                      (unsigned int)X->RxSize);
}
