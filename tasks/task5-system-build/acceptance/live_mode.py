"""S7 live behavior through authenticated CMDI and passive TLMO observation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import struct
import time
from typing import TYPE_CHECKING

import spec
import tables as tb
from common import Fail, Problems
from live_common import boot, es_start_app, es_stop_app
from live_sfi import feed, ground_file, ground_capture, reports, status as sfi_status, wait_report
from node import Node
if TYPE_CHECKING:
    from check import Ctx


@dataclass(frozen=True)
class ResponseStatus:
    active: int
    attempts: int
    exhausted: int
    pending: int
    next_eligible: int
    last_sequence: int


@dataclass(frozen=True)
class ModeStatus:
    mode: int
    enabled: int
    peer_enabled: int
    peer_armed: int
    peer_mode: int
    peer_lost: int
    restart_slot: int
    restart_phase: int
    mode_since: int
    healthy_since: int
    peer_last: int
    profile: tuple[int, int, int, int]
    faults: tuple[ResponseStatus, ...]


def parse_status(packet: bytes) -> ModeStatus:
    if len(packet) != 16 + spec.MODE_STATUS_SIZE:
        raise Fail(f'MODE status length {len(packet)}, expected {16+spec.MODE_STATUS_SIZE}')
    prefix = struct.unpack_from('<8B3I', packet, 16)
    profile = struct.unpack_from('<4B',packet,36)
    faults = tuple(ResponseStatus(*struct.unpack_from('<4B2I',packet,40+i*12)) for i in range(57))
    if prefix[0] > 2 or prefix[6] not in (*range(57),255) or prefix[7] > 2:
        raise Fail('MODE status contains invalid mode/restart slot/phase')
    return ModeStatus(*prefix,profile,faults)


def status(n: Node) -> ModeStatus:
    got=n.request(lambda:n.cmd('MODE','SEND_STATUS',gap=0),spec.app_mid('MODE','STATUS_TLM',n.id))
    return parse_status(got[-1].data)


def control(n: Node, name: str, value: int) -> None:
    n.cmd('MODE',name,bytes((value,0,0,0)))


def wait_mode(n: Node, mode: int, timeout: float=5) -> dict[str,int]:
    end=time.monotonic()+timeout
    hk=n.hk('MODE')
    while hk['Mode']!=mode and time.monotonic()<end:
        time.sleep(0.2);hk=n.hk('MODE')
    if hk['Mode']!=mode:
        raise Fail(f'MODE expected mode{mode}, got {hk}')
    return hk


def target_profile(n: Node, mode: int) -> None:
    tlmo,stor=n.hk('TLMO'),n.hk('STOR')
    expected=((0,16,80),(1,8,65),(2,4,50))[mode]
    actual=(tlmo['SelectedProfile'],tlmo['BudgetPacketsPerCycle'],stor['PurgeThresholdPercent'])
    if actual!=expected:
        raise Fail(f'MODE mode{mode} target TLMO profile/budget/STOR purge {actual}, expected{expected}')


def c_fault_response(ctx: 'Ctx') -> None:
    from live_gw import add_conn, conn_status, free_port, udp_conn
    n=boot(ctx,(1,))[1]
    p=Problems()
    # Extra configured slots prove SAFE's mask disables slots above1.
    for slot in (2,3):
        add_conn(n,slot,udp_conn(free_port(),free_port()))
    for mode in (0,1,2,0):
        control(n,'SET_MODE',mode);wait_mode(n,mode);target_profile(n,mode)
        configured=conn_status(n)
        p.check(all(configured[i]['Enabled']==(0 if mode==2 else 1) for i in (2,3)),
                f'profile{mode} did not apply configured GW mask to slots2/3')
    control(n,'SET_ENABLE',1)
    # Reset only counters after the initial and explicit profile applications finish.
    time.sleep(1.2);n.cmd('MODE','RESET_COUNTERS')
    mark=n.g.mark();es_stop_app(n,'GW')
    onset=wait_report(n,mark,3,1,7)
    hk=wait_mode(n,1,4)
    p.check(hk['ResponsesSent']==1,'stopped GW must dispatch one automatic response')
    start=n.wait_event(mark,'CFE_ES',spec.CFE_ES_START_INF_EID,10,text='GW')
    p.check(bool(start),'MODE did not automatically START GW')
    clear=wait_report(n,mark,3,0,6)
    hk=n.hk('MODE');st=status(n)
    p.check(hk['RestartsSent']==1 and st.faults[3].attempts==1 and st.faults[3].active==0,
            'restart counters/status do not show one successful recovered attempt')
    p.check((clear.onset_secs,clear.onset_subsecs)==(onset.onset_secs,onset.onset_subsecs),'SFI restart clear changed onset identity')
    # The transition packet/event timestamps carry the recovery timing evidence.
    wait_mode(n,0,26)
    transitions=n.events(mark,'MODE',spec.EID['MODE']['TRANSITION_INF'])
    clear_time=clear.change_secs+clear.change_subsecs/2**32
    # Event telemetry supplies the transition instant without an HK-period delay.
    recovery_event=transitions[-1] if len(transitions)>=2 else None
    recovered=(recovery_event.seconds+recovery_event.subseconds/65536) if recovery_event else 0
    p.check(recovery_event is not None and 20-1/65536<=recovered-clear_time<=25,
            'quiet recovery must occur[20,25]s after SFI clear')
    hk=n.hk('MODE')
    p.check(hk['RestartsSent']==1 and hk['ResponsesSent']==1 and hk['ActionErrors']==0,'unexpected retry or action error during recovery')
    target_profile(n,0);p.raise_if()


def c_cooldown_cap(ctx: 'Ctx') -> None:
    n=boot(ctx,(1,))[1]
    p=Problems();control(n,'SET_ENABLE',1)
    n.cmd('MODE','RESET_COUNTERS');marks=[];attempt_deadlines=[]
    mark=n.g.mark()
    for attempt in range(1,4):
        recurrence=n.g.mark();es_stop_app(n,'GW');wait_report(n,recurrence,3,1,7)
        if not n.wait_event(recurrence,'CFE_ES',spec.CFE_ES_START_INF_EID,36,text='GW'):
            raise Fail(f'automatic restart attempt{attempt} did not START within cooldown')
        wait_report(n,recurrence,3,0,6)
        st=status(n);attempt_deadlines.append(st.faults[3].next_eligible)
        p.check(st.faults[3].attempts==attempt and not st.faults[3].exhausted,
                f'successful attempt{attempt} consumed wrong count or exhausted while inactive')
        if attempt>1:
            p.check(attempt_deadlines[-1]-attempt_deadlines[-2]>=30,'attempt starts were less than30 onboard seconds apart')
    recurrence=n.g.mark();es_stop_app(n,'GW');wait_report(n,recurrence,3,1,7)
    wait_mode(n,2,36)
    st=status(n);hk=n.hk('MODE')
    p.check(st.faults[3].attempts==3 and st.faults[3].exhausted==1 and hk['RestartsSent']==3,
            'fourth recurrence must exhaust without fourth START')
    p.check(len(n.events(mark,'MODE',spec.EID['MODE']['RETRY_ERR']))==1,'RETRY_ERR must occur once')
    es_start_app(n,'GW','GW_AppMain','/cf/gw.so',70,131072)
    wait_report(n,recurrence,3,0,6)
    n.cmd('MODE','RESET_COUNTERS');n.cmd('SFI','ACK_FAULT',bytes((3,0,0,0)))
    p.check(status(n).faults[3].exhausted==1,'counter reset and SFI ACK erased response cap')
    control(n,'SET_MODE',0)
    p.check(n.hk('MODE')['Mode']==2,'SAFE exit bypassed exhausted slot')
    control(n,'RESET_RESPONSE',3);control(n,'SET_MODE',0);wait_mode(n,0)
    p.check(status(n).faults[3].attempts==0,'inactive response reset did not free cap')
    p.raise_if()


def c_response_actions(ctx: 'Ctx') -> None:
    from live_gw import setup_dual_links, conn_snapshot
    from live_stor import add_payload, file_info, parse_stats
    payload=bytes((index*31+7)&255 for index in range(1536))
    nodes=boot(ctx,(1,2),seed={1:{'mode_response.bin':payload}},quiet_stor=True)
    links=setup_dual_links(ctx,nodes)
    n=nodes[1];p=Problems()
    n.load_table('STOR',tb.StorTable(entries=((0x0801,10,1),)).pack())
    n.cmd('STOR','ADD_FILE',add_payload('/cf/mode_response.bin','mode/response.bin'))
    # Use the native table to select each action on an ordinary LINK fault.
    responses=list(tb.ModeTable().responses)
    responses[5]=replace(responses[5],restart=0,set_priority=1,transfer=1,switch_route=1)
    n.load_table('MODE',replace(tb.ModeTable(),enabled=0,responses=tuple(responses)).pack())
    time.sleep(2.2);n.cmd('MODE','RESET_COUNTERS');control(n,'SET_ENABLE',1)
    mark=n.g.mark();n.cmd('SFI','REPORT_LINK',bytes((2,1,0,0)))
    wait_report(n,mark,50,1)
    end=time.monotonic()+5
    while n.hk('MODE')['ResponsesSent']<1 and time.monotonic()<end:time.sleep(0.2)
    p.check(bool(n.wait_event(mark,'STOR',spec.EID['STOR']['XFER_DONE_INF'],8)), 'response TRANSFER did not complete')
    p.check(ground_capture(n,mark,'mode/response.bin')==payload,'configured transfer fixture differs at ground')
    # The configured response itself must also produce a successful transfer.
    p.check(bool(n.events(mark,'STOR',spec.EID['STOR']['XFER_DONE_INF'])),'response did not issue fixture TRANSFER KEEP')
    got=n.request(lambda:n.cmd('STOR','SEND_STATS',gap=0),spec.app_mid('STOR','STATS_TLM',n.id))
    stats=parse_stats(got[-1].data)
    p.check(0x0801 in stats and stats[0x0801].priority==200,'SET_PRIORITY action did not set EVS HK priority200')
    _conns,_selected,preference=conn_snapshot(n)
    p.check(preference[0]==1,'response did not prefer backup route0')
    # A second requesting slot shares the route; only its final clear releases it.
    n.cmd('SFI','REPORT_LINK',bytes((3,1,0,0)));wait_report(n,mark,51,1)
    time.sleep(1.3);n.cmd('SFI','REPORT_LINK',bytes((2,0,0,0)));wait_report(n,mark,50,0)
    p.check(conn_snapshot(n)[2][0]==1,'first of two requesting clears released shared preference')
    control(n,'SET_ENABLE',0)
    before=n.hk('MODE')['ActionsSent'];n.cmd('SFI','REPORT_LINK',bytes((3,0,0,0)));wait_report(n,mark,51,0)
    time.sleep(1.2)
    p.check(conn_snapshot(n)[2][0]==0 and n.hk('MODE')['ActionsSent']==before+1,
            'last requesting clear while disabled must send exactly one counted preference release')
    control(n,'SET_ENABLE',1);time.sleep(1.2)
    p.check(n.hk('MODE')['ActionsSent']==before+1,'cleanup release duplicated on re-enable')
    # Reconfigure the same action with an absent source; target rejection must be visible.
    control(n,'SET_ENABLE',0);control(n,'RESET_RESPONSE',50)
    responses[5]=replace(responses[5],path='mode/missing.bin')
    n.load_table('MODE',replace(tb.ModeTable(),enabled=0,responses=tuple(responses)).pack())
    time.sleep(2.2);control(n,'SET_ENABLE',1);mark=n.g.mark()
    n.cmd('SFI','REPORT_LINK',bytes((2,1,0,0)))
    p.check(bool(n.wait_event(mark,'STOR',spec.EID['STOR']['XFER_ERR'],5)), 'absent response source rejection was not visible in STOR')
    links.check();p.raise_if()
    restart_snapshot_live(ctx)


def c_peer_mode(ctx: 'Ctx') -> None:
    from live_gw import setup_dual_links
    nodes=boot(ctx,(1,2));links=setup_dual_links(ctx,nodes);p=Problems()
    time.sleep(3)
    for n in nodes.values():control(n,'SET_ENABLE',1)
    for n in nodes.values():
        st=status(n);p.check(st.peer_armed==1 and st.peer_lost==0,'valid routed MODE packets failed first-contact arming')
    marks={i:n.g.mark() for i,n in nodes.items()};cut=links.set_filter({spec.MODE_PEER_MID})
    for i,n in nodes.items():
        lost=n.wait_event(marks[i],'MODE',spec.EID['MODE']['PEER_LOSS_ERR'],13)
        p.check(bool(lost) and 9<=lost[0].t-cut<=12,'MODE peer loss outside[9,12]s from steady relay filter')
        p.check(n.hk('SFI')['PeerLost']==0 and sfi_status(n).active[56]==0,'MODE-only filter also lost SFI health')
        wait_mode(n,1)
    links.set_filter(set())
    for i,n in nodes.items():
        p.check(bool(n.wait_event(marks[i],'MODE',spec.EID['MODE']['PEER_RESTORED_INF'],5)),'peer mode contact did not restore')
    time.sleep(2)
    marks={i:n.g.mark() for i,n in nodes.items()};cut=links.set_filter({spec.SFI_HEALTH_MID})
    for i,n in nodes.items():
        wait_report(n,marks[i],56,1,13)
        p.check(status(n).peer_lost==0,'health-only filter must preserve MODE timer')
    links.set_filter(set())
    for i,n in nodes.items():wait_report(n,marks[i],56,0,5)
    # A primary failure with backup forwarding still demands DEGRADED via LINK.
    marks={i:n.g.mark() for i,n in nodes.items()};cut=links.primary.set_enabled(False)
    for i,n in nodes.items():
        wait_report(n,marks[i],48,1,9);wait_mode(n,1,3)
        p.check(sfi_status(n).active[56]==0 and status(n).peer_lost==0,'surviving backup failed health/mode continuity')
    links.primary.set_enabled(True)
    for i,n in nodes.items():wait_report(n,marks[i],48,0,6)
    time.sleep(12)
    for n in nodes.values():wait_mode(n,0,26);target_profile(n,0)
    links.check();p.raise_if()


def stage_mode_table(n: Node, table: tb.ModeTable) -> None:
    """Stage and validate a native image, leaving activation for WAIT_STOP."""
    n.table_loads+=1
    filename=f'acc{n.table_loads}.tbl'
    (n.cf/filename).write_bytes(tb.with_body(n.table_image('MODE'),'MODE',table.pack()))
    mark=n.g.mark()
    n.send(spec.tbl_cmd_mid(n.id),spec.CFE_TBL_LOAD_CC,spec.tbl_load_payload('/cf/'+filename))
    if not n.wait_event(mark,'CFE_TBL',spec.CFE_TBL_FILE_LOADED_INF_EID,4):
        raise Fail('snapshot fixture native table LOAD did not complete')
    mark=n.g.mark()
    n.send(spec.tbl_cmd_mid(n.id),spec.CFE_TBL_VALIDATE_CC,spec.tbl_validate_payload('MODE.ModeTable'))
    if not n.wait_event(mark,'CFE_TBL',spec.CFE_TBL_VALIDATION_INF_EID,5,text='MODE.ModeTable'):
        raise Fail('snapshot fixture native table VALIDATE did not complete')


def restart_snapshot_live(ctx: 'Ctx') -> None:
    """Activate staged parameters inside a real STOP/START response interval."""
    n=boot(ctx,(1,))[1]
    # Expose native ES commands through TLMO for independent payload comparison.
    n.cmd('TLMO','ADD_STREAM',struct.pack('<IB3x',spec.es_cmd_mid(n.id),0))
    n.cmd('SFI','SET_APP_ENABLE',bytes((3,0,0,0)))
    responses=list(tb.ModeTable().responses)
    responses[5]=replace(responses[5],restart=1,switch_route=0)
    old=replace(tb.ModeTable(),enabled=0,responses=tuple(responses))
    n.load_table('MODE',old.pack());time.sleep(2.2)
    restarts=list(old.restarts)
    restarts[3]=('GW','GW_AppMain','/cf/gw.so',65536,71)
    new=replace(old,enabled=1,restarts=tuple(restarts))
    stage_mode_table(n,new)
    control(n,'SET_ENABLE',1)
    mark=n.g.mark();n.cmd('SFI','REPORT_LINK',bytes((2,1,0,0)))
    if not n.wait_event(mark,'MODE',spec.EID['MODE']['RESPONSE_INF'],4):
        raise Fail('snapshot response did not dispatch')
    before=status(n)
    if before.restart_phase!=1 or before.restart_slot!=50:
        raise Fail(f'snapshot activation must occur in WAIT_STOP slot50; observed {before.restart_phase}/{before.restart_slot}')
    n.send(spec.tbl_cmd_mid(n.id),spec.CFE_TBL_ACTIVATE_CC,spec.tbl_activate_payload('MODE.ModeTable'))
    if not n.wait_event(mark,'CFE_TBL',spec.CFE_TBL_UPDATE_SUCCESS_INF_EID,5,text='MODE.ModeTable'):
        raise Fail('snapshot table activation did not complete')
    def start_payload(from_mark: int, expected_stack: int, expected_priority: int) -> None:
        def captured() -> list[bytes]:
            return [d.data[8:] for d in n.g.since(from_mark,spec.es_cmd_mid(n.id))
                    if len(d.data)==128 and d.data[6]==spec.CFE_ES_START_APP_CC]
        if not n.g.wait(lambda:bool(captured()),9):
            raise Fail('snapshot restart did not expose native ES START command')
        values=struct.unpack('<20s20s64sQBxH4x',captured()[0])
        expected=(b'GW',b'GW_AppMain',b'/cf/gw.so',expected_stack,0,expected_priority)
        actual=tuple(v.split(b'\0',1)[0] if isinstance(v,bytes) else v for v in values)
        if actual!=expected:
            raise Fail(f'ES START snapshot {actual}, expected {expected}')
    start_payload(mark,131072,70)
    if status(n).faults[50].next_eligible!=before.faults[50].next_eligible:
        raise Fail('table activation changed existing response cooldown')
    n.cmd('SFI','REPORT_LINK',bytes((2,0,0,0)));wait_report(n,mark,50,0)
    eligibility=before.faults[50].next_eligible
    end=time.monotonic()+34
    while n.onboard_seconds()<eligibility and time.monotonic()<end:
        time.sleep(0.3)
    mark=n.g.mark();n.cmd('SFI','REPORT_LINK',bytes((2,1,0,0)))
    start_payload(mark,65536,71)
    n.cmd('SFI','REPORT_LINK',bytes((2,0,0,0)))
    n.cmd('SFI','SET_APP_ENABLE',bytes((3,1,0,0)))
