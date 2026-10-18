#!/usr/bin/env python3
"""Pass path of the live plumbing and of the S3, S4 and node-level live checks, against fake nodes.

  live_fake.py <work-dir> [--mutant NAME]   (a seeded fault in the fake: the checks it targets must then fail)

Builds an installed-looking tree whose core-cpu1/core-cpu2 are selftest/fake_node.py, whose tools/ are the
reference run/stop scripts and frame builder, and runs the review's own live check functions on it (through
check.run_one, with its retry policy) inside a private network and mount namespace, so the fixed node ports
and /dev/shm are private. Every selected check must pass. Prints one line per check.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
sys.path.insert(0, str(PACK))

CHECKS = ["s3-harness-cli", "n-run-scripts", "n-nodes-operational", "n-ram-disks-distinct", "n-node-clocks",
          "s4-default-telemetry", "s3-valid-frame-executes", "s3-unframed-format", "s3-rejection-reasons",
          "s3-set-enable-mask", "s3-set-max-age", "s3-cmdi-commands", "s3-harness-live", "s3-cpu2-frame",
          "s4-hold-drop-arithmetic", "s4-drain-order", "s4-set-class", "s4-set-budget", "s4-add-remove-stream",
          "s4-table-update", "s4-class-map", "s4-tlmo-commands", "s4-cpu2-telemetry", "s4-pipe-depth-probe"]


def header(name: str, size: int) -> bytes:
    hdr = bytearray(116)
    struct.pack_into(">II", hdr, 0, 0x63464531, 8)
    struct.pack_into(">III", hdr, 64, 0, 0, size)
    hdr[76:76 + len(name)] = name.encode()
    return bytes(hdr)


def make_tree(work: Path) -> Path:
    import spec
    import tables as tb
    tree = work / "fake-tree"
    if tree.exists():
        shutil.rmtree(tree)
    shutil.copytree(HERE / "fixtures" / "ref_tools", tree / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    for node in spec.NODES:
        exe = tree / "build-native_std" / "exe" / f"cpu{node}"
        (exe / "cf").mkdir(parents=True)
        core = exe / f"core-cpu{node}"
        core.write_text(f"#!{sys.executable} -IB\nimport sys\nsys.argv[0] = 'core-cpu{node}'\n"
                        f"sys.path.insert(0, {str(HERE)!r})\nimport fake_node\nfake_node.main()\n")
        core.chmod(0o755)
        streams = tuple(spec.tlmo_default_streams(node).items())
        bodies = {"STOR": tb.StorTable().pack(), "CMDI": tb.CmdiTable().pack(), "TLMO": tb.TlmoTable(16, 64, streams).pack(),
                  "GW": tb.gw_table(), "SFI": tb.SfiTable().pack()}
        for app, (fname, name, size) in spec.TABLES.items():
            (exe / "cf" / fname).write_bytes(header(name, size) + bodies[app])
        (exe / "cf" / "cfe_es_startup.scr").write_text("CFE_APP, cmdi, CMDI_AppMain, CMDI, 40, 32768, 0x0, 0;\n"
                                                        "CFE_APP, gw, GW_AppMain, GW, 70, 131072, 0x0, 0;\n")
    return tree


MUTANTS = {  # seeded fault -> checks that must then fail
    "evict-newest": ["s4-hold-drop-arithmetic"],
    "format-as-crc": ["s3-unframed-format", "s3-rejection-reasons"],
    "budget-ignored": ["s4-drain-order", "s4-set-budget"],
    "max-age-ignored": ["s3-set-max-age"],
}


def inner(work: Path) -> int:
    import check
    mutant = os.environ.get("T5_FAKE_MUTANT", "")
    tree = make_tree(work)
    ctx = check.Ctx(tree, work / "pack-work", time.monotonic() + 1800)
    ctx.work.mkdir(parents=True, exist_ok=True)
    ctx.tree = tree
    ctx.build_ok = True
    results = check.Results()
    bad = 0
    t0 = time.monotonic()
    for cid in (MUTANTS[mutant] if mutant else CHECKS):
        check.run_one(ctx, check.BY_ID[cid], results)
        r = results.items[cid]
        ok = bool(r["passed"]) != bool(mutant)
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'BAD '} {cid}: {str(r['detail'])[:400]}", flush=True)
    ctx.shutdown_session()
    label = f"live fake{' mutant ' + mutant if mutant else ''}"
    print(f"{label}: {'OK' if bad == 0 else f'{bad} unexpected verdicts'} ({time.monotonic() - t0:.0f} s)", flush=True)
    return 0 if bad == 0 else 1


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    if os.environ.get("T5_LIVE_FAKE_INNER") == "1":
        return inner(work)
    work.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 3 and sys.argv[2] == "--mutant":
        assert sys.argv[3] in MUTANTS
        os.environ["T5_FAKE_MUTANT"] = sys.argv[3]
    uid, gid = os.getuid(), os.getgid()
    script = (f"mount -t tmpfs tmpfs /dev/shm && ip link set lo up && exec unshare -U --map-user={uid} --map-group={gid} "
              f"env T5_LIVE_FAKE_INNER=1 {sys.executable} -B {Path(__file__).resolve()} {work}")
    return subprocess.run(["unshare", "-rnm", "--propagation", "private", "sh", "-c", script]).returncode


if __name__ == "__main__":
    sys.exit(main())
