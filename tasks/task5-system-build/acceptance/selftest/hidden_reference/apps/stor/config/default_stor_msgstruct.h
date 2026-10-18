#ifndef DEFAULT_STOR_MSGSTRUCT_H
#define DEFAULT_STOR_MSGSTRUCT_H

#include "stor_mission_cfg.h"
#include "stor_msgdefs.h"
#include "cfe_msg_hdr.h"

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_NoopCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_ResetCountersCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_SetPriority_Payload_t Payload;
} STOR_SetPriorityCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_SetDecimation_Payload_t Payload;
} STOR_SetDecimationCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_SetPurgeThreshold_Payload_t Payload;
} STOR_SetPurgeThresholdCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_AddFile_Payload_t Payload;
} STOR_AddFileCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_DeleteFile_Payload_t Payload;
} STOR_DeleteFileCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
    STOR_ListDir_Payload_t Payload;
} STOR_ListDirCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_ListAllCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_SendStatsCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_CloseFilesCmd_t;

typedef struct
{
    CFE_MSG_CommandHeader_t CommandHeader;
} STOR_SendHkCmd_t;

typedef struct
{
    CFE_MSG_TelemetryHeader_t TelemetryHeader;
    STOR_HkTlm_Payload_t      Payload;
} STOR_HkTlm_t;

typedef struct
{
    CFE_MSG_TelemetryHeader_t TelemetryHeader;
    STOR_List_Payload_t       Payload;
} STOR_ListTlm_t;

typedef struct
{
    CFE_MSG_TelemetryHeader_t TelemetryHeader;
    STOR_Stats_Payload_t      Payload;
} STOR_StatsTlm_t;

#endif
