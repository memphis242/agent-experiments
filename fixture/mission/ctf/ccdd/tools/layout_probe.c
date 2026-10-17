/*
 * Prints the authoritative wire layout of the messages that ccdd/json/ describes:
 * MID values, sizeof() of whole messages, and offsetof() of header/payload fields.
 * Built and run by probe_layout.sh against the include paths of a cFS build tree.
 * Extend the three tables below when adding an app (see ccdd/README.md).
 */
#include <stdio.h>
#include <stddef.h>
#include "cfe.h"
#include "cfe_es_msgids.h"
#include "cfe_evs_msgids.h"
#include "cfe_es_msgstruct.h"
#include "cfe_evs_msgstruct.h"
#include "sample_app_msgids.h"
#include "sample_app_msgstruct.h"
#include "sample_app_fcncodes.h"
#include "to_lab_msgids.h"
#include "to_lab_msgstruct.h"
#include "to_lab_fcncodes.h"
#include "ci_lab_msgids.h"
#include "ci_lab_msgstruct.h"
#include "ci_lab_fcncodes.h"
#include "hk_msgids.h"
#include "hk_msgdefs.h"
#include "hk_msgstruct.h"
#include "hk_fcncodes.h"

#define MID(name)  printf("MID %s 0x%04X\n", #name, (unsigned)(name))
#define CC(name)   printf("CC %s %d\n", #name, (int)(name))
#define SZ(type)   printf("SIZE %s %zu\n", #type, sizeof(type))
#define OFF(type, field) printf("OFFSET %s.%s %zu\n", #type, #field, offsetof(type, field))

