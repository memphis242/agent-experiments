"""Identifiers and layouts pinned by the Task 5 specification (TASK.md), plus the cFE values of this build.

Everything the live checks put on the wire or decode is defined here once. Names follow the
specification; values for cFE and the other NASA apps were read from the pinned template build.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

NODES = (1, 2)
HOST = "127.0.0.1"
CMDI_BASE_PORT = 1234
TLMO_BASE_PORT = 2234


def cmdi_port(node: int) -> int:
    assert node in NODES
    return CMDI_BASE_PORT + node - 1


def tlmo_port(node: int) -> int:
    assert node in NODES
    return TLMO_BASE_PORT + node - 1


def cmd_mid(topic: int, node: int = 1) -> int:
    """CFE_PLATFORM_CMD_TOPICID_TO_MIDV on this mission: 0x18xx on cpu1, 0x19xx on cpu2."""
    assert node in NODES and 0 <= topic <= 0xFF
    return (0x1800 if node == 1 else 0x1900) | topic


def tlm_mid(topic: int, node: int = 1) -> int:
    assert node in NODES and 0 <= topic <= 0xFF
    return (0x0800 if node == 1 else 0x0900) | topic


def localize(mid_cpu1: int, node: int) -> int:
    """The cpu2 value of a local cpu1 MID (0x18xx / 0x08xx); global MIDs are unchanged."""
    assert node in NODES
    if node == 1:
        return mid_cpu1
    if mid_cpu1 in GLOBAL_MIDS:
        return mid_cpu1
    hi = mid_cpu1 & 0xFF00
    assert hi in (0x1800, 0x0800), f"{mid_cpu1:#06x} is not a local cpu1 MID"
    return (hi + 0x100) | (mid_cpu1 & 0xFF)


# ----------------------------------------------------------------------------- topics (C.3)

APP_TOPICS: dict[str, dict[str, tuple[str, int]]] = {
    # name -> (cmd|tlm, topic)
    "STOR": {"CMD": ("cmd", 0xC4), "SEND_HK": ("cmd", 0xC5), "WAKEUP": ("cmd", 0xC6), "HK_TLM": ("tlm", 0xC4),
             "LIST_TLM": ("tlm", 0xC5), "STATS_TLM": ("tlm", 0xC6), "XFER_TLM": ("tlm", 0xC7)},
    "CMDI": {"CMD": ("cmd", 0xD0), "SEND_HK": ("cmd", 0xD1), "WAKEUP": ("cmd", 0xD2), "HK_TLM": ("tlm", 0xD0)},
    "TLMO": {"CMD": ("cmd", 0xD4), "SEND_HK": ("cmd", 0xD5), "WAKEUP": ("cmd", 0xD6), "HK_TLM": ("tlm", 0xD4),
             "CLASS_TLM": ("tlm", 0xD5)},
    "GW": {"CMD": ("cmd", 0xD8), "SEND_HK": ("cmd", 0xD9), "WAKEUP": ("cmd", 0xDA), "HK_TLM": ("tlm", 0xD8),
           "CONN_TLM": ("tlm", 0xD9)},
    "SFI": {"CMD": ("cmd", 0xDC), "SEND_HK": ("cmd", 0xDD), "WAKEUP": ("cmd", 0xDE), "HK_TLM": ("tlm", 0xDC),
            "STATUS_TLM": ("tlm", 0xDD), "HEARTBEAT": ("cmd", 0xDF)},
}
NEW_APPS = ("STOR", "CMDI", "TLMO", "GW", "SFI")
XFER_SEG_MID = 0x1878
XFER_ACK_MID = 0x1879
GLOBAL_MIDS = (XFER_SEG_MID, XFER_ACK_MID)


def app_mid(app: str, name: str, node: int = 1) -> int:
    kind, topic = APP_TOPICS[app][name]
    return cmd_mid(topic, node) if kind == "cmd" else tlm_mid(topic, node)


def verif_mid(topic: int, node: int = 1) -> int:
    """Reserved verification command MID (C.3), topics 0xE0..0xEF."""
    assert 0xE0 <= topic <= 0xEF
    return cmd_mid(topic, node)


# ----------------------------------------------------------------------------- cFE and other apps (template build)

CFE_TOPICS_TLM = {"CFE_ES_HK_TLM_MID": 0x00, "CFE_EVS_HK_TLM_MID": 0x01, "CFE_SB_HK_TLM_MID": 0x03,
                  "CFE_TBL_HK_TLM_MID": 0x04, "CFE_TIME_HK_TLM_MID": 0x05, "CFE_TIME_DIAG_TLM_MID": 0x06,
                  "CFE_EVS_LONG_EVENT_MSG_MID": 0x08, "CFE_EVS_SHORT_EVENT_MSG_MID": 0x09,
                  "CFE_SB_STATS_TLM_MID": 0x0A, "CFE_ES_APP_TLM_MID": 0x0B, "CFE_TBL_REG_TLM_MID": 0x0C,
                  "CFE_ES_MEMSTATS_TLM_MID": 0x10, "SAMPLE_APP_HK_TLM_MID": 0x83,
                  "CF_HK_TLM_MID": 0xB0, "CF_EOT_TLM_MID": 0xB3, "CF_CH0_TX_MID": 0xC2, "CF_CH1_TX_MID": 0xC3,
                  "MD_HK_TLM_MID": 0x90, "HS_HK_TLM_MID": 0xAD, "HK_HK_TLM_MID": 0x9B, "SC_HK_TLM_MID": 0xAA,
                  "FM_HK_TLM_MID": 0x8A, "FM_FILE_INFO_TLM_MID": 0x8B, "FM_DIR_LIST_TLM_MID": 0x8C,
                  "FM_OPEN_FILES_TLM_MID": 0x8D, "FM_MONITOR_TLM_MID": 0x8E, "DS_HK_TLM_MID": 0xB8,
                  "LC_HK_TLM_MID": 0xA7, "CS_HK_TLM_MID": 0xA4, "SBN_HK_TLM_MID": 0xFB, "SBN_HKNET_TLM_MID": 0xFC,
                  "SBN_HKPEER_TLM_MID": 0xFD, "SBN_HKMYSUBS_TLM_MID": 0xFE, "SBN_HKPEERSUBS_TLM_MID": 0xFF}
for _app in NEW_APPS:
    for _name, (_kind, _topic) in APP_TOPICS[_app].items():
        if _kind == "tlm":
            CFE_TOPICS_TLM[f"{_app}_{_name}_MID"] = _topic
CFE_ES_CMD_TOPIC = 0x06
CFE_SB_SEND_HK_TOPIC = 0x0B
SB_HK_OVERFLOW_OFFSET = 28  # PipeOverflowErrorCounter (uint16) in CFE_SB_HousekeepingTlm_t
SB_HK_MSGLIM_OFFSET = 30    # MsgLimitErrorCounter (uint16)
CFE_TBL_CMD_TOPIC = 0x04
SAMPLE_APP_CMD_TOPIC = 0x82
SAMPLE_APP_SEND_HK_TOPIC = 0x83


def named_tlm_mid(name: str, node: int = 1) -> int:
    return tlm_mid(CFE_TOPICS_TLM[name], node)


def es_cmd_mid(node: int = 1) -> int:
    return cmd_mid(CFE_ES_CMD_TOPIC, node)


def tbl_cmd_mid(node: int = 1) -> int:
    return cmd_mid(CFE_TBL_CMD_TOPIC, node)


def sample_cmd_mid(node: int = 1) -> int:
    return cmd_mid(SAMPLE_APP_CMD_TOPIC, node)


def sample_hk_mid(node: int = 1) -> int:
    return tlm_mid(0x83, node)


def sample_send_hk_mid(node: int = 1) -> int:
    return cmd_mid(SAMPLE_APP_SEND_HK_TOPIC, node)


def evs_long_mid(node: int = 1) -> int:
    return tlm_mid(0x08, node)


CFE_ES_START_APP_CC = 4
CFE_ES_STOP_APP_CC = 5
CFE_ES_RESTART_APP_CC = 6
CFE_ES_START_INF_EID = 6
CFE_ES_STOP_INF_EID = 8
CFE_ES_RESTART_APP_INF_EID = 10
CFE_TBL_LOAD_CC = 2
CFE_TBL_VALIDATE_CC = 4
CFE_TBL_ACTIVATE_CC = 5
CFE_TBL_FILE_LOADED_INF_EID = 12
CFE_TBL_VALIDATION_INF_EID = 36
CFE_TBL_UPDATE_SUCCESS_INF_EID = 37
CFE_TBL_VALIDATION_ERR_EID = 96
CFE_TBL_UPDATE_ERR_EID = 95
SAMPLE_NOOP_CC = 0

OS_MAX_PATH_LEN = 64
TBL_NAME_LEN = 40
API_LEN = 20
CFE_FS_HDR = 64
TBL_FILE_HDR = 116  # CFE_FS_Header_t + CFE_TBL_File_Hdr_t
TLM_HDR = 16
CMD_HDR = 8


def es_start_app_payload(app: str, entry: str, file: str, priority: int, stack: int, exception_action: int = 0) -> bytes:
    """CFE_ES_StartAppCmd_Payload_t (Application, AppEntryPoint, AppFileName, StackSize, ExceptionAction, Priority)."""
    p = struct.pack("<20s20s64sQBxH4x", app.encode(), entry.encode(), file.encode(), stack, exception_action, priority)
    assert len(p) == 120
    return p


def es_app_name_payload(app: str) -> bytes:
    p = struct.pack("<20s", app.encode())
    assert len(p) == 20
    return p


def tbl_load_payload(path: str) -> bytes:
    raw = path.encode()
    assert len(raw) < OS_MAX_PATH_LEN
    return raw.ljust(OS_MAX_PATH_LEN, b"\0")


def tbl_validate_payload(name: str, active: bool = False) -> bytes:
    raw = name.encode()
    assert len(raw) < TBL_NAME_LEN
    return struct.pack("<H", 1 if active else 0) + raw.ljust(TBL_NAME_LEN, b"\0")


def tbl_activate_payload(name: str) -> bytes:
    raw = name.encode()
    assert len(raw) < TBL_NAME_LEN
    return raw.ljust(TBL_NAME_LEN, b"\0")


# ----------------------------------------------------------------------------- function codes

CC = {
    "STOR": {"NOOP": 0, "RESET_COUNTERS": 1, "SET_PRIORITY": 2, "SET_DECIMATION": 3, "SET_PURGE_THRESHOLD": 4,
             "ADD_FILE": 5, "DELETE_FILE": 6, "LIST_DIR": 7, "LIST_ALL": 8, "SEND_STATS": 9, "CLOSE_FILES": 10,
             "TRANSFER": 11, "ABORT_TRANSFER": 12},
    "CMDI": {"NOOP": 0, "RESET_COUNTERS": 1, "SET_MAX_AGE": 2, "SET_ENABLE_MASK": 3},
    "TLMO": {"NOOP": 0, "RESET_COUNTERS": 1, "SET_CLASS": 2, "SET_BUDGET": 3, "SEND_CLASS_MAP": 4,
             "ADD_STREAM": 5, "REMOVE_STREAM": 6},
    "GW": {"NOOP": 0, "RESET_COUNTERS": 1, "ADD_CONN": 2, "REMOVE_CONN": 3, "ENABLE_CONN": 4, "DISABLE_CONN": 5,
           "RELOAD": 6, "SEND_CONN_STATUS": 7},
    "SFI": {"NOOP": 0, "RESET_COUNTERS": 1, "SET_MISS_LIMIT": 2, "SET_CLUSTER_WINDOW": 3, "SEND_STATUS": 4,
            "CLEAR_HISTORY": 5},
}

# ----------------------------------------------------------------------------- event IDs

COMMON_EIDS = {"INIT_INF": 1, "CC_ERR": 2, "NOOP_INF": 3, "RESET_INF": 4, "MID_ERR": 5, "CMD_LEN_ERR": 6,
               "PIPE_ERR": 7, "TBL_REG_ERR": 8, "TBL_LOAD_ERR": 9, "TBL_VAL_ERR": 10}
EID = {
    "STOR": {**COMMON_EIDS, "SUB_ERR": 11, "DIR_CREATE_ERR": 12, "TLM_QUOTA_ERR": 13, "PURGE_INF": 14,
             "FILE_WRITE_ERR": 15, "SET_PARAM_INF": 16, "SET_PARAM_ERR": 17, "PATH_ERR": 18, "FILE_SRC_ERR": 19,
             "FILE_EXISTS_ERR": 20, "FILE_QUOTA_ERR": 21, "FILE_ADD_INF": 22, "FILE_DEL_ERR": 23, "FILE_DEL_INF": 24,
             "LIST_ERR": 25, "LIST_INF": 26, "CLOSE_INF": 27, "STATS_INF": 28, "XFER_START_INF": 29, "XFER_ERR": 30,
             "XFER_DONE_INF": 31, "XFER_FAIL_ERR": 32, "XFER_ABORT_INF": 33, "XFER_RX_INF": 34, "XFER_RX_ERR": 35,
             "XFER_ACTION_ERR": 36},
    "CMDI": {**COMMON_EIDS, "SOCKET_ERR": 11, "REJECT_ERR": 12, "ACCEPT_DBG": 13, "SET_PARAM_INF": 14,
             "SET_PARAM_ERR": 15},
    "TLMO": {**COMMON_EIDS, "SUB_ERR": 11, "SOCKET_ERR": 12, "SET_CLASS_INF": 13, "SET_CLASS_ERR": 14,
             "SET_BUDGET_INF": 15, "SET_BUDGET_ERR": 16, "DROP_INF": 17, "CLASS_MAP_INF": 18, "TBL_UPDATE_INF": 19,
             "ADD_STREAM_INF": 20, "ADD_STREAM_ERR": 21, "REMOVE_STREAM_INF": 22, "REMOVE_STREAM_ERR": 23},
    "GW": {**COMMON_EIDS, "CONN_ADD_INF": 11, "CONN_ADD_ERR": 12, "CONN_REMOVE_INF": 13, "CONN_REMOVE_ERR": 14,
           "CONN_ENABLE_INF": 15, "CONN_ENABLE_ERR": 16, "CONN_DISABLE_INF": 17, "CONN_DISABLE_ERR": 18,
           "RELOAD_INF": 19, "CONN_UP_INF": 20, "CONN_DOWN_ERR": 21, "EGRESS_ERR": 22, "INGRESS_ERR": 23,
           "CONN_STATUS_INF": 24, "FRAME_REJECT_ERR": 25, "VALTBL_ERR": 26, "VALTBL_INF": 27, "CONN_TIMEOUT_ERR": 28},
    "SFI": {**COMMON_EIDS, "SUB_ERR": 11, "HB_MISS_ERR": 12, "HB_RESTORED_INF": 13, "HB_UNKNOWN_DBG": 14,
            "STALL_ERR": 15, "RATE_ERR": 16, "WATCH_CLEAR_INF": 17, "CLUSTER_ERR": 18, "SET_PARAM_INF": 19,
            "SET_PARAM_ERR": 20, "STATUS_INF": 21, "HISTORY_CLEAR_INF": 22},
}

# ----------------------------------------------------------------------------- enumerations

XFER_AREA = {"FILE": 0, "TLM": 1}
XFER_ACTION = {"KEEP": 0, "DELETE": 1, "MARK_ARCHIVED": 2}
XFER_STATUS = {"OK": 0, "EXISTS": 1, "QUOTA": 2, "SEQUENCE": 3, "CRC": 4, "IO": 5, "PATH": 6, "TIMEOUT": 7,
               "ABORTED": 8, "ACTION_FAILED": 9}
XFER_STATE = {"IDLE": 0, "SENDING": 1, "AWAITING_ACK": 2}
GW_IF = {"NONE": 0, "UDP": 1, "TCP": 2, "SERIAL": 3}
GW_STATE = {"DISABLED": 0, "UP": 1, "CONNECTING": 2, "DOWN": 3}
GW_TCP_ATTEMPT_SEC = 5
GW_TCP_MAX_RETRIES = 3
SFI_APP_STATE = {"UNARMED": 0, "OK": 1, "MISSING": 2}
SFI_WATCH_STATE = {"UNUSED": 0, "BASELINE_PENDING": 1, "OK": 2, "FAULT": 3}
SFI_CORRELATION = {"NONE": 0, "UNCORRELATED": 1, "CLUSTERED": 2}
SFI_RULE = {"UNUSED": 0, "STALL": 1, "RATE": 2}

# ----------------------------------------------------------------------------- sizes pinned by the spec

STOR_PATH_LEN = 64
STOR_MAX_ENTRIES = 32
STOR_XFER_DATA_LEN = 512
STOR_XFER_SEGS_PER_WAKEUP = 8
STOR_FILE_SUBTYPE = 0x5354
TLMO_MAX_STREAMS = 64
TLMO_NUM_CLASSES = 4
GW_MAX_CONNECTIONS = 8
GW_MAX_SUBS = 16
SFI_MAX_APPS = 16
SFI_MAX_WATCHES = 16
SFI_HISTORY_LEN = 16
CMDVAL_MAX_SOURCES = 8


@dataclass(frozen=True)
class Layout:
    """A payload layout: struct format (little-endian, no implicit padding) and field names (None = spare)."""
    fmt: str
    fields: tuple[str | None, ...]

    def __post_init__(self) -> None:
        assert self.fmt.startswith("<")
        assert struct.calcsize(self.fmt) == self.size

    @property
    def size(self) -> int:
        return struct.calcsize(self.fmt)

    def unpack(self, payload: bytes) -> dict[str, int]:
        values = struct.unpack_from(self.fmt, payload)
        assert len(values) == len(self.fields), (len(values), len(self.fields))
        return {f: v for f, v in zip(self.fields, values) if f is not None}


HK_LAYOUTS: dict[str, Layout] = {
    "STOR": Layout("<BBBBIIIIIIIIIHHHHBBBBIHHIIHH", (
        "CommandCounter", "CommandErrorCounter", "PurgeThresholdPercent", None,
        "TelemetryBytesUsed", "TelemetryQuotaBytes", "FileBytesUsed", "FileQuotaBytes",
        "PacketsStored", "PacketsDecimated", "PacketsDropped", "FilesPurged", "PurgeCycles",
        "FileAddsRejected", "FileCount", "ActiveEntries", "OpenPacketFiles",
        "XferState", "XferLastStatus", "RxState", None, "XferActiveId", "XfersCompleted", "XfersFailed",
        "XferSegmentsSent", "XferSegmentsReceived", "FilesReceived", "RxErrors")),
    "CMDI": Layout("<BBBBIIIIIIII", (
        "CommandCounter", "CommandErrorCounter", "EnableMask", None, "FramesReceived", "FramesAccepted",
        "FormatErrors", "CrcErrors", "HmacErrors", "SourceErrors", "AgeErrors", "MaxAgeSeconds")),
    "TLMO": Layout("<BBHHHHHI4I4II", (
        "CommandCounter", "CommandErrorCounter", "BudgetPacketsPerCycle", "QueuedPackets", "QueueCapacity",
        "ActiveStreams", None, "PacketsReceived", "Sent0", "Sent1", "Sent2", "Sent3",
        "Dropped0", "Dropped1", "Dropped2", "Dropped3", "SocketErrors")),
    "GW": Layout("<BBBBIIIIIHBBIIIIII", (
        "CommandCounter", "CommandErrorCounter", "ConnectionsConfigured", "ConnectionsUp",
        "PacketsEgressed", "PacketsIngressed", "EgressErrors", "IngressErrors", "LoopDrops",
        "Subscriptions", "ValTableShared", None, "FramesReceived",
        "FormatErrors", "CrcErrors", "HmacErrors", "SourceErrors", "AgeErrors")),
    "SFI": Layout("<BBBBBBBBHHHHIIIII", (
        "CommandCounter", "CommandErrorCounter", "AppsMonitored", "AppsMissing",
        "WatchesInFault", "Correlation", "HbMissLimit", None,
        "RecentMisses", "ClusterCount", "LargestClusterSize", "ClusterWindowSec",
        "TotalHeartbeats", "UnknownHeartbeats", "HbMissEvents", "StallFaults", "RateFaults")),
}
assert HK_LAYOUTS["STOR"].size == 72
assert HK_LAYOUTS["CMDI"].size == 36
assert HK_LAYOUTS["TLMO"].size == 52
assert HK_LAYOUTS["GW"].size == 52
assert HK_LAYOUTS["SFI"].size == 36

XFER_SEG_FMT = "<IBBBBHHIIIHH64s512s"
XFER_SEG_SIZE = struct.calcsize(XFER_SEG_FMT)
assert XFER_SEG_SIZE == 604
XFER_ACK_FMT = "<IBBBBI"
assert struct.calcsize(XFER_ACK_FMT) == 12
GW_CONN_DEF_FMT = "<BBH16sHH64s16I"
GW_CONN_DEF_SIZE = struct.calcsize(GW_CONN_DEF_FMT)
assert GW_CONN_DEF_SIZE == 152
GW_CONN_STATUS_FMT = "<BBBBIIII"
assert struct.calcsize(GW_CONN_STATUS_FMT) == 20
SFI_APP_STATUS_FMT = "<20sBBHII"
assert struct.calcsize(SFI_APP_STATUS_FMT) == 32
SFI_MISS_RECORD_FMT = "<B3xII"
assert struct.calcsize(SFI_MISS_RECORD_FMT) == 12
SFI_WATCH_STATUS_FMT = "<B3xI"
assert struct.calcsize(SFI_WATCH_STATUS_FMT) == 8
SFI_STATUS_SIZE = 8 + 16 * 32 + 16 * 12 + 16 * 8
assert SFI_STATUS_SIZE == 840
TLMO_CLASS_SIZE = 4 + TLMO_MAX_STREAMS * 8
assert TLMO_CLASS_SIZE == 516
STOR_LIST_ENTRY_FMT = f"<{STOR_PATH_LEN}sII"
STOR_LIST_SIZE = 8 + 4 * struct.calcsize(STOR_LIST_ENTRY_FMT)
assert STOR_LIST_SIZE == 296
STOR_STATS_ENTRY_FMT = "<IBBHIIII"
assert struct.calcsize(STOR_STATS_ENTRY_FMT) == 24

# Table images (bodies after the 116-byte file headers).
STOR_TABLE_SIZE = 400
CMDI_TABLE_SIZE = 56
TLMO_TABLE_SIZE = 516
GW_TABLE_SIZE = GW_MAX_CONNECTIONS * GW_CONN_DEF_SIZE
assert GW_TABLE_SIZE == 1216
SFI_TABLE_SIZE = 8 + SFI_MAX_APPS * API_LEN + SFI_MAX_WATCHES * 12
assert SFI_TABLE_SIZE == 520

TABLES = {  # app -> (table file in /cf, full table name, body size)
    "STOR": ("stor_tbl.tbl", "STOR.StorageTable", STOR_TABLE_SIZE),
    "CMDI": ("cmdi_tbl.tbl", "CMDI.ValTable", CMDI_TABLE_SIZE),
    "TLMO": ("tlmo_tbl.tbl", "TLMO.PriorityTable", TLMO_TABLE_SIZE),
    "GW": ("gw_tbl.tbl", "GW.ConnTable", GW_TABLE_SIZE),
    "SFI": ("sfi_tbl.tbl", "SFI.SfiTable", SFI_TABLE_SIZE),
}

# TLMO default stream classes (S4.1), by name; verification streams are listed as ("VERIF", topic).
TLMO_DEFAULT_CLASSES: dict[int, tuple[str | tuple[str, int], ...]] = {
    0: ("CFE_EVS_LONG_EVENT_MSG_MID", "SFI_HK_TLM_MID", ("VERIF", 0xE8)),
    1: ("CFE_ES_HK_TLM_MID", "CFE_EVS_HK_TLM_MID", "CFE_SB_HK_TLM_MID", "CFE_TBL_HK_TLM_MID", "STOR_HK_TLM_MID",
        "CMDI_HK_TLM_MID", "TLMO_HK_TLM_MID", "GW_HK_TLM_MID", "CF_HK_TLM_MID", "MD_HK_TLM_MID", "HS_HK_TLM_MID",
        "HK_HK_TLM_MID", "SC_HK_TLM_MID", "FM_HK_TLM_MID", "DS_HK_TLM_MID", "LC_HK_TLM_MID", "CS_HK_TLM_MID",
        ("VERIF", 0xE9), "SBN_HK_TLM_MID", "SBN_HKNET_TLM_MID", "SBN_HKPEER_TLM_MID", "SBN_HKMYSUBS_TLM_MID",
        "SBN_HKPEERSUBS_TLM_MID"),
    2: ("CFE_TIME_HK_TLM_MID", "SAMPLE_APP_HK_TLM_MID", "CFE_TIME_DIAG_TLM_MID", "CFE_SB_STATS_TLM_MID",
        "CFE_TBL_REG_TLM_MID", "CFE_ES_APP_TLM_MID", "CFE_ES_MEMSTATS_TLM_MID", "CF_EOT_TLM_MID",
        "FM_FILE_INFO_TLM_MID", "FM_DIR_LIST_TLM_MID", "FM_OPEN_FILES_TLM_MID", "FM_MONITOR_TLM_MID",
        "STOR_LIST_TLM_MID", "STOR_STATS_TLM_MID", "TLMO_CLASS_TLM_MID", "GW_CONN_TLM_MID", "SFI_STATUS_TLM_MID",
        ("VERIF", 0xEA)),
    3: ("CF_CH0_TX_MID", "CF_CH1_TX_MID", "STOR_XFER_TLM_MID", ("VERIF", 0xEB)),
}


def tlmo_default_streams(node: int) -> dict[int, int]:
    """MID -> class of the default PriorityTable on `node` (48 streams with SBN)."""
    out: dict[int, int] = {}
    for cls, names in TLMO_DEFAULT_CLASSES.items():
        for n in names:
            mid = verif_mid(n[1], node) if isinstance(n, tuple) else named_tlm_mid(n, node)
            assert mid not in out
            out[mid] = cls
    assert len(out) == 48
    return out


SFI_DEFAULT_APPS = ("STOR", "CMDI", "TLMO", "GW", "SFI_T1", "SFI_T2", "SFI_T3")
SFI_DEFAULT_WATCH_ROWS = (  # (MID name or verification topic, offset, width, rule, limit)
    ("STOR_HK_TLM_MID", 17, 1, "RATE", 2), ("CMDI_HK_TLM_MID", 17, 1, "RATE", 2),
    ("TLMO_HK_TLM_MID", 17, 1, "RATE", 2), ("GW_HK_TLM_MID", 17, 1, "RATE", 2),
    (0xEC, 16, 4, "STALL", 3), (0xED, 16, 2, "RATE", 5),
)

# Startup script lines of C.6 (fields after the object type).
STARTUP_LINES = {
    "cmdval_lib": ("CFE_LIB", "cmdval_lib", "CMDVAL_LibInit", "CMDVAL_LIB", "0", "0", "0x0", "0"),
    "cmdi": ("CFE_APP", "cmdi", "CMDI_AppMain", "CMDI", "40", "32768", "0x0", "0"),
    "tlmo": ("CFE_APP", "tlmo", "TLMO_AppMain", "TLMO", "45", "32768", "0x0", "0"),
    "stor": ("CFE_APP", "stor", "STOR_AppMain", "STOR", "70", "131072", "0x0", "0"),
    "gw": ("CFE_APP", "gw", "GW_AppMain", "GW", "70", "131072", "0x0", "0"),
    "sfi": ("CFE_APP", "sfi", "SFI_AppMain", "SFI", "70", "131072", "0x0", "0"),
}

COMPONENTS = {  # component -> (directory, source prefix)
    "stor": ("apps/stor", "STOR_"), "cmdi": ("apps/cmdi", "CMDI_"), "tlmo": ("apps/tlmo", "TLMO_"),
    "gw": ("apps/gw", "GW_"), "sfi": ("apps/sfi", "SFI_"), "cmdval_lib": ("libs/cmdval_lib", "CMDVAL_"),
}
