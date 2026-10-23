"""Live nodes: start a scratch copy of an installed processor with tools/run_cfs.sh, command it only with
CMDI frames on its CMDI port, and observe it only through TLMO datagrams on its TLMO port.

Standard library only. Every wait is bounded; every failure raises Fail (or PrereqFail for the
conditions many checks share) with a detail for the experimenter.
"""
from __future__ import annotations

import os
import json
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import frames as fr
import lifecycle
import spec
import tables as tb
from common import Fail, PrereqFail

BOOT_TIMEOUT_S = 40
FIRST_TLM_S = 15.0
CMD_PATH_S = 8.0
SETTLE_S = 0.35
HK_WAIT_S = 2.0


# ----------------------------------------------------------------------------- telemetry receiver

@dataclass(frozen=True)
class Datagram:
    t: float          # host monotonic arrival time
    mid: int          # -1 for datagrams shorter than a primary header
    data: bytes


@dataclass(frozen=True)
class Event:
    t: float
    app: str
    eid: int
    etype: int
    text: str
    seconds: int = 0
    subseconds: int = 0


EVENT_TYPES = {1: "DEBUG", 2: "INFORMATION", 3: "ERROR", 4: "CRITICAL"}


def parse_long_event(pkt: bytes, t: float = 0.0) -> Event | None:
    """CFE_EVS_LongEventTlm_t: AppName at 16, EventID at 36, EventType at 38, Message at 48 (122 bytes)."""
    if len(pkt) < 50:
        return None
    app = pkt[16:36].split(b"\0", 1)[0].decode("latin-1")
    eid, etype = struct.unpack_from("<HH", pkt, 36)
    text = pkt[48:170].split(b"\0", 1)[0].decode("latin-1")
    return Event(t, app, eid, etype, text, fr.tlm_seconds(pkt), struct.unpack_from(">H", pkt, 10)[0])


class Ground(threading.Thread):
    """Collects every datagram arriving on one TLMO port, in arrival order."""

    def __init__(self, port: int, events_mid: int) -> None:
        super().__init__(daemon=True, name=f"ground-{port}")
        self.port = port
        self.events_mid = events_mid
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        try:
            self.sock.bind((spec.HOST, port))
        except OSError as exc:
            self.sock.close()
            raise PrereqFail(f"the review cannot listen on UDP {spec.HOST}:{port}: {exc} (a node from an earlier "
                             f"scenario may still be running)") from exc
        self.sock.settimeout(0.1)
        self.lock = threading.Lock()
        self.log: list[Datagram] = []
        self.last_secs: tuple[int, float] | None = None
        self.listeners: list[Callable[[Datagram], None]] = []
        self.listener_errors: list[str] = []
        self.running = True
        self.start()

    def run(self) -> None:
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            now = time.monotonic()
            mid = struct.unpack(">H", data[:2])[0] if len(data) >= 6 else -1
            d = Datagram(now, mid, data)
            with self.lock:
                self.log.append(d)
                if len(data) >= 10 and not (data[0] & 0x10):
                    self.last_secs = (fr.tlm_seconds(data), now)
                listeners = list(self.listeners)
            for fn in listeners:
                try:
                    fn(d)
                except Exception as exc:  # noqa: BLE001 - surface collector failures to the check
                    with self.lock:
                        self.listener_errors.append(f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.running = False
        self.join(timeout=2)
        self.sock.close()

    def mark(self) -> int:
        with self.lock:
            return len(self.log)

    def since(self, mark: int = 0, mid: int | None = None) -> list[Datagram]:
        with self.lock:
            return [d for d in self.log[mark:] if mid is None or d.mid == mid]

    def wait(self, pred: Callable[[], bool], timeout: float, poll: float = 0.05) -> bool:
        end = time.monotonic() + timeout
        while True:
            if pred():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(poll)

    def wait_mid(self, mark: int, mid: int, timeout: float, count: int = 1) -> list[Datagram]:
        self.wait(lambda: len(self.since(mark, mid)) >= count, timeout)
        return self.since(mark, mid)

    def events(self, mark: int = 0, app: str | None = None, eid: int | None = None) -> list[Event]:
        out = []
        for d in self.since(mark, self.events_mid):
            ev = parse_long_event(d.data, d.t)
            if ev is not None and (app is None or ev.app == app) and (eid is None or ev.eid == eid):
                out.append(ev)
        return out

    def onboard_seconds(self) -> int | None:
        """Onboard seconds now: the latest telemetry seconds field plus the host time elapsed since it arrived."""
        with self.lock:
            last = self.last_secs
        if last is None:
            return None
        return last[0] + int(time.monotonic() - last[1])


# ----------------------------------------------------------------------------- one node

def find_node_pids(exe_dir: Path, cpu: int) -> list[int]:
    """core-cpuN processes whose working directory is `exe_dir`."""
    out = []
    want = str(exe_dir.resolve())
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / "comm").read_text().strip() != f"core-cpu{cpu}":
                continue
            if os.readlink(p / "cwd") == want:
                out.append(int(p.name))
        except OSError:
            continue
    return out


