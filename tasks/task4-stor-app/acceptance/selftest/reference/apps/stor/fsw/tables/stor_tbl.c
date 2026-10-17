#include "cfe_tbl_filedef.h"
#include "cfe_msgids.h"
#include "sample_app_msgids.h"
#include "stor_tbl.h"

STOR_StorageTable_t StorageTable = {
    .TelemetryRoot         = "/cf/stor/tlm",
    .FileRoot              = "/cf/stor/files",
    .TelemetryQuotaBytes   = 262144,
    .FileQuotaBytes        = 1048576,
    .MaxPacketFileBytes    = 16384,
    .PurgeThresholdPercent = 80,
    .Entries =
        {
            {CFE_SB_MSGID_WRAP_VALUE(CFE_EVS_HK_TLM_MID), 10, 1, 0},
            {CFE_SB_MSGID_WRAP_VALUE(CFE_ES_HK_TLM_MID), 20, 1, 0},
            {CFE_SB_MSGID_WRAP_VALUE(SAMPLE_APP_HK_TLM_MID), 30, 2, 0},
            {CFE_SB_MSGID_WRAP_VALUE(CFE_TIME_HK_TLM_MID), 40, 1, 0},
        },
};

CFE_TBL_FILEDEF(StorageTable, STOR.StorageTable, STOR Storage Table, stor_tbl.tbl)
