#!/usr/bin/env python3
"""Pass path of the live plumbing and of the S3, S4 and node-level live checks, against fake nodes.

  live_fake.py <work-dir> [--mutant NAME]   (a seeded fault in the fake: the checks it targets must then fail)

Builds an installed-looking tree whose core-cpu1/core-cpu2 are selftest/fake_node.py, whose tools/ are the
reference run/stop scripts and frame builder, and runs the review's own live check functions on it through the
review's isolated lane (check.run_isolated: groups in their own namespaces, T5_PACK_JOBS at a time, each check
with its retry policy). Every selected check must pass (with --mutant, every targeted check must fail). Prints
one line per check.
"""
from __future__ import annotations

import os
import shutil
import struct
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


def main() -> int:
    import check
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    mutant = ""
    if len(sys.argv) > 3 and sys.argv[2] == "--mutant":
        mutant = sys.argv[3]
        assert mutant in MUTANTS, f"unknown mutant {mutant}"
        os.environ["T5_FAKE_MUTANT"] = mutant  # inherited by the fake nodes
    selected = MUTANTS[mutant] if mutant else CHECKS
    tree = make_tree(work)
    pack_work = work / "pack-work"
    shutil.rmtree(pack_work, ignore_errors=True)
    ctx = check.Ctx(tree, pack_work, time.monotonic() + 1800)
    ctx.work.mkdir(parents=True)
    ctx.tree = tree
    ctx.build_ok = True
    results = check.Results()
    t0 = time.monotonic()
    try:
        check.run_isolated(ctx, results, set(selected), check.group_jobs())
    finally:
        check.GROUP_PROCS.kill_all()
    bad = 0
    for cid in selected:
        r = results.items[cid]
        ok = bool(r["passed"]) != bool(mutant)
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'BAD '} {cid}: {str(r['detail'])[:400]}", flush=True)
    label = f"live fake{' mutant ' + mutant if mutant else ''}"
    print(f"{label}: {'OK' if bad == 0 else f'{bad} unexpected verdicts'} ({time.monotonic() - t0:.0f} s)", flush=True)
    return 0 if bad == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
