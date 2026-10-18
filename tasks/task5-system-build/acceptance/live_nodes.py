"""Node-level live checks: the run scripts of C.1, both nodes reaching OPERATIONAL with the new apps (N), and
the pilot probes for distinct RAM disks (C.1) and node clocks within the age window (S5.2)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from common import Fail, Problems, short
from live_common import boot
from node import Node, console_events, find_node_pids

if TYPE_CHECKING:
    from check import Ctx

NEW_APPS = ("STOR", "CMDI", "TLMO", "GW", "SFI")


def two_nodes(ctx: "Ctx") -> dict[int, Node]:
    key = "two-nodes"
    cached = ctx.cache.get(key)
    if cached is not None and all(n.alive() for n in cached.values()):
        return cached
    nodes = boot(ctx, (1, 2))
    ctx.cache[key] = nodes
    return nodes


def judge_startup_events(events: list[tuple[str, int, str]]) -> list[str]:
    p = Problems()
    for app in NEW_APPS:
        p.check(any(a == app and e == 1 for a, e, _t in events), f"no {app} initialization event (ID 1)")
    for lab in ("CI_LAB", "TO_LAB"):
        p.check(not any(a == lab for a, _e, _t in events), f"{lab} is running")
    return p.items


def c_nodes_operational(ctx: "Ctx") -> None:
    nodes = two_nodes(ctx)
    p = Problems()
    time.sleep(2.0)
    for nid, n in nodes.items():
        for item in judge_startup_events(console_events(n.console_log())):
            p.check(False, f"cpu{nid}: {item}")
        p.check(n.alive(), f"cpu{nid} stopped running")
    p.raise_if()


def ram_disk_identity(pid: int) -> tuple[str, tuple[int, int]] | None:
    """(host view path, (st_dev, st_ino)) of the OSAL RAM volume directory as process `pid` sees it, in the
    order the POSIX OSAL chooses its location (/dev/shm, $TMPDIR, /var/tmp, /tmp)."""
    root = Path(f"/proc/{pid}/root")
    try:
        env = dict(kv.split("=", 1) for kv in (Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0"))
                   if "=" in kv)
    except OSError:
        env = {}
    for base in ("/dev/shm", env.get("TMPDIR"), "/var/tmp", "/tmp"):
        if not base:
            continue
        cand = root / base.lstrip("/") / "osal:RAM"
        try:
            st = cand.stat()
        except OSError:
            continue
        return f"{base}/osal:RAM", (st.st_dev, st.st_ino)
    return None


def c_ram_disks_distinct(ctx: "Ctx") -> None:
    nodes = two_nodes(ctx)
    ids = {}
    for nid, n in nodes.items():
        pids = n.pids()
        if not pids:
            raise Fail(f"cpu{nid} is not running")
        ident = ram_disk_identity(pids[0])
        if ident is None:
            raise Fail(f"the OSAL RAM disk of cpu{nid} (pid {pids[0]}) could not be located under /dev/shm, $TMPDIR, /var/tmp or /tmp "
                       f"as the process sees them")
        ids[nid] = ident
    if ids[1][1] == ids[2][1]:
        raise Fail(f"cpu1 and cpu2 share one RAM disk directory ({ids[1][0]})")


def c_node_clocks(ctx: "Ctx") -> None:
    nodes = two_nodes(ctx)
    secs = {nid: n.onboard_seconds() for nid, n in nodes.items()}
    diff = abs(secs[1] - secs[2])
    if diff > 25:
        raise Fail(f"onboard seconds differ by {diff} s (cpu1 {secs[1]}, cpu2 {secs[2]}); GW frames between the nodes need "
                   f"agreement within MaxAgeSeconds 30")


def c_run_scripts(ctx: "Ctx") -> None:
    """CFS_CPU selects the processor; default run directories are run/ (cpu1) and run/cpu2/ (cpu2)."""
    ctx.shutdown_session()
    p = Problems()
    for nid in (1, 2):
        n = Node(ctx.tree, ctx.work / "nodes" / f"runscripts-cpu{nid}", nid, ctx.pristine(nid))
        n.prepare()
        env = dict(os.environ, CFS_TIMEOUT="40")
        if nid == 2:
            env["CFS_CPU"] = "cpu2"
        env.pop("CFS_RUN_DIR", None)
        run_dir = n.root / "build-native_std" / "run" / ("cpu2" if nid == 2 else "")
        try:
            proc = subprocess.run(["bash", str(n.root / "tools" / "run_cfs.sh")], env=env, capture_output=True, text=True,
                                  errors="replace", timeout=70)
        except subprocess.TimeoutExpired:
            p.check(False, f"cpu{nid}: run_cfs.sh did not return within 70 s")
            continue
        try:
            if not p.check(proc.returncode == 0, f"cpu{nid}: run_cfs.sh exited {proc.returncode}: {short(proc.stderr, 300)}"):
                continue
            pids = find_node_pids(n.exe, nid)
            p.check(bool(pids), f"cpu{nid}: no core-cpu{nid} runs from build-native_std/exe/cpu{nid}")
            pidfile = run_dir / "cfs.pid"
            logs = list(run_dir.glob("*.log")) if run_dir.is_dir() else []
            p.check(pidfile.is_file(), f"cpu{nid}: no PID file in build-native_std/run/{'cpu2/' if nid == 2 else ''}")
            p.check(bool(logs), f"cpu{nid}: no log in build-native_std/run/{'cpu2/' if nid == 2 else ''}")
            if nid == 1:
                p.check((run_dir / "cfs.log").is_file(), "cpu1: the log is not build-native_std/run/cfs.log")
        finally:
            subprocess.run(["bash", str(n.root / "tools" / "stop_cfs.sh")], env=env, capture_output=True, timeout=30, check=False)
            time.sleep(0.5)
            left = find_node_pids(n.exe, nid)
            p.check(not left, f"cpu{nid}: stop_cfs.sh{' with CFS_CPU=cpu2' if nid == 2 else ''} left core-cpu{nid} running")
            n.stop()
    p.raise_if()
