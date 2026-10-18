#ifndef DEFAULT_STOR_MSGDEFS_H
#define DEFAULT_STOR_MSGDEFS_H

#include "common_types.h"
#include "cfe_sb_api_typedefs.h"
#include "stor_fcncodes.h"
#include "stor_tbldefs.h"

#define STOR_FILE_SUBTYPE          0x5354
#define STOR_LIST_ENTRIES_PER_PKT  4

typedef struct STOR_SetPriority_Payload
{
    CFE_SB_MsgId_t MsgId;
    uint8          Priority;
    uint8          Spare[3];
} STOR_SetPriority_Payload_t;

typedef struct STOR_SetDecimation_Payload
{
    CFE_SB_MsgId_t MsgId;
    uint8          Decimation;
    uint8          Spare[3];
} STOR_SetDecimation_Payload_t;

typedef struct STOR_SetPurgeThreshold_Payload
{
    uint8 Percent;
    uint8 Spare[3];
} STOR_SetPurgeThreshold_Payload_t;

typedef struct STOR_AddFile_Payload
{
    char SourcePath[STOR_PATH_LEN];
    char DestPath[STOR_PATH_LEN];
} STOR_AddFile_Payload_t;

typedef struct STOR_DeleteFile_Payload
{
    char Path[STOR_PATH_LEN];
} STOR_DeleteFile_Payload_t;

typedef struct STOR_ListDir_Payload
{
    char Path[STOR_PATH_LEN];
} STOR_ListDir_Payload_t;

typedef struct
{
    char   Name[STOR_PATH_LEN];
    uint32 Size;
    uint32 Spare;
} STOR_ListEntry_t;

typedef struct
{
    uint16           TotalEntries;
    uint16           PacketIndex;
    uint16           EntriesInPacket;
    uint16           Spare;
    STOR_ListEntry_t Entries[STOR_LIST_ENTRIES_PER_PKT];
} STOR_List_Payload_t;

typedef struct
{
    CFE_SB_MsgId_t MsgId;
    uint8          Priority;
    uint8          Decimation;
    uint16         Spare;
    uint32         Received;
    uint32         Stored;
    uint32         Decimated;
    uint32         Dropped;
} STOR_EntryStats_t;

typedef struct
{
    STOR_EntryStats_t Entries[STOR_MAX_ENTRIES];
} STOR_Stats_Payload_t;

typedef struct STOR_HkTlm_Payload
{
    uint8  CommandCounter;
    uint8  CommandErrorCounter;
    uint8  PurgeThresholdPercent;
    uint8  Spare;
    uint32 TelemetryBytesUsed;
    uint32 TelemetryQuotaBytes;
    uint32 FileBytesUsed;
    uint32 FileQuotaBytes;
    uint32 PacketsStored;
    uint32 PacketsDecimated;
    uint32 PacketsDropped;
    uint32 FilesPurged;
    uint32 PurgeCycles;
    uint16 FileAddsRejected;
    uint16 FileCount;
    uint16 ActiveEntries;
    uint16 OpenPacketFiles;
    uint8  XferState;
    uint8  XferLastStatus;
    uint8  RxState;
    uint8  Spare2;
    uint32 XferActiveId;
    uint16 XfersCompleted;
    uint16 XfersFailed;
    uint32 XferSegmentsSent;
    uint32 XferSegmentsReceived;
    uint16 FilesReceived;
    uint16 RxErrors;
} STOR_HkTlm_Payload_t;

#endif
