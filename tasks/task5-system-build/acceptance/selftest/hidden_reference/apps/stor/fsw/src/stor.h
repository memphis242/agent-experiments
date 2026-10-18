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

typedef struct
{
    uint32 MsgIdValue;
    uint32 Seq;
    uint32 Size;
    bool   Archived;
} STOR_ClosedFile_t;

#define STOR_XFER_TLM_MID  CFE_PLATFORM_TLM_TOPICID_TO_MIDV(0xC7)
#define STOR_XFER_SEG_MID  CFE_GLOBAL_CMD_TOPICID_TO_MIDV(0x18)
#define STOR_XFER_ACK_MID  CFE_GLOBAL_CMD_TOPICID_TO_MIDV(0x19)
#define STOR_HB_MID        CFE_PLATFORM_CMD_TOPICID_TO_MIDV(0xDF)
#define STOR_XFER_DATA_LEN 512

typedef struct { uint32 TransferId; uint8 SrcNode; uint8 DstNode; uint8 Area; uint8 Spare; uint16 SegmentIndex;
                 uint16 SegmentCount; uint32 FileSize; uint32 FileCrc; uint32 Offset; uint16 DataLength; uint16 Spare2;
                 char DestPath[STOR_PATH_LEN]; uint8 Data[STOR_XFER_DATA_LEN]; } STOR_XferSeg_Payload_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; STOR_XferSeg_Payload_t Payload; } STOR_XferSegCmd_t;
typedef struct { CFE_MSG_TelemetryHeader_t TelemetryHeader; STOR_XferSeg_Payload_t Payload; } STOR_XferTlm_t;
typedef struct { uint32 TransferId; uint8 SrcNode; uint8 DstNode; uint8 Status; uint8 Spare; uint32 BytesReceived; } STOR_XferAck_Payload_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; STOR_XferAck_Payload_t Payload; } STOR_XferAckCmd_t;
typedef struct { uint8 Area; uint8 Destination; uint8 SuccessAction; uint8 Spare; char Path[STOR_PATH_LEN];
                 char DestPath[STOR_PATH_LEN]; } STOR_Transfer_Payload_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; STOR_Transfer_Payload_t Payload; } STOR_TransferCmd_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; } STOR_AbortTransferCmd_t;
typedef struct { CFE_MSG_CommandHeader_t CommandHeader; char AppName[CFE_MISSION_MAX_API_LEN]; uint32 Sequence; } STOR_HbCmd_t;

typedef struct
{
    uint8  State, LastStatus, Area, Dest, Action;
    uint32 ActiveId, NextId;
    char   Path[STOR_PATH_LEN];
    char   DestPath[STOR_PATH_LEN];
    char   Abs[192];
    uint32 FileSize, FileCrc;
    uint16 SegCount, NextSeg;
    uint32 AckWait;
    uint16 XfersCompleted, XfersFailed;
    uint32 SegsSent, SegsReceived;
    uint16 FilesReceived, RxErrors;
    uint8  RxState, RxSrc;
    uint32 RxTid, RxSize, RxCrc, RxRun, RxIdle;
    uint16 RxNext, RxCount;
    char   RxPart[200];
    char   RxFinal[192];
    osal_id_t RxFd;
    uint32 HbSeq;
} STOR_Xfer_t;

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
    uint32 NextSeq;

    STOR_StorageTable_t Tbl;
    STOR_Entry_t        Entries[STOR_MAX_ENTRIES];
    STOR_ClosedFile_t   Closed[STOR_MAX_FILES];
    uint32              NumClosed;

    STOR_Xfer_t     Xfer;
    STOR_XferTlm_t  XferTlm;
    STOR_XferSegCmd_t XferSeg;
    STOR_XferAckCmd_t XferAck;
    STOR_HbCmd_t    Hb;
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
uint32 STOR_Crc32(uint32 Crc, const uint8 *Data, size_t Len);
void  STOR_Transfer(const STOR_TransferCmd_t *Cmd);
void  STOR_AbortTransfer(void);
void  STOR_XferWakeup(void);
void  STOR_RxSegment(const STOR_XferSegCmd_t *Seg);
void  STOR_RxAck(const STOR_XferAckCmd_t *Ack);
bool  STOR_PacketName(const char *Name, uint32 *Mid, uint32 *Seq, bool *Arc);
void  STOR_ForgetClosed(uint32 Mid, uint32 Seq, bool Arc, bool Archive);

#endif
