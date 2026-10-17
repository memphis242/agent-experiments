#ifndef DEFAULT_STOR_TBLDEFS_H
#define DEFAULT_STOR_TBLDEFS_H

#include "common_types.h"
#include "cfe_sb_api_typedefs.h"

#define STOR_MAX_ENTRIES 32
#define STOR_PATH_LEN    64

typedef struct
{
    CFE_SB_MsgId_t MsgId;
    uint8          Priority;
    uint8          Decimation;
    uint16         Spare;
} STOR_EntryDef_t;

typedef struct
{
    char            TelemetryRoot[STOR_PATH_LEN];
    char            FileRoot[STOR_PATH_LEN];
    uint32          TelemetryQuotaBytes;
    uint32          FileQuotaBytes;
    uint32          MaxPacketFileBytes;
    uint8           PurgeThresholdPercent;
    uint8           Spare[3];
    STOR_EntryDef_t Entries[STOR_MAX_ENTRIES];
} STOR_StorageTable_t;

#endif