class Node:
    """A scratch copy of one installed processor, started with the agent's tools/run_cfs.sh."""

    def __init__(self, tree: Path, root: Path, node_id: int, pristine: dict[str, bytes]) -> None:
        assert node_id in spec.NODES
        self.tree = tree
        self.root = root
        self.id = node_id
        self.cpu = f"cpu{node_id}"
        self.exe = root / "build-native_std" / "exe" / self.cpu
        self.cf = self.exe / "cf"
        self.run_dir = root / "run"
        self.pristine = pristine
        self.ground: Ground | None = None
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.serials = fr.SERIALS
        self.last_burst = float("-inf")
        self.large_request_lock = threading.Lock()
        self.last_large_reply = float("-inf")
        self.large_packet_lengths: dict[int, int] = {}
        self.tlmo_budget = 16
        self.tlmo_profiles = tb.TlmoTable().profiles
        self.last_send = 0.0
        self.command_path_ok = False
        self.table_loads = 0
        self.key = fr.DEFAULT_KEY
        self.started = False
        self.owned_pids: set[int] = set()
        self.psp_baseline: tuple[lifecycle.Segment, ...] | None = None
        self.sb_baseline: tuple[int, int, int] | None = None
        self.observation_log_start = 0
        self.observation_mark = 0

    # -- lifecycle -------------------------------------------------------------
    def env(self) -> dict[str, str]:
        return dict(os.environ, CFS_CPU=self.cpu, CFS_RUN_DIR=str(self.run_dir), CFS_TIMEOUT=str(BOOT_TIMEOUT_S))

    def prepare(self, startup_edit: Callable[[str], str] | None = None, seed: dict[str, bytes] | None = None) -> None:
        if self.started or self.psp_baseline is not None:
            raise Fail(f"{self.cpu} preparation requires complete owned-node and PSP cleanup")
        if self.root.exists():
            shutil.rmtree(self.root)
        src = self.tree / "build-native_std" / "exe" / self.cpu
        if not (src / f"core-{self.cpu}").is_file():
            raise PrereqFail(f"build-native_std/exe/{self.cpu}/core-{self.cpu} was not installed by the build")
        shutil.copytree(self.tree / "tools", self.root / "tools", symlinks=True)
        shutil.copytree(src, self.exe, symlinks=True)
        self.run_dir.mkdir(parents=True)
        if startup_edit is not None:
            scr = self.cf / "cfe_es_startup.scr"
            scr.write_text(startup_edit(scr.read_text(errors="replace")))
        for rel, data in (seed or {}).items():
            dest = self.cf / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    def start(self) -> None:
        """Run tools/run_cfs.sh for this processor and wait for the first TLMO datagram (no enable step)."""
        self.owned_pids.clear()
        self.psp_baseline = lifecycle.segments()
        self.sb_baseline = None
        self.ground = Ground(spec.tlmo_port(self.id), spec.evs_long_mid(self.id))
        script = self.root / "tools" / "run_cfs.sh"
        try:
            proc = subprocess.run(["bash", str(script)], env=self.env(), capture_output=True, text=True,
                                  errors="replace", timeout=BOOT_TIMEOUT_S + 20, check=False)
        except subprocess.TimeoutExpired:
            self.stop()
            raise PrereqFail(f"CFS_CPU={self.cpu} tools/run_cfs.sh did not return within {BOOT_TIMEOUT_S + 20} s")
        self.started = True
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-700:]
            self.stop()
            raise PrereqFail(f"CFS_CPU={self.cpu} tools/run_cfs.sh exited {proc.returncode}: {tail}")
        self.owned_pids.update(find_node_pids(self.exe, self.id))
        if not self.owned_pids:
            self.stop()
            raise PrereqFail(f"CFS_CPU={self.cpu} tools/run_cfs.sh returned success but no core-{self.cpu} is running "
                             f"from build-native_std/exe/{self.cpu}")
        assert self.ground is not None
        if not self.ground.wait(lambda: self.ground is not None and self.ground.last_secs is not None, FIRST_TLM_S):
            n = len(self.ground.since())
            tail = self.log_tail()
            self.stop()
            raise PrereqFail(f"{self.cpu}: no telemetry packet arrived on UDP {spec.tlmo_port(self.id)} within "
                             f"{FIRST_TLM_S:.0f} s of OPERATIONAL without any enable command ({n} datagrams of any "
                             f"kind); log tail: {tail}")

    def log_tail(self, n: int = 1200) -> str:
        texts = []
        for f in sorted(self.run_dir.glob("**/*.log")) if self.run_dir.is_dir() else []:
            try:
                texts.append(f.read_text(errors="replace")[-n:])
            except OSError:
                pass
        return " | ".join(texts)[-n:]

    def console_log(self) -> str:
        out = []
        for f in sorted(self.run_dir.glob("**/*.log")) if self.run_dir.is_dir() else []:
            try:
                out.append(f.read_text(errors="replace"))
            except OSError:
                pass
        return "\n".join(out)

    def stop(self) -> None:
        try:
            self.owned_pids.update(find_node_pids(self.exe, self.id))
            if self.started:
                script = self.root / "tools" / "stop_cfs.sh"
                if script.is_file():
                    try:
                        subprocess.run(["bash", str(script)], env=self.env(), capture_output=True, timeout=30, check=False)
                    except subprocess.TimeoutExpired:
                        pass
            for pid in find_node_pids(self.exe, self.id):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            end = time.monotonic() + 5
            while find_node_pids(self.exe, self.id) and time.monotonic() < end:
                time.sleep(0.05)
            if find_node_pids(self.exe, self.id):
                raise Fail(f"{self.cpu} cleanup: owned node remains alive; PSP objects retained")
            if self.psp_baseline is not None:
                lifecycle.retire_psp(self.exe, self.psp_baseline, frozenset(self.owned_pids))
                self.psp_baseline = None
        finally:
            if self.ground is not None:
                self.ground.close()
                self.ground = None
            self.started = False

    def alive(self) -> bool:
        return bool(find_node_pids(self.exe, self.id))

    def pids(self) -> list[int]:
        return find_node_pids(self.exe, self.id)

    @property
    def g(self) -> Ground:
        if self.ground is None:
            raise Fail(f"{self.cpu} is not running")
        return self.ground

    # -- commanding --------------------------------------------------------------
    def onboard_seconds(self, timeout: float = 5.0) -> int:
        g = self.g
        if not g.wait(lambda: g.onboard_seconds() is not None, timeout):
            raise Fail(f"{self.cpu}: no telemetry to read the onboard time from")
        secs = g.onboard_seconds()
        assert secs is not None
        return secs

    def frame(self, command: bytes, *, key: bytes | None = None, source_id: int = fr.OBSERVATION_SOURCE,
              time_offset: int = 0, sequence: int | None = None, **header: int) -> bytes:
        if sequence is None:
            sequence = self.serials.next(self.id, source_id)
            self.seq = sequence
        secs = max(0, self.onboard_seconds() + time_offset)
        return fr.build_frame(command, time_seconds=secs, key=self.key if key is None else key,
                              source_id=source_id, sequence=sequence, **header)

    def send_raw(self, data: bytes, port: int | None = None) -> None:
        self.tx.sendto(data, (spec.HOST, spec.cmdi_port(self.id) if port is None else port))
        self.last_send = time.monotonic()

    def send_batch(self, frames: tuple[bytes, ...]) -> None:
        """Queue a bounded authenticated batch before allowing the node to wake.

        Only processes belonging to this scratch node are suspended. The host
        watchdog remains running, and resume is guaranteed even on send failure.
        """
        assert 1 <= len(frames) <= 8
        assert all(len(frame) <= fr.MAX_FRAME for frame in frames)
        pids = tuple(self.pids())
        if not pids:
            raise Fail(f"{self.cpu} pre-wakeup batch: expected own running node; observed no node process")
        stopped = []
        try:
            for pid in pids:
                os.kill(pid, signal.SIGSTOP)
                stopped.append(pid)
                deadline = time.monotonic() + 0.5
                while True:
                    state = Path(f"/proc/{pid}/status").read_text()
                    if re.search(r"(?m)^State:\s+[Tt]", state):
                        break
                    if time.monotonic() >= deadline:
                        raise Fail(f"{self.cpu} pre-wakeup batch: expected pid {pid} stopped; observed running after 0.5 s")
                    time.sleep(0.001)
            for frame in frames:
                self.send_raw(frame)
        finally:
            for pid in stopped:
                os.kill(pid, signal.SIGCONT)

    def send(self, mid: int, cc: int, payload: bytes = b"", *, corrupt: str | None = None, **opts: object) -> bytes:
        f = self.frame(fr.build_command(mid, cc, payload), **opts)  # type: ignore[arg-type]
        if corrupt is not None:
            f = fr.corrupt(f, corrupt)
        self.send_raw(f)
        return f

    def cmd(self, app: str, name: str, payload: bytes = b"", *, gap: float = 0.12, **opts: object) -> bytes:
        f = self.send(spec.app_mid(app, "CMD", self.id), spec.CC[app][name], payload, **opts)
        if app == "TLMO" and name == "SET_BUDGET" and len(payload) == 4:
            self.tlmo_budget = struct.unpack_from("<H", payload)[0]
        if app == "TLMO" and name == "SELECT_PROFILE" and len(payload) == 4 and payload[0] < 3:
            self.tlmo_budget = self.tlmo_profiles[payload[0]][0]
        time.sleep(gap)
        return f

    def settle(self, seconds: float = SETTLE_S) -> None:
        left = seconds - (time.monotonic() - self.last_send)
        if left > 0:
            time.sleep(left)

    def ensure_command_path(self) -> None:
        """A framed CMDI SEND_HK must produce CMDI housekeeping; without it no commanded check can run."""
        if self.command_path_ok:
            return
        g = self.g
        mid = spec.app_mid("CMDI", "HK_TLM", self.id)
        end = time.monotonic() + CMD_PATH_S
        while time.monotonic() < end:
            mark = g.mark()
            self.send(spec.app_mid("CMDI", "SEND_HK", self.id), 0)
            if g.wait_mid(mark, mid, 1.5):
                self.command_path_ok = True
                return
        raise PrereqFail(f"{self.cpu}: a valid CMDI frame carrying CMDI SEND_HK sent to UDP {spec.cmdi_port(self.id)} "
                         f"produced no CMDI housekeeping (MID {mid:#06x}) on UDP {spec.tlmo_port(self.id)} within "
                         f"{CMD_PATH_S:.0f} s")

    # -- telemetry requests ---------------------------------------------------------
    def request(self, send: Callable[[], object], reply_mid: int, timeout: float = HK_WAIT_S,
                tail: float = 0.25, count: int = 1, *, held_snapshot: bool = False) -> list[Datagram]:
        g = self.g
        self.settle()
        large = reply_mid in self.large_mids()
        with self.large_request_lock:
            previous_budget = self.tlmo_budget
            if large:
                time.sleep(max(0, 0.6 - (time.monotonic() - self.last_large_reply)))
                if not held_snapshot:
                    self.cmd("TLMO", "SET_BUDGET", struct.pack("<HH", 256, 0))
            mark = g.mark()
            try:
                send()
                if held_snapshot:
                    self.cmd("TLMO", "SET_BUDGET", struct.pack("<HH", 256, 0), gap=0)
                got = g.wait_mid(mark, reply_mid, min(timeout, 2.0), count)
                if not got:
                    raise Fail(f"{self.cpu} request MID {reply_mid:#06x}: expected {count} reply within 2 s "
                               "at sufficient nonzero output budget; observed no reply")
                if large:
                    length = len(got[0].data)
                    expected = self.large_mids()[reply_mid]
                    if length != expected or length > 8192:
                        raise Fail(f"{self.cpu} large request {reply_mid:#06x}: expected {expected} bytes <=8192; "
                                   f"observed {length}")
                    self.large_packet_lengths[reply_mid] = length
                    self.last_large_reply = time.monotonic()
                time.sleep(tail)
                return g.since(mark, reply_mid)
            finally:
                if large and not held_snapshot and previous_budget != self.tlmo_budget:
                    self.cmd("TLMO", "SET_BUDGET", struct.pack("<HH", previous_budget, 0))

    def large_mids(self) -> dict[int, int]:
        return {
            spec.app_mid("STOR", "HK_TLM", self.id): spec.TLM_HDR + spec.HK_LAYOUTS["STOR"].size,
            spec.app_mid("SFI", "STATUS_TLM", self.id): spec.TLM_HDR + spec.SFI_STATUS_SIZE,
            spec.app_mid("TLMO", "STREAM_TLM", self.id): spec.TLM_HDR + spec.TLMO_STREAM_SIZE,
        }

    def hk(self, app: str) -> dict[str, int]:
        self.ensure_command_path()
        mid = spec.app_mid(app, "HK_TLM", self.id)
        send_hk = spec.app_mid(app, "SEND_HK", self.id)
        got = self.request(lambda: self.send(send_hk, 0), mid)
        if not got:
            if not self.alive():
                raise Fail(f"{self.cpu} stopped running")
            raise Fail(f"{self.cpu}: no {app} housekeeping (MID {mid:#06x}) followed a framed SEND_HK")
        parsed = parse_hk(app, got[-1].data)
        if app == "TLMO":
            self.tlmo_budget = parsed["BudgetPacketsPerCycle"]
        return parsed

    def events(self, mark: int, app: str | None = None, eid: int | None = None) -> list[Event]:
        return self.g.events(mark, app, eid)

    def wait_event(self, mark: int, app: str, eid: int, timeout: float = 4.0, count: int = 1,
                   text: str | None = None) -> list[Event]:
        def got() -> list[Event]:
            return [e for e in self.events(mark, app, eid) if text is None or text.lower() in e.text.lower()]
        self.g.wait(lambda: len(got()) >= count, timeout)
        return got()

    # -- tables -----------------------------------------------------------------------
    def table_image(self, app: str) -> bytes:
        if app not in self.pristine:
            raise Fail(f"the build does not install /cf/{spec.TABLES[app][0]} on {self.cpu}")
        return self.pristine[app]

    def load_table(self, app: str, body: bytes, *, expect_valid: bool = True, timeout: float = 8.0,
                   confirm: Callable[[], bool] | None = None) -> None:
        """Load a table through cFE Table Services: LOAD, VALIDATE (inactive), ACTIVATE, all as CMDI frames.
        `confirm` decides completion when the update event cannot be observed (a table that changes which
        streams TLMO forwards may drop the event itself)."""
        self.ensure_command_path()
        name = spec.TABLES[app][1]
        activated_table = tb.TlmoTable.unpack(body) if app == "TLMO" else None
        image = tb.with_body(self.table_image(app), app, body)
        self.table_loads += 1
        fname = f"acc{self.table_loads}.tbl"
        (self.cf / fname).write_bytes(image)
        tbl = spec.tbl_cmd_mid(self.id)
        g = self.g
        mark = g.mark()
        self.send(tbl, spec.CFE_TBL_LOAD_CC, spec.tbl_load_payload(f"/cf/{fname}"))
        if not self.wait_event(mark, "CFE_TBL", spec.CFE_TBL_FILE_LOADED_INF_EID, 4.0):
            errs = [f"{e.eid}: {e.text}" for e in self.events(mark, "CFE_TBL")]
            raise Fail(f"{self.cpu}: cFE TBL LOAD of {name} from /cf/{fname} was not accepted; TBL events {errs}")
        mark = g.mark()
        self.send(tbl, spec.CFE_TBL_VALIDATE_CC, spec.tbl_validate_payload(name))
        end = time.monotonic() + timeout
        ok = bad = []
        while time.monotonic() < end:
            ok = [e for e in self.events(mark, "CFE_TBL") if e.eid in (spec.CFE_TBL_VALIDATION_INF_EID, 23)
                  and name in e.text]
            bad = [e for e in self.events(mark, "CFE_TBL") if e.eid == spec.CFE_TBL_VALIDATION_ERR_EID and name in e.text]
            if ok or bad:
                break
            time.sleep(0.1)
        if not expect_valid:
            if not bad:
                raise Fail(f"{self.cpu}: a {name} image that the specification requires to be rejected was not "
                           f"reported invalid by validation ({'accepted' if ok else 'no result'})")
            mark = g.mark()
            self.send(tbl, spec.CFE_TBL_ABORT_LOAD_CC, spec.tbl_activate_payload(name))
            if not self.wait_event(mark, "CFE_TBL", spec.CFE_TBL_LOAD_ABORT_INF_EID, timeout, text=name):
                raise Fail(f"{self.cpu}: native ABORT_LOAD of invalid {name} did not complete")
            return
        if bad or not ok:
            raise Fail(f"{self.cpu}: validation of {name} " + (f"failed: {bad[0].text}" if bad else
                       f"produced no result within {timeout:.0f} s"))
        mark = g.mark()
        self.send(tbl, spec.CFE_TBL_ACTIVATE_CC, spec.tbl_activate_payload(name))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if [e for e in self.events(mark, "CFE_TBL") if e.eid == spec.CFE_TBL_UPDATE_SUCCESS_INF_EID and name in e.text]:
                if activated_table is not None:
                    self.tlmo_budget, self.tlmo_profiles = activated_table.budget, activated_table.profiles
                return
            bad = [e for e in self.events(mark, "CFE_TBL") if e.eid == spec.CFE_TBL_UPDATE_ERR_EID]
            if bad:
                break
            time.sleep(0.1)
        if not bad and confirm is not None and confirm():
            if activated_table is not None:
                self.tlmo_budget, self.tlmo_profiles = activated_table.budget, activated_table.profiles
            return
        raise Fail(f"{self.cpu}: activation of {name} did not complete: " +
                   (bad[0].text if bad else f"no update event within {timeout:.0f} s"))

    # -- injection ----------------------------------------------------------------------
    def inject(self, packets: list[bytes], burst: int = 8, gap: float = 0.6, **opts: object) -> None:
        """Send each packet in its own frame, `burst` frames at a time, `gap` seconds apart."""
        assert 1 <= burst <= 8
        assert all(8 <= len(pkt) <= 1024 for pkt in packets), "live SB injection envelope is 8..1024 bytes"
        for i, pkt in enumerate(packets):
            if i % burst == 0:
                time.sleep(max(0.0, max(gap, 0.6) - (time.monotonic() - self.last_burst)))
                self.last_burst = time.monotonic()
            self.send_raw(self.frame(pkt, **opts))  # type: ignore[arg-type]
        time.sleep(max(gap, 0.6))

    def read_sb_counters(self) -> tuple[int, int, int]:
        """Request current native SB counters through the authenticated ground path."""
        sb_mid = spec.named_tlm_mid("CFE_SB_HK_TLM_MID", self.id)
        previous_budget = self.tlmo_budget
        self.cmd("TLMO", "SET_BUDGET", struct.pack("<HH", 256, 0))
        mapping = self.request(lambda: self.cmd("TLMO", "SEND_CLASS_MAP", gap=0),
                               spec.app_mid("TLMO", "CLASS_TLM", self.id))[-1].data
        if len(mapping) != spec.TLM_HDR + spec.TLMO_CLASS_SIZE:
            raise Fail(f"{self.cpu} final SB observation: expected full class map "
                       f"{spec.TLM_HDR + spec.TLMO_CLASS_SIZE} bytes; observed {len(mapping)}")
        mids = tuple(struct.unpack_from("<I", mapping, 20 + i * 8)[0] for i in range(64))
        appended = sb_mid not in mids
        try:
            if appended:
                self.cmd("TLMO", "ADD_STREAM", struct.pack("<IB3x", sb_mid, 1))
            reply = self.request(lambda: self.send(spec.cmd_mid(spec.CFE_SB_SEND_HK_TOPIC, self.id), 0), sb_mid)[-1].data
        finally:
            if appended:
                self.cmd("TLMO", "REMOVE_STREAM", struct.pack("<I", sb_mid))
            self.cmd("TLMO", "SET_BUDGET", struct.pack("<HH", previous_budget, 0))
        if len(reply) < 32:
            raise Fail(f"{self.cpu} SB housekeeping: expected at least32 bytes; observed {len(reply)}")
        return reply[19], *struct.unpack_from("<HH", reply, 28)

    def begin_observation(self) -> None:
        """Snapshot counters and allocator logs after the complete node startup."""
        self.sb_baseline = self.read_sb_counters()
        self.observation_log_start = len(self.console_log())
        self.observation_mark = self.g.mark()
        (self.run_dir / "acceptance-sb-baseline.json").write_text(
            json.dumps({"counters": self.sb_baseline, "console_offset": self.observation_log_start}) + "\n")

    def verify_observation_integrity(self) -> None:
        """Check collector health, fault mirrors, and measured SB counter deltas."""
        if self.g.listener_errors:
            raise Fail(f"{self.cpu} telemetry collector: expected successful listeners; observed {self.g.listener_errors}")
        if self.sb_baseline is None:
            raise Fail(f"{self.cpu} SB live envelope: missing post-startup baseline")
        current = self.read_sb_counters()
        delta = tuple((after - before) % modulus for before, after, modulus in
                      zip(self.sb_baseline, current, (256, 65536, 65536), strict=True))
        with (self.run_dir / "acceptance-sb-observations.jsonl").open("a") as stream:
            stream.write(json.dumps({"baseline": self.sb_baseline, "current": current,
                                     "send_pipe_message_limit_delta": delta}) + "\n")
        allocation_events = [event for event in self.g.events(self.observation_mark, "CFE_SB")
                             if event.eid in (16, 20)]
        if allocation_events:
            raise Fail(f"{self.cpu} SB live envelope: allocator error events after setup: "
                       f"{[(event.eid, event.text) for event in allocation_events]}")
        packets = self.g.since()
        raw_mid = spec.app_mid("SFI", "FAULT", self.id)
        mirror_mid = spec.app_mid("SFI", "FAULT_TLM", self.id)
        raw = {d.data[8:] for d in packets if d.mid == raw_mid}
        mirrored = {d.data[16:] for d in packets if d.mid == mirror_mid}
        if raw != mirrored:
            raise Fail(f"{self.cpu} SFI fault publication: expected byte-identical raw/mirror report payloads; "
                       f"observed {len(raw - mirrored)} missing mirrors and {len(mirrored - raw)} missing raw reports")
        log = self.console_log()[self.observation_log_start:]
        allocation_console = [(app, eid, text) for app, eid, text in console_events(log)
                              if app == "CFE_SB" and eid in (16, 20)]
        if allocation_console:
            raise Fail(f"{self.cpu} SB live envelope: native allocation failure: {allocation_console}")
        failure = re.search(r"(?im)^.*(?:GetPoolBuf.*(?:fail|error)|(?:SB|pool).*allocat.*fail|"
                            r"(?:SB|pool).*insufficient.*(?:memory|buffer)).*$", log)
        if failure:
            raise Fail(f"{self.cpu} SB live envelope: expected no allocation failure log; observed {failure.group(0)[:220]}")


