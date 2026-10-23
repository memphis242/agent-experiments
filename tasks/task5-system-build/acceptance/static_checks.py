"""Checks that read the repository copy and the build: layout and mission integration (C.2, C.6), the
build and unit tests (N), coverage (N), design notes and the report (N), installed default tables
(S1.1, S3.3, S4.1, S5.1, S6.1) and the library's self-containment (S3.2)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import spec
import tables as tb
from common import Fail, InfraError, Problems

# A coverage figure: a percentage ("97.5%", "100 percent") or covered/total lines ("44 / 44", "176 of 176").
COVERAGE_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|percent\b)|\b\d+\s*(?:/|of)\s*\d+\b", re.IGNORECASE)

if TYPE_CHECKING:
    from check import Ctx

PACK = Path(__file__).resolve().parent
SOURCE_PCT_MIN = 95.0
DESIGN_MAX_LINES = 80
APPS = ("stor", "cmdi", "tlmo", "gw", "sfi", "mode")


def strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def strip_cmake_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


# ============================================================================ C.2 layout

def component_sources(tree: Path, comp: str) -> list[Path]:
    d = tree / spec.COMPONENTS[comp][0] / "fsw" / "src"
    return sorted(p for p in d.glob("*.c") if "eds" not in p.stem) if d.is_dir() else []


def judge_layout(tree: Path) -> list[str]:
    p = Problems()
    for app in APPS:
        d = tree / "apps" / app
        if not p.check(d.is_dir(), f"apps/{app} does not exist"):
            continue
        for sub in ("fsw/src", "fsw/inc", "fsw/tables", "config", "unit-test"):
            p.check((d / sub).is_dir(), f"apps/{app}/{sub} is missing")
        p.check((d / "DESIGN.md").is_file(), f"apps/{app}/DESIGN.md is missing")
        for hdr in (f"fsw/inc/{app}_topicids.h", f"fsw/inc/{app}_eventids.h", f"fsw/inc/{app}_fcncodes.h",
                    f"config/default_{app}_msgids.h", f"config/default_{app}_fcncode_values.h",
                    f"config/default_{app}_tbldefs.h", f"fsw/tables/{app}_tbl.c"):
            p.check((d / hdr).is_file(), f"apps/{app}/{hdr} is missing")
        srcs = component_sources(tree, app)
        p.check(bool(srcs), f"apps/{app}/fsw/src has no C sources")
        text = "\n".join(read(s) for s in srcs)
        p.check(f"{app.upper()}_AppMain" in text, f"no {app.upper()}_AppMain entry point in apps/{app}/fsw/src")
        eds = [str(x.relative_to(d)) for x in d.rglob("*") if x.is_file() and
               (re.search(r"_eds_", x.name) or x.name.startswith("eds_"))]
        p.check(not eds, f"apps/{app} carries EDS variants that are not carried over: {eds[:3]}")
    sfi = tree / "apps" / "sfi" / "fsw" / "src"
    p.check((sfi / "sfi_cluster.c").is_file() and (sfi / "sfi_cluster.h").is_file(),
            "SFI_ComputeClusters must live in apps/sfi/fsw/src/sfi_cluster.c, declared in sfi_cluster.h")
    if (sfi / "sfi_cluster.h").is_file():
        p.check("SFI_ComputeClusters" in read(sfi / "sfi_cluster.h"), "sfi_cluster.h does not declare SFI_ComputeClusters")
    lib = tree / "libs" / "cmdval_lib"
    if p.check(lib.is_dir(), "libs/cmdval_lib does not exist"):
        for sub in ("fsw/src", "fsw/public_inc", "unit-test", "ut-stubs"):
            p.check((lib / sub).is_dir(), f"libs/cmdval_lib/{sub} is missing")
        p.check((lib / "DESIGN.md").is_file(), "libs/cmdval_lib/DESIGN.md is missing")
        p.check((lib / "fsw" / "public_inc" / "cmdval_lib.h").is_file(), "libs/cmdval_lib/fsw/public_inc/cmdval_lib.h is missing")
        text = "\n".join(read(s) for s in component_sources(tree, "cmdval_lib"))
        p.check("CMDVAL_LibInit" in text, "no CMDVAL_LibInit in libs/cmdval_lib/fsw/src")
    return p.items


def c_layout(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_layout(ctx.tree)
    if items:
        raise Fail("; ".join(items))


# ============================================================================ C.6 integration

def applist(text: str, var: str) -> list[str] | None:
    m = re.search(r"\bSET\(\s*" + var + r"\s+([^)]*)\)", strip_cmake_comments(text), re.I)
    return m.group(1).split() if m else None


def judge_targets(tree: Path) -> list[str]:
    p = Problems()
    text = read(tree / "sample_defs" / "targets.cmake")
    code = strip_cmake_comments(text)
    glob = []
    for m in re.finditer(r"\b(?:list|LIST)\(\s*APPEND\s+MISSION_GLOBAL_APPLIST\s+([^)]*)\)", code):
        glob += m.group(1).split()
    for app in ("cmdval_lib", "stor", "cmdi", "tlmo", "gw", "sfi", "mode"):
        p.check(app in glob, f"{app} is not added to MISSION_GLOBAL_APPLIST in sample_defs/targets.cmake")
    for cpu in ("cpu1", "cpu2"):
        lst = applist(text, f"{cpu}_APPLIST")
        p.check(lst == ["sch_lab"], f"{cpu}_APPLIST is {lst}, expected only sch_lab (ci_lab and to_lab removed)")
    osc = read(tree / "sample_defs" / "default_osconfig.cmake")
    m = re.search(r"^\s*set\(\s*OSAL_CONFIG_MAX_FILE_NAME\s+(\d+)\s*\)", osc, re.M)
    p.check(m is not None and int(m.group(1)) == 32, "OSAL_CONFIG_MAX_FILE_NAME is not 32 in sample_defs/default_osconfig.cmake")
    m = re.search(r"^\s*set\(\s*OSAL_CONFIG_QUEUE_MAX_DEPTH\s+(\d+)\s*\)", osc, re.M | re.I)
    p.check(m is not None and int(m.group(1)) == 512,
            "OSAL_CONFIG_QUEUE_MAX_DEPTH is not 512 in sample_defs/default_osconfig.cmake")
    return p.items


def c_targets(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_targets(ctx.tree)
    if items:
        raise Fail("; ".join(items))


def norm_fields(line: str) -> tuple[str, ...]:
    return tuple(f.strip() for f in line.strip().rstrip(";").split(","))


def judge_startup_script(text: str) -> list[str]:
    """Problems with one generated cf/cfe_es_startup.scr (C.6)."""
    from node import startup_lines
    p = Problems()
    entries = [tuple(e[:8]) for e in startup_lines(text)]
    objs = [e[1] for e in entries if len(e) > 1]
    for lab in ("ci_lab", "to_lab"):
        p.check(lab not in objs and lab not in text, f"the startup script names {lab}")
    for name, want in spec.STARTUP_LINES.items():
        got = [e for e in entries if len(e) > 1 and e[1] == name]
        if not p.check(len(got) == 1, f"the startup script has {len(got)} entries for {name}"):
            continue
        p.check(tuple(got[0]) == want, f"startup entry for {name} is {', '.join(got[0])}; expected {', '.join(want)}")
    if all(x in objs for x in ("sample_lib", "cmdval_lib")):
        p.check(objs.index("cmdval_lib") == objs.index("sample_lib") + 1, "cmdval_lib is not listed right after sample_lib")
    if all(x in objs for x in ("sch_lab", "cmdi", "tlmo", "sample_app")):
        p.check(objs.index("sch_lab") < objs.index("cmdi") < objs.index("tlmo") < objs.index("sample_app"),
                "cmdi and tlmo do not take over the lab apps' slots between sch_lab and sample_app")
    loop = [o for o in objs if o in ("lc", "cf", "ds", "fm", "hk", "hs", "sc", "md", "cs", "stor", "gw", "sfi", "mode", "sbn")]
    p.check(loop == ["lc", "cf", "ds", "fm", "hk", "hs", "sc", "md", "cs", "stor", "gw", "sfi", "mode", "sbn"],
            f"the started loop apps are in the order {loop}; CFS_APP_STARTUP_ORDER must be lc cf ds fm hk hs sc md cs stor gw sfi mode sbn")
    return p.items


def c_startup_scripts(ctx: "Ctx") -> None:
    ctx.build()
    p = Problems()
    for cpu in ("cpu1", "cpu2"):
        scr = ctx.tree / "build-native_std" / "exe" / cpu / "cf" / "cfe_es_startup.scr"
        if not p.check(scr.is_file(), f"build-native_std/exe/{cpu}/cf/cfe_es_startup.scr was not generated"):
            continue
        for item in judge_startup_script(read(scr)):
            p.check(False, f"{cpu}: {item}")
    gen = strip_cmake_comments(read(ctx.tree / "sample_defs" / "generate_startup.cmake"))
    m = re.search(r"set\s*\(\s*CFS_APP_STARTUP_ORDER\s+([^)]*)\)", gen, re.I)
    p.check(m is not None and m.group(1).split() == "lc cf ds fm hk hs sc md cs stor gw sfi mode sbn".split(),
            "CFS_APP_STARTUP_ORDER in sample_defs/generate_startup.cmake is not lc cf ds fm hk hs sc md cs stor gw sfi mode sbn")
    p.raise_if()


def judge_install_custom(text: str, cpu: str) -> list[str]:
    p = Problems()
    code = strip_cmake_comments(text)
    m = re.search(r"set\s*\(\s*APP_INCLUDE_TABLE_LIST(.*?)\)\s*\n", code, re.S | re.I)
    block = m.group(1) if m else ""
    p.check(m is not None, f"{cpu}/install_custom.cmake has no APP_INCLUDE_TABLE_LIST")
    for lab in ("ci_lab", "to_lab"):
        p.check(f"TARGET_PROPERTY:{lab}," not in block.replace(" ", ""), f"{cpu}/install_custom.cmake still lists {lab} in APP_INCLUDE_TABLE_LIST")
    for app in APPS:
        p.check(re.search(r"TARGET_PROPERTY:\s*" + app + r"\s*,\s*INCLUDE_DIRECTORIES", code) is not None,
                f"{cpu}/install_custom.cmake does not add {app}'s include directories as hk is")
    p.check("to_lab.table" not in code, f"{cpu}/install_custom.cmake still names to_lab.table")
    p.check(re.search(r"add_cfe_tables\s*\(\s*to_lab\b", code) is None, f"{cpu}/install_custom.cmake still builds to_lab_sub_alt.c")
    for t in ("tlmo.table", "stor.table", "sfi.table", "mode.table"):
        p.check(re.search(r"target_include_directories\s*\(\s*" + re.escape(t) + r"\b[^)]*APP_INCLUDE_TABLE_LIST", code) is not None,
                f"{cpu}/install_custom.cmake does not give {t} the APP_INCLUDE_TABLE_LIST include directories")
        p.check(re.search(r"target_compile_definitions\s*\(\s*" + re.escape(t) + r"\b[^)]*APP_DEFINITION_TABLE_LIST", code) is not None,
                f"{cpu}/install_custom.cmake does not give {t} the APP_DEFINITION_TABLE_LIST definitions")
    return p.items


def c_install_custom(ctx: "Ctx") -> None:
    ctx.copy_tree()
    p = Problems()
    for cpu in ("cpu1", "cpu2"):
        f = ctx.tree / "sample_defs" / cpu / "install_custom.cmake"
        if p.check(f.is_file(), f"sample_defs/{cpu}/install_custom.cmake is missing"):
            for item in judge_install_custom(read(f), cpu):
                p.check(False, item)
    p.raise_if()


SCHED_NEW = (("STOR_SEND_HK_MID", 49), ("STOR_WAKEUP_MID", 10), ("CMDI_SEND_HK_MID", 50), ("CMDI_WAKEUP_MID", 1),
             ("TLMO_SEND_HK_MID", 51), ("TLMO_WAKEUP_MID", 1), ("GW_SEND_HK_MID", 53), ("GW_WAKEUP_MID", 1),
             ("SFI_SEND_HK_MID", 55), ("SFI_WAKEUP_MID", 10),
             ("MODE_SEND_HK_MID", 57), ("MODE_WAKEUP_MID", 10))


def judge_schedule(text: str) -> list[str]:
    p = Problems()
    s = strip_c_comments(text)
    entries = re.findall(r"\{\s*CFE_SB_MSGID_WRAP_VALUE\(\s*(\w+)\s*\)\s*,\s*(\d+)\s*,\s*(\w+)\s*\}", s)
    names = [e[0] for e in entries]
    for name, period in SCHED_NEW:
        got = [e for e in entries if e[0] == name]
        if p.check(len(got) == 1, f"sch_lab_table.c has {len(got)} {name} entries, expected one"):
            p.check(int(got[0][1]) == period and got[0][2] == "0",
                    f"sch_lab_table.c {name} entry is ({got[0][1]}, {got[0][2]}), expected ({period}, 0)")
    for lab in ("CI_LAB_SEND_HK_MID", "TO_LAB_SEND_HK_MID"):
        p.check(lab not in names, f"sch_lab_table.c still schedules {lab}")
    p.check(re.search(r"#\s*include\s*[\"<](ci_lab_msgids|to_lab_msgids)\.h", s) is None,
            "sch_lab_table.c still includes ci_lab_msgids.h or to_lab_msgids.h")
    actual = len(re.findall(r"\{[^{}]*\}", s))
    p.check(actual == 32 and len(entries) == 32,
            f"sch_lab_table.c has {actual} actual initializers ({len(entries)} recognized), expected 22 - 2 + 12 = 32 (0 spare), including both SBN entries")
    return p.items


def c_schedule(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_schedule(read(ctx.tree / "sample_defs" / "tables" / "sch_lab_table.c"))
    if items:
        raise Fail("; ".join(items))


def judge_readme(text: str) -> list[str]:
    p = Problems()
    m = re.search(r"^#+\s*Ground interface.*?(?=^#{1,2}\s|\Z)", text, re.M | re.S)
    sec = m.group(0) if m else ""
    p.check(bool(sec), "README.md has no Ground interface section")
    if sec:
        p.check("CMDI" in sec and "TLMO" in sec, "the Ground interface section does not name CMDI and TLMO")
        p.check(re.search(r"fram", sec, re.I) is not None, "the Ground interface section does not mention framed commands")
        for port in ("1234", "2234"):
            p.check(port in sec, f"the Ground interface section does not give port {port}")
    m = re.search(r"^#+\s*Registering an app.*?(?=^#{1,2}\s|\Z)", text, re.M | re.S)
    reg = m.group(0) if m else ""
    p.check(bool(reg), "README.md has no Registering an app section")
    if reg:
        p.check("to_lab_sub.c" not in reg, "the Registering an app section still names to_lab_sub.c")
        p.check(re.search(r"tlmo", reg, re.I) is not None, "the Registering an app section does not name TLMO's table")
    return p.items


def c_readme(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_readme(read(ctx.tree / "README.md"))
    if items:
        raise Fail("; ".join(items))


# ============================================================================ change scope (N)

ALLOWED_CHANGED = {
    "sample_defs/targets.cmake", "sample_defs/generate_startup.cmake", "sample_defs/cpu1/install_custom.cmake",
    "sample_defs/cpu2/install_custom.cmake", "sample_defs/default_osconfig.cmake", "sample_defs/tables/sch_lab_table.c",
    "README.md", "tools/run_cfs.sh", "tools/stop_cfs.sh", "tools/udp_harness.py",
}
ALLOWED_PREFIXES = ("apps/stor/", "apps/cmdi/", "apps/tlmo/", "apps/gw/", "apps/sfi/", "apps/mode/", "libs/cmdval_lib/", "ctf/", "docs/")


IDENT_RE = re.compile(rb"\$Id:[^$\n]*\$")


def git_blob_sha(path: Path) -> str:
    """The git blob id of a working-tree file (symlink target text; $Id$ keywords collapsed as git's ident does)."""
    if path.is_symlink():
        data = os.readlink(path).encode()
    else:
        data = IDENT_RE.sub(b"$Id$", path.read_bytes())
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load_manifest() -> dict[str, list[str]]:
    doc = json.loads(gzip.decompress((PACK / "baseline_manifest.json.gz").read_bytes()))
    return doc["files"]


def mission_sb_override(rel: str) -> bool:
    """C.6 permits mission SB pool configuration headers, never flight SB source edits."""
    return re.fullmatch(r"sample_defs/(?:cpu[12]/)?(?:inc/)?(?:cpu[12]_)?cfe_sb_(?:internal|platform|mission)_cfg(?:_values)?\.h", rel) is not None


def judge_change_scope(tree: Path, manifest: dict[str, list[str]]) -> list[str]:
    changed, removed = [], []
    for rel, (_mode, sha) in manifest.items():
        f = tree / rel
        if not (f.exists() or f.is_symlink()):
            removed.append(rel)
            continue
        if f.is_file() or f.is_symlink():
            try:
                if git_blob_sha(f) != sha:
                    changed.append(rel)
            except OSError:
                changed.append(rel)
    bad = [r for r in changed + removed if r not in ALLOWED_CHANGED and not r.startswith(ALLOWED_PREFIXES)
           and not mission_sb_override(r) and not r.endswith(".gitignore")]
    lab_new = [str(x.relative_to(tree)) for d in ("apps/ci_lab", "apps/to_lab") if (tree / d).is_dir()
               for x in (tree / d).rglob("*") if x.is_file() and str(x.relative_to(tree)) not in manifest]
    out = []
    if bad:
        out.append(f"{len(bad)} existing files outside the allowed set were changed or removed, first: {sorted(bad)[:6]}")
    if lab_new:
        out.append(f"files were added to the lab apps: {sorted(lab_new)[:4]}")
    return out


def c_change_scope(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_change_scope(ctx.tree, load_manifest())
    if items:
        raise Fail("; ".join(items))


# ============================================================================ build and unit tests (N)

def c_build_werror(ctx: "Ctx") -> None:
    ctx.build()
    p = Problems()
    p.check(not ctx.warnings, f"{len(ctx.warnings)} compiler warnings, first: {ctx.warnings[0] if ctx.warnings else ''}")
    abc = read(ctx.tree / "sample_defs" / "arch_build_custom.cmake")
    p.check(re.search(r"^\s*-Werror\b", strip_cmake_comments(abc), re.M) is not None,
            "-Werror was removed from sample_defs/arch_build_custom.cmake")
    p.raise_if()


def c_unit_tests(ctx: "Ctx") -> None:
    ctx.build()
    proc = ctx.runtest()
    p = Problems()
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-900:]
        p.check(False, f"make native_std.runtest exited {proc.returncode}: {tail}")
    results = ctx.tree / "build-native_std" / "test-results"
    labs = sorted(x.name for x in results.glob("coverage-*lab*.log") if re.match(r"coverage-(ci_lab|to_lab)-", x.name)) \
        if results.is_dir() else []
    p.check(not labs, f"CI_LAB or TO_LAB unit tests are still built and run: {labs[:4]}")
    p.raise_if()


def c_coverage_tests_present(ctx: "Ctx") -> None:
    ctx.build()
    ctx.runtest()
    p = Problems()
    results = ctx.tree / "build-native_std" / "test-results"
    for comp, (rel, _prefix) in spec.COMPONENTS.items():
        srcs = component_sources(ctx.tree, comp)
        if not p.check(bool(srcs), f"{rel}/fsw/src has no sources"):
            continue
        for s in srcs:
            cov = ctx.tree / rel / "unit-test" / "coveragetest" / f"coveragetest_{s.stem}.c"
            p.check(cov.is_file(), f"{rel}/unit-test/coveragetest/coveragetest_{s.stem}.c is missing")
            log = results / f"coverage-{comp}-{s.stem}.log"
            if not p.check(log.is_file(), f"no coverage-{comp}-{s.stem} test result"):
                continue
            text = read(log)
            p.check(all(int(f) == 0 for f in re.findall(r"FAIL::(\d+)", text)) and "[ FAIL]" not in text,
                    f"coverage-{comp}-{s.stem} has failing assertions")
            p.check(sum(int(x) for x in re.findall(r"PASS::(\d+)", text)) > 0, f"coverage-{comp}-{s.stem} has no passing assertions")
    p.raise_if()


def gcov_line_coverage(tree: Path, comp: str) -> dict[str, tuple[int, int]]:
    """Per source file of `comp`: (executed lines, instrumented lines), from its coverage test objects."""
    rel = spec.COMPONENTS[comp][0]
    base = tree / "build-native_std" / "native"
    gcdas = sorted(g for g in base.rglob(f"coverage-{comp}-*-object.dir/**/*.gcda") if "-hidden" not in str(g))
    src_dir = (tree / rel / "fsw" / "src").resolve()
    result: dict[str, tuple[int, int]] = {}
    for gcda in gcdas:
        try:
            proc = subprocess.run(["gcov", "--json-format", "--stdout", gcda.name], cwd=gcda.parent, text=True,
                                  errors="replace", capture_output=True, timeout=120)
        except FileNotFoundError as exc:
            raise InfraError("gcov is not installed") from exc
        if proc.returncode != 0:
            continue
        dec = json.JSONDecoder()
        out, pos = proc.stdout, 0
        while pos < len(out):
            while pos < len(out) and out[pos].isspace():
                pos += 1
            if pos >= len(out):
                break
            doc, pos = dec.raw_decode(out, pos)
            for f in doc.get("files", []):
                path = Path(f["file"])
                if not path.is_absolute():
                    path = gcda.parent / path
                try:
                    path = path.resolve()
                except OSError:
                    continue
                if path.parent != src_dir or path.suffix != ".c":
                    continue
                counts: dict[int, int] = {}
                for ln in f.get("lines", []):
                    n = int(ln["line_number"])
                    counts[n] = max(counts.get(n, 0), int(ln["count"]))
                hit = sum(1 for c in counts.values() if c > 0)
                prev = result.get(path.name, (0, 0))
                if prev[1] == 0 or hit * prev[1] > prev[0] * len(counts):
                    result[path.name] = (hit, len(counts))
    return result


def judge_coverage(srcs: dict[str, list[str]], cov: dict[str, dict[str, tuple[int, int]]]) -> list[str]:
    """srcs: component -> source file names; cov: component -> file -> (hit, total)."""
    p = Problems()
    for comp, names in srcs.items():
        rel = spec.COMPONENTS[comp][0]
        for name in names:
            got = cov.get(comp, {}).get(name)
            if got is None:
                p.check(False, f"{rel}/fsw/src/{name}: no coverage data")
                continue
            hit, total = got
            pct = 100.0 if total == 0 else 100.0 * hit / total
            p.check(pct >= SOURCE_PCT_MIN, f"{rel}/fsw/src/{name}: {hit}/{total} lines = {pct:.1f}% (< {SOURCE_PCT_MIN:.0f}%)")
    return p.items


def c_coverage(ctx: "Ctx") -> None:
    ctx.build()
    if ctx.runtest().returncode != 0:
        raise Fail("make native_std.runtest does not pass, so there is no complete coverage data")
    srcs = {comp: [s.name for s in component_sources(ctx.tree, comp)] for comp in spec.COMPONENTS}
    empty = [spec.COMPONENTS[c][0] for c, names in srcs.items() if not names]
    if empty:
        raise Fail(f"no sources to measure in {', '.join(empty)}")
    cov = {comp: gcov_line_coverage(ctx.tree, comp) for comp in spec.COMPONENTS}
    ctx.coverage = cov
    print("coverage: " + "; ".join(f"{c}: " + ", ".join(f"{n} {h}/{t}" for n, (h, t) in sorted(v.items()))
                                    for c, v in cov.items()), flush=True)
    items = judge_coverage(srcs, cov)
    if items:
        raise Fail("; ".join(items))


# ============================================================================ documents (N)

DESIGN_TOPICS = {
    "purpose": r"purpose",
    "data flow": r"data[ -]?flow|flow of data",
    "main data structures": r"data structure|structures",
    "timing": r"timing|wake ?up|wakeup",
    "error handling": r"error",
    "decisions": r"decision|chose|choice",
}


def judge_design(text: str, rel: str) -> list[str]:
    p = Problems()
    lines = text.splitlines()
    p.check(len(lines) <= DESIGN_MAX_LINES, f"{rel} has {len(lines)} lines (at most {DESIGN_MAX_LINES})")
    p.check(sum(1 for ln in lines if ln.strip()) >= 8, f"{rel} has fewer than 8 non-empty lines")
    low = text.lower()
    missing = [t for t, rx in DESIGN_TOPICS.items() if re.search(rx, low) is None]
    p.check(not missing, f"{rel} does not cover: {', '.join(missing)}")
    return p.items


def c_design_notes(ctx: "Ctx") -> None:
    ctx.copy_tree()
    p = Problems()
    for comp, (rel, _prefix) in spec.COMPONENTS.items():
        f = ctx.tree / rel / "DESIGN.md"
        if p.check(f.is_file(), f"{rel}/DESIGN.md does not exist"):
            for item in judge_design(read(f), f"{rel}/DESIGN.md"):
                p.check(False, item)
    p.raise_if()


REPORT_SECTIONS = ("Design summary", "Interfaces", "Unit tests", "Coverage", "System tests", "Limitations")


def report_sections(text: str) -> dict[str, str]:
    """Heading title (as in REPORT_SECTIONS, matched case-insensitively) -> section body."""
    out: dict[str, str] = {}
    heads = [(m.start(), m.end(), m.group(1).strip()) for m in re.finditer(r"^#{1,6}\s*(.+?)\s*#*\s*$", text, re.M)]
    for i, (_s, e, title) in enumerate(heads):
        body = text[e:heads[i + 1][0]] if i + 1 < len(heads) else text[e:]
        for want in REPORT_SECTIONS:
            if want.lower() in title.lower() and want not in out:
                out[want] = body
    return out


def judge_report(text: str) -> list[str]:
    p = Problems()
    secs = report_sections(text)
    missing = [s for s in REPORT_SECTIONS if s not in secs]
    p.check(not missing, f"docs/reports/system.md lacks the sections {missing}")
    if "Unit tests" in secs:
        body = secs["Unit tests"].lower()
        names = [c for c in ("stor", "cmdi", "tlmo", "gw", "sfi", "mode", "cmdval") if c not in body]
        p.check(not names and re.search(r"\d", body) is not None, f"the Unit tests section gives no counts for {names}")
    if "Coverage" in secs:
        body = secs["Coverage"]
        p.check(len(re.findall(r"\w+\.c\b", body)) >= 7 and COVERAGE_FIGURE_RE.search(body) is not None,
                "the Coverage section does not give coverage per source file")
    if "System tests" in secs:
        p.check("run_system_tests.sh" in secs["System tests"], "the System tests section does not refer to ctf/run_system_tests.sh")
    from ctf_checks import SMOKE, SYSTEM
    if "System tests" in secs:
        for script in SMOKE + SYSTEM:
            p.check(script.removesuffix(".json") in secs["System tests"], f"System tests omits the result of {script}")
    interfaces = secs.get("Interfaces", "")
    for app, names in spec.APP_TOPICS.items():
        for name in names:
            p.check(f"{app}_{name}_MID" in interfaces, f"Interfaces omits {app}_{name}_MID")
    for name in ("STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID", "SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID"):
        p.check(name in interfaces, f"Interfaces omits {name}")
    summary = secs.get("Design summary", "").lower()
    for term in ("32", "0 spare", "peer", "replay", "key", "lifetime"):
        p.check(term in summary, f"Design summary omits scheduler/lifetime decision {term!r}")
    low = text.lower()
    p.check("ram" in low and "disk" in low, "the report does not record how the two nodes' RAM disks are kept apart")
    p.check("clock" in low or "time" in low, "the report does not record how the nodes' clocks are kept within the age window")
    return p.items


def c_system_report(ctx: "Ctx") -> None:
    ctx.copy_tree()
    f = ctx.tree / "docs" / "reports" / "system.md"
    if not f.is_file():
        raise Fail("docs/reports/system.md does not exist")
    items = judge_report(read(f))
    if items:
        raise Fail("; ".join(items))


# ============================================================================ installed default tables

def installed_table(ctx: "Ctx", app: str, cpu: str = "cpu1") -> bytes:
    ctx.build()
    fname = spec.TABLES[app][0]
    f = ctx.tree / "build-native_std" / "exe" / cpu / "cf" / fname
    if not f.is_file():
        raise Fail(f"the build does not install /cf/{fname} on {cpu}")
    return f.read_bytes()


def body_of(ctx: "Ctx", app: str, cpu: str) -> bytes:
    try:
        return tb.split_image(installed_table(ctx, app, cpu), app)[1]
    except tb.TableImageError as exc:
        raise Fail(f"{cpu}: {exc}") from None


def judge_stor_default(t: tb.StorTable, node: int) -> list[str]:
    p = Problems()
    p.check(t.telemetry_root == "/cf/stor/tlm" and t.file_root == "/cf/stor/files", f"roots {t.telemetry_root!r} {t.file_root!r}")
    p.check((t.telemetry_quota, t.file_quota, t.max_packet_file, t.threshold) == (262144, 1048576, 16384, 80),
            f"quotas/max file/threshold {t.telemetry_quota}/{t.file_quota}/{t.max_packet_file}/{t.threshold}")
    want = {(spec.named_tlm_mid("CFE_EVS_HK_TLM_MID", node), 10, 1), (spec.named_tlm_mid("CFE_ES_HK_TLM_MID", node), 20, 1),
            (spec.named_tlm_mid("SAMPLE_APP_HK_TLM_MID", node), 30, 2), (spec.named_tlm_mid("CFE_TIME_HK_TLM_MID", node), 40, 1)}
    p.check(set(t.entries) == want and len(t.entries) == 4, f"entries {[(hex(m), a, b) for m, a, b in t.entries]}")
    return p.items


def judge_cmdi_default(t: tb.CmdiTable) -> list[str]:
    p = Problems()
    p.check(t.key == b"cFS-cmdval-test-key-0123456789ab", f"key {t.key!r}")
    p.check(t.sources == (1, 2, 0, 0, 0, 0, 0, 0), f"AllowedSources {t.sources}")
    p.check(t.max_age == 30 and t.mask == 0x0F, f"MaxAgeSeconds {t.max_age}, EnableMask {t.mask:#x}")
    p.check(t.next_key == bytes(32) and t.next_valid == 0, "default NEXT key/NextValid must be zero")
    return p.items


def judge_tlmo_default(t: tb.TlmoTable, node: int) -> list[str]:
    p = Problems()
    p.check((t.budget, t.capacity) == (16, 64), f"budget/capacity {t.budget}/{t.capacity}")
    want = spec.tlmo_default_streams(node)
    got = t.used()
    p.check(len(got) == len([s for s in t.streams if s[0] != 0]), "duplicate MIDs in the stream list")
    for row in t.streams:
        p.check(len(row) >= 3 and (row[2] == 1 if row[0] else row[1:] == (0, 0)),
                f"default stream {row} must have decimation 1 or an all-zero unused slot")
    p.check(t.profiles == ((16, (0, 1, 2, 3)), (8, (0, 1, 2, 2)), (4, (0, 1, 1, 1))),
            f"default profile budgets/class maps differ: {t.profiles}")
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    wrong = sorted(m for m in set(want) & set(got) if want[m] != got[m])
    p.check(not missing, f"streams missing: {[hex(m) for m in missing][:8]}")
    p.check(not extra, f"unexpected streams: {[hex(m) for m in extra][:8]}")
    p.check(not wrong, f"wrong classes: {[(hex(m), got[m], want[m]) for m in wrong][:8]}")
    return p.items


def judge_sfi_default(t: tb.SfiTable, node: int) -> list[str]:
    p = Problems()
    p.check((t.miss_limit, t.window, t.history) == (3, 5, 60), f"N/W/H {t.miss_limit}/{t.window}/{t.history}")
    p.check(tuple(t.apps) == spec.SFI_DEFAULT_APPS + ("",) * 8, f"apps {t.apps}")
    p.check(t.app_controls == ((1, 1, 5, 10),) * 8 + ((0, 0, 0, 0),) * 8, "default app enable/event-window controls differ")
    expected = []
    for index, app in enumerate(("STOR", "CMDI", "TLMO", "GW")):
        expected.append((spec.app_mid(app, "HK_TLM", node), 17, 1, 2, 2, 0, 0, 0, index, 1, 1))
    expected += [(spec.verif_mid(0xEC, node), 16, 4, 1, 3, 0, 0, 0, 255, 1, 1),
                 (spec.verif_mid(0xED, node), 16, 2, 2, 5, 0, 0, 0, 255, 1, 1),
                 (spec.verif_mid(0xEF, node), 16, 4, 3, 0, 10, 20, 3, 255, 2, 1)]
    expected += [(0,) * 11] * 9
    p.check(tuple(t.watches) == tuple(expected), f"expanded default watch rows differ: {t.watches}")
    p.check((t.peer_enabled, t.peer_timeout) == (1, 10), "default peer enable/timeout must be 1/10")
    return p.items


def judge_mode_default(body: bytes, node: int) -> list[str]:
    from dataclasses import replace
    from tables_sfi_mode import ModeTable, default_responses
    responses = tuple(replace(row, record_mid=spec.named_tlm_mid("CFE_EVS_HK_TLM_MID", node)) for row in default_responses())
    expected = ModeTable(responses=responses).pack()
    if body == expected:
        return []
    offsets = [i for i, (actual, want) in enumerate(zip(body, expected)) if actual != want]
    return [f"MODE default table differs from S7.1 pinned fields/zero spares at byte offsets {offsets[:16]}; size {len(body)}/{len(expected)}"]


def c_default_tables(ctx: "Ctx", app: str) -> None:
    p = Problems()
    for node in spec.NODES:
        cpu = f"cpu{node}"
        body = body_of(ctx, app, cpu)
        if app == "STOR":
            items = judge_stor_default(tb.StorTable.unpack(body), node)
        elif app == "CMDI":
            items = judge_cmdi_default(tb.CmdiTable.unpack(body))
        elif app == "TLMO":
            items = judge_tlmo_default(tb.TlmoTable.unpack(body), node)
        elif app == "GW":
            used = tb.gw_table_used_slots(body)
            items = [] if not used else [f"slots {used} are used; the default table has every slot unused"]
            if any(body):
                items.append("default GW connections/routes/spares must all be zero")
        elif app == "SFI":
            items = judge_sfi_default(tb.SfiTable.unpack(body), node)
        elif app == "MODE":
            items = judge_mode_default(body, node)
        else:
            raise AssertionError(app)
        for item in items:
            p.check(False, f"{cpu}: {item}")
    p.raise_if()


# ============================================================================ cmdval_lib self-containment (S3.2)

FORBIDDEN_LIB_SYMBOLS = re.compile(r"^(malloc|calloc|realloc|free|aligned_alloc|posix_memalign|SHA256.*|HMAC.*|EVP_.*|"
                                   r"OPENSSL_.*|CRYPTO_.*|gcry_.*|mbedtls_.*|wc_.*)$")


def c_cmdval_self_contained(ctx: "Ctx") -> None:
    ctx.build()
    so = ctx.tree / "build-native_std" / "exe" / "cpu1" / "cf" / "cmdval_lib.so"
    if not so.is_file():
        raise Fail("the build does not install /cf/cmdval_lib.so")
    try:
        proc = subprocess.run(["nm", "-D", "--undefined-only", str(so)], capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise InfraError("nm is not installed") from exc
    undefined = [ln.split()[-1].split("@")[0] for ln in proc.stdout.splitlines() if ln.strip()]
    bad = sorted({s for s in undefined if FORBIDDEN_LIB_SYMBOLS.match(s)})
    if bad:
        raise Fail(f"cmdval_lib.so uses {bad}")


def judge_mid_inventory(probe_out: str, node: int = 1) -> list[str]:
    """Audit full native MID values, preserving the command/telemetry distinction."""
    p = Problems()
    actual = {}
    for line in probe_out.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "MID":
            try:
                actual[fields[1]] = int(fields[2], 0)
            except ValueError:
                p.check(False, f"invalid native MID probe value: {line}")
    expected = {f"{app}_{name}_MID": spec.app_mid(app, name, node) for app, names in spec.APP_TOPICS.items() for name in names}
    expected.update(STOR_XFER_SEG_MID=0x1878, STOR_XFER_ACK_MID=0x1879,
                    SFI_HEALTH_MID=0x187A, MODE_PEER_MID=0x187B, GW_KEEPALIVE_MID=0x187C)
    for name, value in expected.items():
        p.check(actual.get(name) == value, f"native {name}={actual.get(name)!r}, expected full MID {value:#06x}")
        aliases = [other for other, got in actual.items() if other != name and got == value]
        p.check(not aliases, f"full MID collision: {name} {value:#06x} aliases {aliases}")
    reserved = {spec.verif_mid(topic, node) for topic in range(0xE0, 0xF0)}
    for name, value in actual.items():
        p.check(value not in reserved, f"production {name} uses reserved verification MID {value:#06x}")
    return p.items


def c_mid_inventory(ctx: "Ctx") -> None:
    """Probe candidate headers independently of its CCDD inventory on both native CPUs."""
    ctx.build()
    headers = []
    for base in (ctx.tree / "apps", ctx.tree / "cfe/modules"):
        for path in base.glob("*/config/default_*_msgids.h"):
            if any(part in ("ci_lab", "to_lab") for part in path.parts):
                continue
            headers.append(path)
    if not headers:
        raise Fail("no bundled native MID headers found for the independent inventory")
    include_names = sorted({path.name.removeprefix("default_") for path in headers})
    macros = sorted({name for path in headers for name in re.findall(r"^\s*#\s*define\s+(\w+_MID)\s+", strip_c_comments(read(path)), re.M)})
    source = '#include <stdio.h>\n#include "cfe.h"\n'
    source += "".join(f'#include "{name}"\n' for name in include_names)
    source += 'int main(void) {\n'
    source += "".join(f'printf("MID {name} 0x%04X\\n", (unsigned)({name}));\n' for name in macros)
    source += 'return 0; }\n'
    out = ctx.work / "native-mid-inventory"
    out.mkdir(parents=True, exist_ok=True)
    code = out / "mids.c"
    code.write_text(source)
    p = Problems()
    for node in (1, 2):
        cpu = f"cpu{node}"
        binary = out / cpu
        arguments = native_compiler_arguments(ctx.tree, cpu)
        proc = subprocess.run(["cc", "-std=c11", "-Werror", *arguments, str(code), "-o", str(binary)],
                              capture_output=True, text=True, timeout=ctx.timeout_for(120), cwd=out)
        if proc.returncode:
            raise Fail(f"{cpu} independent MID inventory could not compile bundled/mission headers: {proc.stderr[-1600:]}")
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise Fail(f"{cpu} independent MID inventory exited {result.returncode}: {result.stderr}")
        (out / f"{cpu}.txt").write_text(result.stdout)
        for item in judge_mid_inventory(result.stdout, node):
            p.check(False, f"{cpu}: {item}")
    p.raise_if()


def judge_mode_integration(tree: Path) -> list[str]:
    p = Problems()
    limits = "\n".join(strip_c_comments(read(path)) for base in (tree / "sample_defs", tree / "apps/sch_lab/config")
                       for path in base.rglob("*.h"))
    matches = re.findall(r"#\s*define\s+SCH_LAB_MISSION_MAX_SCHEDULE_ENTRIES\s+\(?\s*(\d+)", limits)
    p.check(bool(matches) and all(int(value) == 32 for value in matches),
            "SCH_LAB_MISSION_MAX_SCHEDULE_ENTRIES must remain 32")
    for cpu in ("cpu1", "cpu2"):
        for item in judge_install_custom(read(tree / "sample_defs" / cpu / "install_custom.cmake"), cpu):
            p.check(False, item)
    mode_src = "\n".join(read(path) for path in component_sources(tree, "mode"))
    p.check("MODE_AppMain" in mode_src and "SFI_HEARTBEAT_MID" in mode_src,
            "MODE must provide MODE_AppMain and publish the SFI heartbeat")
    sfi_table = read(tree / "apps/sfi/fsw/tables/sfi_tbl.c")
    p.check(re.search(r'"MODE"', strip_c_comments(sfi_table)) is not None,
            "SFI default table omits the real MODE heartbeat slot")
    return p.items


def c_mode_integration(ctx: "Ctx") -> None:
    ctx.copy_tree()
    items = judge_mode_integration(ctx.tree)
    if items:
        raise Fail("; ".join(items))


POOL_PROBE_SOURCE = r'''
#include <stdio.h>
#include <stddef.h>
#include "cfe.h"
#include "cfe_es_internal_cfg.h"
#include "cfe_sb_internal_cfg.h"
#include "cfe_sb_priv.h"
#include "cfe_es_generic_pool.h"
int main(void)
{
    const size_t blocks[] = {
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_01, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_02,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_03, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_04,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_05, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_06,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_07, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_08,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_09, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_10,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_11, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_12,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_13, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_14,
        CFE_PLATFORM_SB_MEM_BLOCK_SIZE_15, CFE_PLATFORM_SB_MEM_BLOCK_SIZE_16,
        CFE_PLATFORM_SB_MAX_BLOCK_SIZE
    };
    const size_t natural = _Alignof(CFE_ES_PoolAlign_t);
    const size_t align = natural > CFE_PLATFORM_ES_MEMPOOL_ALIGN_SIZE_MIN ? natural : CFE_PLATFORM_ES_MEMPOOL_ALIGN_SIZE_MIN;
    printf("POOL %zu %zu %zu %zu %zu\n", (size_t)CFE_PLATFORM_SB_BUF_MEMORY_BYTES,
           offsetof(CFE_SB_BufferD_t, Content), sizeof(CFE_ES_GenPoolBD_t), align,
           (size_t)CFE_PLATFORM_SB_POOL_MAX_BUCKETS);
    for (size_t index = 0; index < sizeof(blocks)/sizeof(blocks[0]); ++index) printf("BUCKET %zu\n", blocks[index]);
    return 0;
}
'''


def judge_sb_pool(probe: str, packet_sizes: tuple[int, ...]) -> tuple[list[str], dict[str, object]]:
    """Compute a necessary envelope bound using the flight allocator's actual bucket accounting."""
    p = Problems()
    pool = next((line.split()[1:] for line in probe.splitlines() if line.startswith("POOL ")), [])
    buckets = [int(line.split()[1]) for line in probe.splitlines() if line.startswith("BUCKET ")]
    if len(pool) != 5 or not buckets:
        return ["native SB pool probe omitted capacity, descriptor, allocator metadata, alignment or buckets"], {}
    capacity, descriptor, allocator, alignment, count = map(int, pool)
    p.check(count == len(buckets), f"configured {count} SB allocator buckets, native probe produced {len(buckets)}")
    p.check(alignment > 0 and alignment & (alignment - 1) == 0, f"invalid native pool alignment {alignment}")
    p.check(all(size > 0 for size in buckets), "SB pool contains a non-positive bucket size")
    p.check(bool(packet_sizes) and all(16 <= size <= 8192 for size in packet_sizes), f"expanded request packet lengths invalid: {packet_sizes}")
    if p.items:
        return p.items, {}
    requests = (1024,) * 8 + (max(packet_sizes),)
    tail = 0
    allocation_rows = []
    for payload in requests:
        candidates = [bucket for bucket in buckets if bucket >= payload + descriptor]
        if not candidates:
            p.check(False, f"no SB allocator bucket fits {payload}-byte packet plus {descriptor}-byte SB descriptor")
            continue
        bucket = min(candidates)
        tail = ((tail + allocator + alignment - 1) & ~(alignment - 1)) + bucket
        allocation_rows.append((payload, descriptor, allocator, bucket, tail))
    p.check(capacity >= tail, f"SB pool {capacity} bytes cannot hold declared eight <=1024-byte packets plus one expanded request: "
                             f"allocator-rounded minimum {tail}, before background traffic")
    return p.items, {"capacity": capacity, "descriptor_bytes": descriptor, "allocator_descriptor_bytes": allocator,
                     "alignment": alignment, "buckets": buckets, "request_packet_sizes": packet_sizes,
                     "minimum_envelope_bytes": tail, "allocations": allocation_rows,
                     "background_verification": "live SB allocation-failure counters/logs required; static lower bound alone is insufficient"}


def native_compiler_arguments(tree: Path, cpu: str) -> list[str]:
    candidates = [path for path in (tree / "build-native_std/native").rglob("flags.make")
                  if f"default_{cpu}" in path.parts and any("sb" in part.lower() for part in path.parts)
                  and "coverage" not in str(path)]
    if not candidates:
        raise Fail(f"cannot locate production SB compiler include/define flags for {cpu}")
    flags = read(sorted(candidates, key=lambda path: len(str(path)))[0])
    arguments = []
    for key in ("C_DEFINES", "C_INCLUDES"):
        match = re.search(r"^" + key + r"\s*=\s*(.*)$", flags, re.M)
        if match:
            arguments.extend(shlex.split(match.group(1)))
    for base in (tree / "apps", tree / "cfe/modules"):
        for pattern in ("*/fsw/inc", "*/fsw/src", "*/config"):
            for directory in sorted(base.glob(pattern)):
                arguments += ["-I", str(directory)]
    return arguments


def c_sb_pool_sizing(ctx: "Ctx") -> None:
    """Compile a review-owned native ABI/configuration probe; execute only during acceptance grading."""
    from ctf_checks import c_ccdd_layout
    ctx.build()
    if "ctf-layout-probe" not in ctx.cache:
        c_ccdd_layout(ctx)
    layouts = {}
    for line in ctx.cache["ctf-layout-probe"].splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "SIZE":
            layouts[fields[1]] = int(fields[2], 0)
    required = ("STOR_HkTlm_t", "SFI_StatusTlm_t", "TLMO_StreamTlm_t")
    missing = [name for name in required if name not in layouts]
    if missing:
        raise Fail(f"large-request layout evidence missing for {missing}")
    packet_sizes = tuple(layouts[name] for name in required)
    p = Problems()
    out = ctx.work / "native-pool-probe"
    out.mkdir(parents=True, exist_ok=True)
    source = out / "pool.c"
    source.write_text(POOL_PROBE_SOURCE)
    for cpu in ("cpu1", "cpu2"):
        arguments = native_compiler_arguments(ctx.tree, cpu)
        binary = out / f"pool-{cpu}"
        proc = subprocess.run(["cc", "-std=c11", "-Werror", *arguments, str(source), "-o", str(binary)],
                              capture_output=True, text=True, timeout=ctx.timeout_for(120), cwd=out)
        if proc.returncode:
            raise Fail(f"{cpu} native SB descriptor/configuration probe failed: {proc.stderr[-1500:]}")
        probe = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        if probe.returncode:
            raise Fail(f"{cpu} native SB descriptor/configuration probe exited {probe.returncode}: {probe.stderr}")
        items, evidence = judge_sb_pool(probe.stdout, packet_sizes)
        (out / f"{cpu}.json").write_text(json.dumps(evidence, indent=2))
        for item in items:
            p.check(False, f"{cpu}: {item}")
    p.raise_if()
