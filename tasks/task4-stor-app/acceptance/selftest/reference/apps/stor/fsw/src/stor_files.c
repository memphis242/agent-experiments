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
