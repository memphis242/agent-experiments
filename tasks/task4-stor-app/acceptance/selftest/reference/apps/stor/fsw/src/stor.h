#ifndef STOR_H
#define STOR_H

#include "cfe.h"
#include "cfe_config.h"
#include "stor_mission_cfg.h"
#include "stor_platform_cfg.h"
#include "stor_perfids.h"
#include "stor_msgids.h"
#include "stor_msg.h"
#include "stor_tbl.h"
#include "stor_eventids.h"

#define STOR_MAX_FILES      2048
#define STOR_MAX_LIST       512
#define STOR_IDLE_CLOSE_SEC 10
#define STOR_MAX_SCAN_MIDS  64

typedef struct
{
    uint32 MsgIdValue;
    uint32 Seq;
    uint32 Size;
} STOR_ClosedFile_t;

/* Highest packet file sequence of one MID found by the startup scan. */
typedef struct
{
    uint32 MsgIdValue;
    uint32 Seq;
} STOR_ScanSeq_t;

typedef struct
{
    bool   Used;
    uint32 MsgIdValue;
    uint8  Priority;
    uint8  Decimation;
    uint32 Received;
    uint32 Stored;
    uint32 Decimated;
    uint32 Dropped;
    bool   DropReported;
    bool   Open;
    osal_id_t Fd;
    uint32 OpenSeq;
    uint32 NextSeq;
    bool   SeqInit;
    uint32 OpenBytes;
    uint32 LastWrite;
} STOR_Entry_t;

typedef struct
{
    uint8  CommandCounter;
    uint8  CommandErrorCounter;
    uint8  PurgeThresholdPercent;
    uint32 TelemetryBytesUsed;
    uint32 FileBytesUsed;
    uint32 PacketsStored;
    uint32 PacketsDecimated;
    uint32 PacketsDropped;
    uint32 FilesPurged;
    uint32 PurgeCycles;
    uint16 FileAddsRejected;
    uint16 FileCount;

    STOR_StorageTable_t Tbl;
    STOR_Entry_t        Entries[STOR_MAX_ENTRIES];
    STOR_ClosedFile_t   Closed[STOR_MAX_FILES];
    uint32              NumClosed;
    STOR_ScanSeq_t      ScanSeq[STOR_MAX_SCAN_MIDS];
    uint32              NumScanSeq;

    STOR_HkTlm_t    HkTlm;
    STOR_ListTlm_t  ListTlm;
    STOR_StatsTlm_t StatsTlm;

    uint32           RunStatus;
    CFE_SB_PipeId_t  CommandPipe;
    CFE_TBL_Handle_t TblHandle;
} STOR_Data_t;

extern STOR_Data_t STOR_Data;

void         STOR_AppMain(void);
CFE_Status_t STOR_Init(void);
void         STOR_TaskPipe(const CFE_SB_Buffer_t *SBBufPtr);

/* stor_tlm.c */
int32 STOR_TableValidate(void *TblData);
void  STOR_PollTable(void);
void  STOR_StorePacket(STOR_Entry_t *Entry, const CFE_SB_Buffer_t *SBBufPtr);
void  STOR_Purge(void);
void  STOR_Wakeup(void);
void  STOR_ScanTelemetry(void);
uint32 STOR_CloseAll(void);
STOR_Entry_t *STOR_FindEntry(uint32 MsgIdValue);
void  STOR_SendHk(void);
void  STOR_SendStats(void);

/* stor_files.c */
bool  STOR_MakeDirs(const char *Path, bool IncludeLast);
void  STOR_ScanFiles(void);
void  STOR_AddFile(const STOR_AddFileCmd_t *Cmd);
void  STOR_DeleteFile(const STOR_DeleteFileCmd_t *Cmd);
void  STOR_ListDir(const STOR_ListDirCmd_t *Cmd);
void  STOR_ListAll(void);

#endif
