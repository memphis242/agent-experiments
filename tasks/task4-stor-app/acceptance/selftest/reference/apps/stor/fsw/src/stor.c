#include "stor.h"
#include "stor_version.h"
#include "stor_topicids.h"

STOR_Data_t STOR_Data;

void STOR_AppMain(void)
{
    CFE_Status_t     status;
    CFE_SB_Buffer_t *SBBufPtr;

    CFE_ES_PerfLogEntry(STOR_PERF_ID);

    status = STOR_Init();
    if (status != CFE_SUCCESS)
    {
        STOR_Data.RunStatus = CFE_ES_RunStatus_APP_ERROR;
    }

    while (CFE_ES_RunLoop(&STOR_Data.RunStatus) == true)
    {
        CFE_ES_PerfLogExit(STOR_PERF_ID);
        status = CFE_SB_ReceiveBuffer(&SBBufPtr, STOR_Data.CommandPipe, CFE_SB_PEND_FOREVER);
        CFE_ES_PerfLogEntry(STOR_PERF_ID);

        if (status == CFE_SUCCESS)
        {
            STOR_TaskPipe(SBBufPtr);
        }
        else
        {
            CFE_EVS_SendEvent(STOR_PIPE_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: SB pipe read error 0x%08lX",
                              (unsigned long)status);
            STOR_Data.RunStatus = CFE_ES_RunStatus_APP_ERROR;
        }
    }

    CFE_ES_PerfLogExit(STOR_PERF_ID);
    CFE_ES_ExitApp(STOR_Data.RunStatus);
}

CFE_Status_t STOR_Init(void)
{
    CFE_Status_t status;
    char         VersionString[STOR_CFG_MAX_VERSION_STR_LEN];

    memset(&STOR_Data, 0, sizeof(STOR_Data));
    STOR_Data.RunStatus = CFE_ES_RunStatus_APP_RUN;

    status = CFE_EVS_Register(NULL, 0, CFE_EVS_EventFilter_BINARY);
    if (status != CFE_SUCCESS)
    {
        CFE_ES_WriteToSysLog("STOR: error registering events, RC = 0x%08lX\n", (unsigned long)status);
        return status;
    }

    CFE_MSG_Init(CFE_MSG_PTR(STOR_Data.HkTlm.TelemetryHeader), CFE_SB_ValueToMsgId(STOR_HK_TLM_MID),
                 sizeof(STOR_Data.HkTlm));
    CFE_MSG_Init(CFE_MSG_PTR(STOR_Data.ListTlm.TelemetryHeader), CFE_SB_ValueToMsgId(STOR_LIST_TLM_MID),
                 sizeof(STOR_Data.ListTlm));
    CFE_MSG_Init(CFE_MSG_PTR(STOR_Data.StatsTlm.TelemetryHeader), CFE_SB_ValueToMsgId(STOR_STATS_TLM_MID),
                 sizeof(STOR_Data.StatsTlm));

    status = CFE_SB_CreatePipe(&STOR_Data.CommandPipe, STOR_PLATFORM_PIPE_DEPTH, STOR_PLATFORM_PIPE_NAME);
    if (status != CFE_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_SUB_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: error creating pipe, RC = 0x%08lX",
                          (unsigned long)status);
        return status;
    }

    status = CFE_SB_Subscribe(CFE_SB_ValueToMsgId(STOR_SEND_HK_MID), STOR_Data.CommandPipe);
    if (status == CFE_SUCCESS)
    {
        status = CFE_SB_Subscribe(CFE_SB_ValueToMsgId(STOR_CMD_MID), STOR_Data.CommandPipe);
    }
    if (status == CFE_SUCCESS)
    {
        status = CFE_SB_Subscribe(CFE_SB_ValueToMsgId(STOR_WAKEUP_MID), STOR_Data.CommandPipe);
    }
    if (status != CFE_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_SUB_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: error subscribing, RC = 0x%08lX",
                          (unsigned long)status);
        return status;
    }

    status = CFE_TBL_Register(&STOR_Data.TblHandle, "StorageTable", sizeof(STOR_StorageTable_t), CFE_TBL_OPT_DEFAULT,
                              STOR_TableValidate);
    if (status != CFE_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_TBL_REG_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: error registering table, RC = 0x%08lX",
                          (unsigned long)status);
        return status;
    }

    status = CFE_TBL_Load(STOR_Data.TblHandle, CFE_TBL_SRC_FILE, STOR_PLATFORM_TABLE_FILE);
    if (status != CFE_SUCCESS)
    {
        CFE_EVS_SendEvent(STOR_TBL_LOAD_ERR_EID, CFE_EVS_EventType_ERROR, "STOR: error loading table, RC = 0x%08lX",
                          (unsigned long)status);
        return status;
    }

    STOR_PollTable();
    if (STOR_Data.Tbl.TelemetryQuotaBytes == 0)
    {
        return CFE_STATUS_EXTERNAL_RESOURCE_FAIL;
    }

    if (STOR_MakeDirs(STOR_Data.Tbl.TelemetryRoot, true) == false || STOR_MakeDirs(STOR_Data.Tbl.FileRoot, true) == false)
    {
        return CFE_STATUS_EXTERNAL_RESOURCE_FAIL;
    }
    STOR_ScanTelemetry();
    STOR_ScanFiles();

    CFE_Config_GetVersionString(VersionString, STOR_CFG_MAX_VERSION_STR_LEN, "STOR", STOR_VERSION, STOR_BUILD_CODENAME,
                                STOR_LAST_OFFICIAL);
    CFE_EVS_SendEvent(STOR_INIT_INF_EID, CFE_EVS_EventType_INFORMATION, "STOR Initialized.%s", VersionString);
    return CFE_SUCCESS;
}
