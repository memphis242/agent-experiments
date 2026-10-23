"""Pinned expanded SFI and MODE ABI, independently transcribed from S6/S7."""
from __future__ import annotations

import struct

SFI_FAULT_FMT = '<IBBBBBB2x20sIIII'
SFI_FAULT_SIZE = struct.calcsize(SFI_FAULT_FMT)
SFI_APP_STATUS_FMT = '<20sBBHIIBBH'
SFI_MISS_RECORD_FMT = '<B3xIIBBH'
SFI_WATCH_STATUS_FMT = '<B3xIBBH'
SFI_STATUS_SIZE = 8 + 16 * 36 + 16 * 16 + 16 * 12 + 4 + 57 * 2 + 2 + 57 * 48
SFI_TABLE_SIZE = 8 + 16 * 24 + 16 * 24 + 4
MODE_PROFILE_FMT = '<BBBB'
MODE_RESTART_FMT = '<20s20s64sIHH'
MODE_RESPONSE_FMT = '<8BHBBI4B64s64s'
MODE_STATUS_SIZE = 8 + 12 + 4 + 57 * 12
MODE_TABLE_SIZE = 4 + 3 * 4 + 4 * 112 + 7 * 148
SFI_FAULT = {'NONE': 0, 'HEARTBEAT': 1, 'STALL': 2, 'RATE': 3, 'LIMIT': 4, 'EVENT_RATE': 5,
             'LINK': 6, 'NODE_LOSS': 7}
MODE = {'NOMINAL': 0, 'DEGRADED': 1, 'SAFE': 2}
SFI_DEFAULT_APPS = ('STOR', 'CMDI', 'TLMO', 'GW', 'SFI_T1', 'SFI_T2', 'SFI_T3', 'MODE')
MODE_DEFAULT_PROFILES = ((0, 80, 255, 0), (1, 65, 255, 0), (2, 50, 3, 0))
MODE_DEFAULT_RESTARTS = (('STOR', 'STOR_AppMain', '/cf/stor.so', 131072, 70),
                         ('CMDI', 'CMDI_AppMain', '/cf/cmdi.so', 32768, 40),
                         ('TLMO', 'TLMO_AppMain', '/cf/tlmo.so', 32768, 45),
                         ('GW', 'GW_AppMain', '/cf/gw.so', 131072, 70))
assert SFI_FAULT_SIZE == 48 and SFI_STATUS_SIZE == 3888 and SFI_TABLE_SIZE == 780
assert struct.calcsize(MODE_RESTART_FMT) == 112 and struct.calcsize(MODE_RESPONSE_FMT) == 148
assert MODE_TABLE_SIZE == 1500 and MODE_STATUS_SIZE == 708


def install(s) -> None:  # noqa: ANN001 - the shared spec module
    for name, value in globals().copy().items():
        if name.startswith(('SFI_', 'MODE_')) or name == 'MODE':
            setattr(s, name, value)
    s.SFI_RULE = {'UNUSED': 0, 'STALL': 1, 'RATE': 2, 'LIMIT': 3}
    s.SFI_HEALTH_MID, s.MODE_PEER_MID = 0x187A, 0x187B
    s.HEALTH_MID = 0x187A
    s.APP_TOPICS['SFI'].update(FAULT=('cmd', 0xF3), FAULT_TLM=('tlm', 0xDE))
    s.APP_TOPICS['MODE'] = {'CMD': ('cmd', 0xF0), 'SEND_HK': ('cmd', 0xF1), 'WAKEUP': ('cmd', 0xF2),
                            'HK_TLM': ('tlm', 0xF0), 'STATUS_TLM': ('tlm', 0xF1)}
    s.NEW_APPS = tuple(dict.fromkeys((*s.NEW_APPS, 'MODE')))
    s.GLOBAL_MIDS = tuple(dict.fromkeys((*s.GLOBAL_MIDS, 0x187A, 0x187B, 0x187C)))
    s.CC['SFI'].update(ACK_FAULT=6, SET_APP_ENABLE=7, SET_WATCH_ENABLE=8, DUMP_FAULT_LOG=9,
                       SET_PEER_ENABLE=10, REPORT_LINK=12)
    s.CC['MODE'] = dict(NOOP=0, RESET_COUNTERS=1, SET_ENABLE=2, SET_MODE=3, SEND_STATUS=4,
                        SET_PEER_ENABLE=5, RESET_RESPONSE=6, APPLY_PROFILE=7)
    s.EID['SFI'].update(LIMIT_ERR=23, EVENT_RATE_ERR=24, ACK_INF=25, CONTROL_INF=26, CONTROL_ERR=27,
                        DUMP_INF=28, DUMP_ERR=29, NODE_LOSS_ERR=30, NODE_RESTORED_INF=31,
                        LINK_ERR=32, LINK_CLEAR_INF=33)
    s.EID['MODE'] = {**s.COMMON_EIDS, 'SUB_ERR': 11, 'TRANSITION_INF': 12, 'CONTROL_INF': 13,
                     'MODE_ERR': 14, 'RESPONSE_INF': 15, 'ACTION_ERR': 16, 'RETRY_ERR': 17,
                     'PEER_LOSS_ERR': 18, 'PEER_RESTORED_INF': 19, 'STATUS_INF': 20,
                     'FAULT_ERR': 21, 'PEER_ERR': 22}
    old = s.HK_LAYOUTS['SFI']
    s.HK_LAYOUTS['SFI'] = s.Layout('<8B4H11I2H4B2I', old.fields + (
        'LimitFaults', 'EventRateFaults', 'LinkFaults', 'NodeLossFaults', 'FaultReports', 'FaultLogDumps',
        'LargestAppsMask', 'LargestWatchesMask', 'ActiveFaults', 'LatchedFaults', 'PeerArmed', 'PeerLost',
        'PeerLastSeconds', 'HealthSequence'))
    s.HK_LAYOUTS['MODE'] = s.Layout('<8B10I', (
        'CommandCounter', 'CommandErrorCounter', 'Mode', 'Enabled', 'ActiveFaults', 'ExhaustedFaults',
        'PeerMode', 'PeerLost', 'Transitions', 'ResponsesSent', 'ActionsSent', 'ActionErrors', 'RestartsSent',
        'PeerLosses', 'ModeSinceSeconds', 'HealthySinceSeconds', 'PeerLastSeconds', 'PeerSequence'))
    s.TABLES['SFI'] = ('sfi_tbl.tbl', 'SFI.SfiTable', SFI_TABLE_SIZE)
    s.TABLES['MODE'] = ('mode_tbl.tbl', 'MODE.ModeTable', MODE_TABLE_SIZE)

    s.COMPONENTS['mode'] = ('apps/mode', 'MODE_')
    s.STARTUP_LINES['mode'] = ('CFE_APP', 'mode', 'MODE_AppMain', 'MODE', '70', '131072', '0x0', '0')
    for app in s.NEW_APPS:
        for name, (kind, topic) in s.APP_TOPICS[app].items():
            if kind == 'tlm':
                s.CFE_TOPICS_TLM[f'{app}_{name}_MID'] = topic
    s.SFI_DEFAULT_WATCH_ROWS = tuple(row for row in s.SFI_DEFAULT_WATCH_ROWS if row[0] != 0xEF) + ((0xEF, 16, 4, 'LIMIT', 0),)