# ----------------------------------------------------------------------------- telemetry parsing

def parse_hk(app: str, pkt: bytes) -> dict[str, int]:
    layout = spec.HK_LAYOUTS[app]
    if len(pkt) < spec.TLM_HDR + layout.size:
        raise Fail(f"{app} housekeeping packet is {len(pkt)} bytes; the specified payload needs "
                   f"{spec.TLM_HDR + layout.size}")
    return layout.unpack(pkt[spec.TLM_HDR:spec.TLM_HDR + layout.size])


def startup_lines(text: str) -> list[list[str]]:
    """Entries of a cfe_es_startup.scr: fields of each line up to the terminating ';'."""
    out = []
    for raw in text.split(";"):
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) >= 4:
            out.append(fields)
    return out


def move_startup_line(text: str, name: str, before: str) -> str:
    """The startup script with the entry of object `name` moved right before that of `before`."""
    entries = [e.strip() for e in text.split(";")]
    entries = [e for e in entries if e]
    end = [e for e in entries if e.startswith("!")]
    entries = [e for e in entries if not e.startswith("!")]

    def obj(e: str) -> str:
        parts = [p.strip() for p in e.split(",")]
        return parts[1] if len(parts) > 1 else ""

    mover = [e for e in entries if obj(e) == name]
    rest = [e for e in entries if obj(e) != name]
    idx = next((i for i, e in enumerate(rest) if obj(e) == before), None)
    if not mover or idx is None:
        raise Fail(f"the startup script has no {name!r} or {before!r} entry")
    rest[idx:idx] = mover
    return "".join(e + ";\n" for e in rest) + "".join(e + "\n" for e in end)


EVENT_LINE_RE = re.compile(r"EVS Port\d+ \S+ \d+/\d+/(\S+) (\d+): ?(.*)")


def console_events(text: str) -> list[tuple[str, int, str]]:
    """(app, event id, text) of the event lines EVS writes to the console."""
    return [(m.group(1), int(m.group(2)), m.group(3)) for m in EVENT_LINE_RE.finditer(text)]
