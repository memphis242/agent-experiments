"""Helpers shared by the live checks: sessions of one or two nodes, SAMPLE_APP counters, ES app control."""
from __future__ import annotations

import struct
import time
from typing import TYPE_CHECKING

import frames as fr
import spec
import tables as tb
from common import Fail
from node import Node, console_events

if TYPE_CHECKING:
    from check import Ctx


def boot(ctx: "Ctx", nodes: tuple[int, ...] = (1,), *, seed: dict[int, dict[str, bytes]] | None = None,
         startup_edit=None, quiet_stor: bool = False, isolate_mode: bool = True) -> dict[int, Node]:  # noqa: ANN001
    """Start fresh scratch copies of the given nodes. `quiet_stor` boots STOR with a table that has no entries,
    so that no packet file exists until a scenario table is loaded through cFE Table Services."""
    seeds = {n: dict((seed or {}).get(n, {})) for n in nodes}
    if quiet_stor:
        for n in nodes:
            img = ctx.pristine(n).get("STOR")
            if img is None:
                raise Fail(f"the build does not install /cf/stor_tbl.tbl on cpu{n}")
            seeds[n]["stor_tbl.tbl"] = tb.with_body(img, "STOR", tb.StorTable().pack())
    started = ctx.session(nodes, seed=seeds, startup_edit=startup_edit)
    for n in started.values():
        if isolate_mode:
            n.cmd("MODE", "SET_ENABLE", struct.pack("<B3x", 0))
            wait_hk_field(n, "MODE", lambda hk: hk["Enabled"] == 0, 3,
                          "MODE SET_ENABLE 0 must be visible before isolated setup")
        # Target INIT events occur after registration. Wait without issuing
        # large STOR requests, which temporarily change the output budget.
        def targets_registered() -> bool:
            initialized = {app for app, eid, _text in console_events(n.console_log()) if eid == 1}
            return {"TLMO", "STOR", "GW"} <= initialized
        if not n.g.wait(targets_registered, 5):
            raise Fail(f"{n.cpu} initial MODE profile barrier: expected registered TLMO/STOR/GW INIT events; observed incomplete startup")
    time.sleep(2.2)
    for n in started.values():
        hk = n.hk("MODE")
        if hk.get("ActionErrors", 0):
            raise Fail(f"{n.cpu} initial MODE profile: expected no action errors after target registration; observed {hk}")
    return started


def sample_counters(node: Node) -> tuple[int, int]:
    """SAMPLE_APP (CommandCounter, CommandErrorCounter) after a framed SAMPLE_APP SEND_HK."""
    node.ensure_command_path()
    mid = spec.sample_hk_mid(node.id)
    got = node.request(lambda: node.send(spec.sample_send_hk_mid(node.id), 0), mid)
    if not got:
        raise Fail(f"{node.cpu}: no SAMPLE_APP housekeeping followed a framed SAMPLE_APP SEND_HK")
    raw = got[-1].data
    return raw[16], raw[17]


def sample_noop_frame(node: Node, **opts: object) -> bytes:
    return node.frame(fr.build_command(spec.sample_cmd_mid(node.id), spec.SAMPLE_NOOP_CC), **opts)  # type: ignore[arg-type]


def es_stop_app(node: Node, app: str, timeout: float = 8.0) -> None:
    mark = node.g.mark()
    node.send(spec.es_cmd_mid(node.id), spec.CFE_ES_STOP_APP_CC, spec.es_app_name_payload(app))
    if not node.wait_event(mark, "CFE_ES", spec.CFE_ES_STOP_INF_EID, timeout, text=app):
        raise Fail(f"{node.cpu}: ES STOP_APP {app} produced no CFE_ES_STOP_INF_EID naming it within {timeout:.0f} s")


def es_start_app(node: Node, app: str, entry: str, file: str, priority: int = 70, stack: int = 131072,
                 timeout: float = 8.0) -> None:
    mark = node.g.mark()
    node.send(spec.es_cmd_mid(node.id), spec.CFE_ES_START_APP_CC, spec.es_start_app_payload(app, entry, file, priority, stack))
    if not node.wait_event(mark, "CFE_ES", spec.CFE_ES_START_INF_EID, timeout, text=app):
        evs = [f"{e.app} {e.eid}: {e.text}" for e in node.events(mark, "CFE_ES")]
        raise Fail(f"{node.cpu}: ES START_APP {app} ({file}, {entry}) produced no CFE_ES_START_INF_EID: {evs[:3]}")


def wait_hk_field(node: Node, app: str, pred, timeout: float, what: str) -> dict[str, int]:  # noqa: ANN001
    end = time.monotonic() + timeout
    hk = node.hk(app)
    while not pred(hk) and time.monotonic() < end:
        time.sleep(0.3)
        hk = node.hk(app)
    if not pred(hk):
        raise Fail(f"{node.cpu}: {what} (housekeeping {hk})")
    return hk


def u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]