int main(void)
{
    /* --- message ids --- */
    MID(CFE_ES_CMD_MID);          MID(CFE_ES_SEND_HK_MID);       MID(CFE_ES_HK_TLM_MID);
    MID(CFE_EVS_CMD_MID);         MID(CFE_EVS_SEND_HK_MID);      MID(CFE_EVS_HK_TLM_MID);
    MID(CFE_EVS_LONG_EVENT_MSG_MID); MID(CFE_EVS_SHORT_EVENT_MSG_MID);
    MID(SAMPLE_APP_CMD_MID);      MID(SAMPLE_APP_SEND_HK_MID);   MID(SAMPLE_APP_HK_TLM_MID);
    MID(TO_LAB_CMD_MID);          MID(TO_LAB_SEND_HK_MID);       MID(TO_LAB_HK_TLM_MID);
    MID(CI_LAB_CMD_MID);          MID(CI_LAB_SEND_HK_MID);       MID(CI_LAB_HK_TLM_MID);
    MID(HK_CMD_MID);              MID(HK_SEND_HK_MID);           MID(HK_HK_TLM_MID);
    MID(HK_SEND_COMBINED_PKT_MID);

    /* --- function codes --- */
    CC(SAMPLE_APP_NOOP_CC); CC(SAMPLE_APP_RESET_COUNTERS_CC); CC(SAMPLE_APP_PROCESS_CC); CC(SAMPLE_APP_DISPLAY_PARAM_CC);
    CC(TO_LAB_NOOP_CC); CC(TO_LAB_RESET_STATUS_CC); CC(TO_LAB_OUTPUT_ENABLE_CC);
    CC(CI_LAB_NOOP_CC); CC(CI_LAB_RESET_COUNTERS_CC);
    CC(CFE_ES_NOOP_CC); CC(CFE_ES_RESET_COUNTERS_CC); CC(CFE_EVS_NOOP_CC); CC(CFE_EVS_RESET_COUNTERS_CC);
    CC(HK_NOOP_CC); CC(HK_RESET_COUNTERS_CC);

    /* --- headers and message sizes --- */
    SZ(CFE_MSG_CommandHeader_t);
    SZ(CFE_MSG_TelemetryHeader_t);
    SZ(CFE_ES_NoopCmd_t);
    SZ(CFE_ES_ResetCountersCmd_t);
    SZ(CFE_EVS_ResetCountersCmd_t);
    SZ(SAMPLE_APP_ResetCountersCmd_t);
    SZ(SAMPLE_APP_ProcessCmd_t);
    SZ(TO_LAB_ResetCountersCmd_t);
    SZ(CI_LAB_ResetCountersCmd_t);
    SZ(HK_ResetCountersCmd_t);
    SZ(CFE_ES_SendHkCmd_t);
    SZ(CFE_EVS_NoopCmd_t);
    SZ(CFE_EVS_SendHkCmd_t);
    SZ(SAMPLE_APP_NoopCmd_t);
    SZ(SAMPLE_APP_SendHkCmd_t);
    SZ(TO_LAB_NoopCmd_t);
    SZ(TO_LAB_HkTlm_t);
    SZ(CI_LAB_HkTlm_t);
    SZ(HK_SendHkCmd_t);
    SZ(CFE_EVS_ShortEventTlm_t);
    SZ(SAMPLE_APP_DisplayParamCmd_t);
    SZ(SAMPLE_APP_HkTlm_t);
    SZ(TO_LAB_EnableOutputCmd_t);
    SZ(CI_LAB_NoopCmd_t);
    SZ(HK_NoopCmd_t);
    SZ(HK_SendCombinedPktCmd_t);
    SZ(HK_HkPacket_t);
    SZ(CFE_ES_HousekeepingTlm_t);
    SZ(CFE_EVS_HousekeepingTlm_t);
    SZ(CFE_EVS_LongEventTlm_t);

    /* --- field offsets (counted from the start of the message) --- */
    OFF(SAMPLE_APP_DisplayParamCmd_t, Payload.ValU32);
    OFF(SAMPLE_APP_DisplayParamCmd_t, Payload.ValI16);
    OFF(SAMPLE_APP_DisplayParamCmd_t, Payload.ValStr);
    OFF(SAMPLE_APP_HkTlm_t, Payload.CommandCounter);
    OFF(SAMPLE_APP_HkTlm_t, Payload.CommandErrorCounter);
    OFF(TO_LAB_EnableOutputCmd_t, Payload.dest_IP);
    OFF(CI_LAB_HkTlm_t, Payload.IngestPackets);
    OFF(CI_LAB_HkTlm_t, Payload.IngestErrors);
    OFF(HK_SendCombinedPktCmd_t, Payload.OutMsgToSend);
    OFF(HK_HkPacket_t, Payload.CommandCounter);
    OFF(HK_HkPacket_t, Payload.CommandErrorCounter);
    OFF(HK_HkPacket_t, Payload.Padding);
    OFF(HK_HkPacket_t, Payload.CombinedPacketsSent);
    OFF(HK_HkPacket_t, Payload.MissingDataCtr);
    OFF(HK_HkPacket_t, Payload.MemPoolHandle);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.CommandCounter);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.CommandErrorCounter);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.CFECoreChecksum);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.CFEMajorVersion);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PSPMissionRevision);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.SysLogBytesUsed);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.SysLogSize);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.SysLogEntries);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.ERLogEntries);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.RegisteredCoreApps);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.RegisteredLibs);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.ResetType);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.BootSource);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PerfState);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PerfFilterMask);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PerfTriggerMask);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PerfDataStart);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.PerfDataToWrite);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.HeapBytesFree);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.HeapBlocksFree);
    OFF(CFE_ES_HousekeepingTlm_t, Payload.HeapMaxBlockSize);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.CommandCounter);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.CommandErrorCounter);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.MessageFormatMode);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.MessageSendCounter);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.LogEnabled);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.Padding);
    OFF(CFE_EVS_HousekeepingTlm_t, Payload.AppData);
    OFF(CFE_EVS_LongEventTlm_t, Payload.PacketID.AppName);
    OFF(CFE_EVS_LongEventTlm_t, Payload.PacketID.EventID);
    OFF(CFE_EVS_LongEventTlm_t, Payload.PacketID.EventType);
    OFF(CFE_EVS_LongEventTlm_t, Payload.PacketID.SpacecraftID);
    OFF(CFE_EVS_LongEventTlm_t, Payload.PacketID.ProcessorID);
    OFF(CFE_EVS_LongEventTlm_t, Payload.Message);
    OFF(CFE_EVS_LongEventTlm_t, Payload.Padding);
    printf("SIZE CFE_EVS_AppTlmData_t %zu\n", sizeof(CFE_EVS_AppTlmData_t));
    printf("CONST CFE_MISSION_ES_MAX_APPLICATIONS %d\n", (int)CFE_MISSION_ES_MAX_APPLICATIONS);
    printf("CONST CFE_MISSION_MAX_API_LEN %d\n", (int)CFE_MISSION_MAX_API_LEN);
    printf("CONST CFE_MISSION_EVS_MAX_MESSAGE_LENGTH %d\n", (int)CFE_MISSION_EVS_MAX_MESSAGE_LENGTH);
    printf("CONST CFE_MISSION_ES_PERF_MAX_IDS %d\n", (int)CFE_MISSION_ES_PERF_MAX_IDS);
    printf("CONST SAMPLE_APP_MISSION_STRING_VAL_LEN %d\n", (int)SAMPLE_APP_MISSION_STRING_VAL_LEN);
    return 0;
}
