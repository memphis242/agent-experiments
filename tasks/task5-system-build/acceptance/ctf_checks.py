"""S8: the agent's CTF set-up, plugins, CCDD definitions and system test scripts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import spec
from common import Fail, PrereqFail, Problems, short
from node import find_node_pids

if TYPE_CHECKING:
    from check import Ctx

PACK = Path(__file__).resolve().parent
SCRIPT_LIMIT_S = 300
PLUGINS = {"cmdi_plugin": "CmdiPlugin", "tlm_order_plugin": "TlmOrderPlugin", "link_plugin": "LinkPlugin",
           "es_app_plugin": "EsAppPlugin", "node_file_plugin": "NodeFilePlugin", "stor_ground_plugin": "StorGroundPlugin"}
INSTRUCTIONS = ("SendCmdiCommand", "SendCmdiRawPacket", "StartTlmRecording", "CheckTlmOrder", "StopTlmRecording",
                "OpenUdpLink", "OpenTcpServer", "WaitLinkConnected", "OpenPtyPair", "SendLinkPacket", "SendLinkCmdiCommand",
                "CheckLinkPacket", "CheckNoLinkPacket", "CloseLink", "StopApp", "StartApp", "WaitForEvent",
                "CheckNodeFile", "CompareNodeFiles", "RemoveNodeFile", "ReplayCmdiFrame", "PrepareCmdiKeyTable",
                "ActivateCmdiKeyTable", "CheckTlmCounters", "OpenGwRelay", "SetGwRelayEnabled",
                "StartStorGroundCapture", "CheckStorGroundTransfer")
SMOKE = ("smoke_sample_app.json", "smoke_housekeeping.json")
SYSTEM = ("st_gw_commands.json", "st_cmdval_hmac.json", "st_tlmo_priority.json", "st_gw_udp.json", "st_gw_tcp.json",
          "st_gw_serial.json", "st_sfi_heartbeat.json", "st_stor_transfer.json", "st_mode_fault_response.json",
          "st_gw_failover.json", "st_cmdi_replay.json", "st_fault_log_transfer.json", "st_safe_mode_node_loss.json",
          "st_key_switch.json", "st_tlmo_profiles.json")
assert len(SMOKE + SYSTEM) == 17


class CtfEnv:
    """A fresh copy of the repository whose ctf/ is set up from scratch, shared by the S8 checks."""

    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx
        self.root = ctx.work / "s8-tree"
        self.ctf = self.root / "ctf"
        self.lock = threading.Lock()
        self.setup_done = False
        self.setup_ok = False
        self.setup_detail = ""
        self.diff1 = ""

    def copy(self) -> None:
        if self.root.exists():
            return
        skip = shutil.ignore_patterns(".git", "build-native_std", "__pycache__", "*.pyc")
        shutil.copytree(self.ctx.repo, self.root, ignore=skip, symlinks=True)
        for d in ("CTF", "venv", "results"):
            shutil.rmtree(self.ctf / d, ignore_errors=True)

    def run_setup(self, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.ctf / "setup_ctf.sh")], cwd=self.ctf, capture_output=True, text=True,
                              errors="replace", timeout=timeout, env=dict(os.environ))

    def ctf_diff(self) -> str:
        proc = subprocess.run(["git", "-C", str(self.ctf / "CTF"), "status", "--porcelain"], capture_output=True, text=True)
        diff = subprocess.run(["git", "-C", str(self.ctf / "CTF"), "diff"], capture_output=True, text=True)
        digest = hashlib.sha256((proc.stdout + diff.stdout).encode())
        for base in (self.ctf / "CTF", self.ctf / "venv"):
            for path in sorted(base.rglob("*")):
                if path.is_file() and ".git" not in path.relative_to(base).parts:
                    digest.update(str(path.relative_to(self.ctf)).encode())
                    digest.update(path.read_bytes())
                elif path.is_symlink():
                    digest.update(str(path.relative_to(self.ctf)).encode() + os.readlink(path).encode())
        return digest.hexdigest()

    def setup(self) -> None:
        """Run setup_ctf.sh on a fresh copy (once); raise PrereqFail when it fails."""
        with self.lock:
            if not self.setup_done:
                self.setup_done = True
                self.copy()
                if not (self.ctf / "setup_ctf.sh").is_file():
                    self.setup_detail = "ctf/setup_ctf.sh does not exist"
                else:
                    try:
                        proc = self.run_setup(self.ctx.timeout_for(900))
                        self.setup_ok = proc.returncode == 0 and (self.ctf / "venv" / "bin" / "python").is_file()
                        self.setup_detail = short(proc.stdout + proc.stderr, 1200)
                    except subprocess.TimeoutExpired:
                        self.setup_detail = "ctf/setup_ctf.sh did not finish within 900 s"
                if self.setup_ok:
                    self.diff1 = self.ctf_diff()
        if not self.setup_ok:
            raise PrereqFail("a fresh ctf/setup_ctf.sh failed: " + self.setup_detail)

    def env(self, run1: Path, run2: Path, results: Path) -> dict[str, str]:
        e = dict(os.environ, CFS_ROOT=str(self.root), CFS_RUN_DIR=str(run1), CFS_RUN_DIR_CPU2=str(run2),
                 CTF_RESULTS_DIR=str(results))
        e.pop("CTF_SKIP_PORT_CHECK", None)
        e.pop("CTF_ATTACH", None)
        return e


def ctf_env(ctx: "Ctx") -> CtfEnv:
    if ctx.ctf is None:
        ctx.ctf = CtfEnv(ctx)
    return ctx.ctf


# ============================================================================ set-up, layout

def c_setup_fresh(ctx: "Ctx") -> None:
    env = ctf_env(ctx)
    env.setup()
    p = Problems()
    patches = sorted((env.ctf / "shims" / "patches").glob("*")) if (env.ctf / "shims" / "patches").is_dir() else []
    for patch in patches:
        if patch.suffix not in (".patch", ".diff"):
            continue
        r = subprocess.run(["git", "-C", str(env.ctf / "CTF"), "apply", "--check", "--reverse", str(patch)],
                           capture_output=True, text=True)
        p.check(r.returncode == 0, f"{patch.name} is not applied to the fresh CTF checkout: {short(r.stderr, 200)}")
    try:
        again = env.run_setup(ctx.timeout_for(600))
        p.check(again.returncode == 0, f"a second run of ctf/setup_ctf.sh failed: {short(again.stdout + again.stderr, 400)}")
        p.check(env.ctf_diff() == env.diff1, "a second run of ctf/setup_ctf.sh changed the patched CTF tree (not idempotent)")
    except subprocess.TimeoutExpired:
        p.check(False, "a second run of ctf/setup_ctf.sh did not finish within 600 s")
    p.raise_if()


def judge_plugin_layout(ctf: Path, manifest: dict[str, list[str]] | None = None) -> list[str]:
    p = Problems()
    plugins = ctf / "plugins"
    p.check((plugins / "__init__.py").is_file(), "ctf/plugins/__init__.py does not exist (ctf/plugins must be a package)")
    for d, cls in PLUGINS.items():
        src = "\n".join(f.read_text(errors="replace") for f in (plugins / d).rglob("*.py")) if (plugins / d).is_dir() else ""
        p.check(bool(src), f"ctf/plugins/{d} does not exist")
        if src:
            p.check(re.search(r"class\s+" + cls + r"\b", src) is not None, f"ctf/plugins/{d} defines no class {cls}")
    ini = (ctf / "configs" / "mission.ini").read_text(errors="replace") if (ctf / "configs" / "mission.ini").is_file() else ""
    m = re.search(r"^\s*additional_plugins_path\s*=\s*(.*)$", ini, re.M)
    p.check(m is not None and "${CTF_PLUGINS_DIR}" in m.group(1), "configs/mission.ini does not load plugins from ${CTF_PLUGINS_DIR}")
    for sect, vals in (("cpu1", ("${CFS_RUN_DIR}", "core-cpu1", "1234", "2234", "set1")),
                       ("cpu2", ("${CFS_RUN_DIR_CPU2}", "core-cpu2", "1235", "2235", "set2"))):
        sm = re.search(r"^\[" + sect + r"\]\s*$(.*?)(?=^\[|\Z)", ini, re.M | re.S)
        if p.check(sm is not None, f"configs/mission.ini has no [{sect}] section"):
            assert sm is not None
            body = sm.group(1)
            for v in vals:
                p.check(v in body, f"[{sect}] does not set {v}")
            p.check("cmdi_key_hex" in body and "cmdi_source_id" in body, f"[{sect}] lacks cmdi_key_hex or cmdi_source_id")
    run = (ctf / "run_ctf.sh").read_text(errors="replace") if (ctf / "run_ctf.sh").is_file() else ""
    p.check("CTF_PLUGINS_DIR" in run, "run_ctf.sh does not export CTF_PLUGINS_DIR")
    p.check("CFS_RUN_DIR_CPU2" in run, "run_ctf.sh does not export CFS_RUN_DIR_CPU2")
    p.check("1235" in run, "run_ctf.sh does not check that UDP 1235 is free")
    readme = (ctf / "README.md").read_text(errors="replace") if (ctf / "README.md").is_file() else ""
    for d in PLUGINS:
        p.check(d in readme, f"ctf/README.md does not list the plugin {d}")
    patches = sorted((ctf / "shims" / "patches").glob("*")) if (ctf / "shims" / "patches").is_dir() else []
    for patch in patches:
        p.check(patch.name in readme, f"ctf/README.md does not list the patch {patch.name}")
    if manifest is not None:
        from static_checks import git_blob_sha
        for rel in ("ctf/shims/requirements.txt", "ctf/shims/constraints.txt"):
            f = ctf.parent / rel
            p.check(f.is_file() and git_blob_sha(f) == manifest[rel][1], f"{rel} was changed (no requirement may be added)")
    return p.items


def c_plugin_layout(ctx: "Ctx") -> None:
    ctx.copy_tree()
    from static_checks import load_manifest
    items = judge_plugin_layout(ctx.tree / "ctf", load_manifest())
    if items:
        raise Fail("; ".join(items))


def c_plugin_info(ctx: "Ctx") -> None:
    env = ctf_env(ctx)
    env.setup()
    out = ctx.work / "plugin-info"
    shutil.rmtree(out, ignore_errors=True)
    e = env.env(ctx.tree / "build-native_std" / "exe" / "cpu1", ctx.tree / "build-native_std" / "exe" / "cpu2",
                ctx.work / "ctf-results" / "plugininfo")
    e.update(CTF_CCDD_DIR=str(env.ctf / "ccdd" / "json"), CTF_HEADER_DIR=str(env.ctf / "headers"),
             CTF_PLUGINS_DIR=str(env.ctf / "plugins"), CTF_BUILD_CFS="false", CTF_BUILD_CMD="true",
             PATH=f"{env.ctf / 'shims' / 'bin'}:{os.environ.get('PATH', '')}")
    (ctx.work / "ctf-results" / "plugininfo").mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([str(env.ctf / "venv" / "bin" / "python"), "ctf", "--config_file",
                               str(env.ctf / "configs" / "mission.ini"), "--pluginInfo", str(out)],
                              cwd=env.ctf / "CTF", env=e, capture_output=True, text=True, errors="replace",
                              timeout=ctx.timeout_for(180))
    except subprocess.TimeoutExpired:
        raise Fail("ctf --pluginInfo did not finish within 180 s") from None
    names: set[str] = set()
    for f in sorted(out.glob("*.json")) if out.is_dir() else []:
        try:
            doc = json.loads(f.read_text())
        except ValueError:
            continue
        names |= {i.get("name") for i in doc.get("instructions", [])}
    missing = [i for i in INSTRUCTIONS if i not in names]
    if missing:
        raise Fail(f"ctf --pluginInfo (exit {proc.returncode}) does not list {missing}; {short(proc.stdout + proc.stderr, 500)}")


# ============================================================================ CCDD

REQUIRED_CCDD_TYPES = ("STOR_HkTlm_t", "STOR_ListTlm_t", "STOR_StatsTlm_t", "STOR_XferTlm_t", "STOR_XferSegCmd_t",
                       "STOR_XferAckCmd_t", "STOR_TransferCmd_t", "CMDI_HkTlm_t", "TLMO_HkTlm_t", "TLMO_ClassTlm_t",
                       "GW_HkTlm_t", "GW_ConnTlm_t", "SFI_HkTlm_t", "SFI_StatusTlm_t", "SFI_HeartbeatCmd_t",
                       "CFE_ES_StartAppCmd_t", "CFE_ES_StopAppCmd_t", "CFE_TBL_LoadCmd_t", "CFE_TBL_ValidateCmd_t",
                       "CFE_TBL_ActivateCmd_t", "STOR_FileInfoCmd_t", "STOR_FileInfoTlm_t", "CMDI_SourceTlm_t",
                       "TLMO_StreamTlm_t", "GW_KeepAliveCmd_t", "SFI_FaultCmd_t", "SFI_FaultTlm_t",
                       "SFI_HealthCmd_t", "MODE_HkTlm_t", "MODE_StatusTlm_t", "MODE_PeerCmd_t")


def required_mid_names() -> list[str]:
    out = []
    for app, mids in spec.APP_TOPICS.items():
        out += [f"{app}_{name}_MID" for name in mids]
    return out + ["STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID", "SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID"]


def judge_ccdd(json_dir: Path, probe_out: str, check_rc: int, check_out: str) -> list[str]:
    p = Problems()
    p.check(check_rc == 0 and re.search(r"\b0 mismatches", check_out) is not None,
            f"ccdd/tools/check_layout.py reports mismatches: {short(check_out, 600)}")
    probe = {"MID": {}, "SIZE": {}, "CC": {}}
    for line in probe_out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in probe:
            probe[parts[0]][parts[1]] = parts[2]
    msgs: dict[str, dict] = {}
    mids_doc: list = []
    for f in sorted(json_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except ValueError as exc:
            p.check(False, f"{f.name} is not valid JSON: {exc}")
            continue
        if isinstance(doc, list):
            mids_doc = doc
        elif "cmd_mid_name" in doc:
            msgs[doc["cmd_mid_name"]] = doc
        elif "tlm_mid_name" in doc:
            msgs[doc["tlm_mid_name"]] = doc
    for name in required_mid_names():
        p.check(name in msgs, f"no CCDD message definition for {name}")
        p.check(name in probe["MID"], f"layout_probe.c does not probe {name}")
    defined_types = set()
    for doc in msgs.values():
        if "cmd_codes" in doc:
            defined_types |= {c.get("cc_data_type") for c in doc["cmd_codes"]}
        else:
            defined_types.add(doc.get("tlm_data_type"))
    for t in REQUIRED_CCDD_TYPES:
        p.check(t in defined_types, f"no CCDD definition uses the type {t}")
        p.check(t in probe["SIZE"], f"layout_probe.c does not probe sizeof({t})")
    for app in spec.NEW_APPS:
        doc = msgs.get(f"{app}_CMD_MID", {})
        ccs = {c.get("cc_name"): int(str(c.get("cc_value")), 0) for c in doc.get("cmd_codes", [])}
        for name, value in spec.CC[app].items():
            p.check(ccs.get(f"{app}_{name}_CC") == value, f"{app}_CMD_MID lacks {app}_{name}_CC = {value}")
    es = msgs.get("CFE_ES_CMD_MID", {})
    ccs = {c.get("cc_name"): (int(str(c.get("cc_value")), 0), c.get("cc_data_type")) for c in es.get("cmd_codes", [])}
    p.check(ccs.get("CFE_ES_START_APP_CC", (None,))[0] == 4, "cfe_es_cmd.json lacks CFE_ES_START_APP_CC = 4")
    p.check(ccs.get("CFE_ES_STOP_APP_CC", (None,))[0] == 5, "cfe_es_cmd.json lacks CFE_ES_STOP_APP_CC = 5")
    tbl = msgs.get("CFE_TBL_CMD_MID", {})
    tbl_ccs = {c.get("cc_name"): int(str(c.get("cc_value")), 0) for c in tbl.get("cmd_codes", [])}
    for name, value in (("LOAD", 2), ("VALIDATE", 4), ("ACTIVATE", 5)):
        macro = f"CFE_TBL_{name}_CC"
        p.check(tbl_ccs.get(macro) == value, f"CCDD lacks native {macro}={value}")
        p.check(probe["CC"].get(macro) == str(value), f"layout_probe.c does not probe native {macro}={value}")
    for name in required_mid_names():
        if name in probe["MID"]:
            if name in ("STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID", "SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID"):
                want = (0x1878, 0x1879, 0x187A, 0x187B, 0x187C)[("STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID", "SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID").index(name)]
            else:
                app, rest = name.split("_", 1)
                want = spec.app_mid(app, rest.removesuffix("_MID"))
            try:
                p.check(int(probe["MID"][name], 0) == want, f"native {name} does not equal full MID {want:#06x}")
            except ValueError:
                p.check(False, f"native {name} is not numeric")
    targets = {e.get("target"): {m["mid_name"]: int(m["mid_value"], 0) for m in e.get("mids", [])} for e in mids_doc}
    set1, set2 = targets.get("set1", {}), targets.get("set2")
    if p.check(set2 is not None, "mission_mids.json has no target set2"):
        assert set2 is not None
        wrong = []
        for name, v1 in set1.items():
            want = v1 if name in ("STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID", "SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID") else (
                ((v1 & 0xFF00) + 0x100) | (v1 & 0xFF) if (v1 & 0xFF00) in (0x1800, 0x0800) else v1)
            if set2.get(name) != want:
                wrong.append(f"{name}={set2.get(name)} (expected {want:#06x})")
        p.check(not wrong, f"set2 MIDs missing or wrong: {wrong[:5]}")
    return p.items


def c_ccdd_layout(ctx: "Ctx") -> None:
    env = ctf_env(ctx)
    env.copy()
    ctx.build()
    tools = env.ctf / "ccdd" / "tools"
    if not (tools / "probe_layout.sh").is_file() or not (tools / "check_layout.py").is_file():
        raise Fail("ctf/ccdd/tools/probe_layout.sh or check_layout.py is missing")
    with ctx.tree_lock:
        try:
            probe = subprocess.run(["bash", str(tools / "probe_layout.sh"), str(ctx.tree)], capture_output=True, text=True,
                                   errors="replace", timeout=ctx.timeout_for(180), cwd=env.ctf)
        except subprocess.TimeoutExpired:
            raise Fail("probe_layout.sh did not finish within 180 s") from None
    if probe.returncode != 0:
        raise Fail(f"probe_layout.sh failed against the built tree: {short(probe.stderr, 800)}")
    chk = subprocess.run(["python3", str(tools / "check_layout.py"), str(env.ctf / "ccdd" / "json")], input=probe.stdout,
                         capture_output=True, text=True, errors="replace", timeout=120)
    ctx.cache["ctf-layout-probe"] = probe.stdout
    items = judge_ccdd(env.ctf / "ccdd" / "json", probe.stdout, chk.returncode, chk.stdout + chk.stderr)
    if items:
        raise Fail("; ".join(items))


# ============================================================================ scripts

def judge_script_static(text: str, name: str) -> list[str]:
    p = Problems()
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return [f"{name} is not valid JSON: {exc}"]
    flat = json.dumps(doc)
    p.check("SendCfsCommand" not in flat, f"{name} uses SendCfsCommand, which CMDI rejects")
    p.check("RegisterCfs" in flat and "ShutdownCfs" in flat, f"{name} does not register and shut down its own nodes")
    if name in SMOKE:
        p.check("TO_LAB" not in flat, f"{name} still enables TO_LAB")
    return p.items


def kill_nodes_under(base: Path) -> None:
    for cpu in (1, 2):
        for d in base.glob(f"*/cpu{cpu}"):
            for pid in find_node_pids(d, cpu):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def run_ctf_script(ctx: "Ctx", script: Path, label: str, side=None, limit: float = SCRIPT_LIMIT_S, extra_env: dict[str, str] | None = None) -> tuple[int, str]:  # noqa: ANN001
    """Run one script with run_ctf.sh against scratch copies of both installed processors."""
    env = ctf_env(ctx)
    env.setup()
    ctx.build()
    base = ctx.work / "ctf-run" / label
    shutil.rmtree(base, ignore_errors=True)
    run1, run2 = base / "cpu1", base / "cpu2"
    shutil.copytree(ctx.tree / "build-native_std" / "exe" / "cpu1", run1, symlinks=True)
    shutil.copytree(ctx.tree / "build-native_std" / "exe" / "cpu2", run2, symlinks=True)
    results = ctx.work / "ctf-results" / label
    results.mkdir(parents=True, exist_ok=True)
    ctx.shutdown_session()
    if ctx.queue_preflight is None or "inventory" not in ctx.queue_preflight:
        # Learn and allocation-probe the full installed inventory before the
        # script's own node lifetime and its 180-second completion clock.
        ctx.session((1, 2))
        ctx.shutdown_session()
    from preflight import namespace_preflight, process_inventory, verify_nodes
    namespace_preflight(2)
    stop = threading.Event()
    queue_errors: list[Exception] = []
    checked: set[tuple[str, int]] = set()
    def monitor_queues() -> None:
        seen: dict[tuple[str, int], float] = {}
        while not stop.wait(0.2):
            ready = []
            for cpu, run in ((1, run1), (2, run2)):
                for pid in find_node_pids(run, cpu):
                    key = (f"cpu{cpu}", pid)
                    seen.setdefault(key, time.monotonic())
                    if key not in checked:
                        try:
                            inventory = process_inventory(pid)
                        except Exception:
                            continue
                        if all(any(app in queue.name.upper() for queue in inventory) for app in ("TLMO", "MODE", "SFI")):
                            ready.append(key)
            if ready:
                try:
                    verify_nodes(tuple(ready))
                except Exception as exc:
                    queue_errors.append(exc)
                    return
                checked.update(ready)
    monitor = threading.Thread(target=monitor_queues, name="ctf-queue-preflight", daemon=True)
    monitor.start()
    th = None
    if side is not None:
        th = threading.Thread(target=side, args=(stop,), daemon=True)
        th.start()
    started = time.monotonic()
    run_env = env.env(run1, run2, results)
    run_env.update(extra_env or {})
    try:
        proc = subprocess.run(["bash", str(env.ctf / "run_ctf.sh"), str(script)], env=run_env,
                              capture_output=True, text=True, errors="replace", timeout=ctx.timeout_for(limit),
                              start_new_session=True)
        rc, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out = -1, f"did not finish within {limit} s; " + str(exc.stdout or "")[-500:]
    finally:
        stop.set()
        monitor.join(timeout=1)
        if th is not None:
            th.join(timeout=3)
        kill_nodes_under(ctx.work / "ctf-run")
    if queue_errors:
        raise queue_errors[0]
    ctx.cache["ctf-queue-checked-" + label] = tuple(checked)
    if not label.startswith("plugin-negative-"):
        started_targets = set()
        for ins in script_instructions(json.loads(script.read_text())):
            if ins["instruction"] == "StartCfs":
                targets = ins.get("data", {}).get("target", [])
                if isinstance(targets, str):
                    targets = [targets]
                started_targets.update("cpu1" if name == "mission" else name for name in targets)
        observed_targets = {name for name, _pid in checked}
        if started_targets - observed_targets:
            raise Fail(f"CTF nodes lack actual queue-capacity evidence: {sorted(started_targets - observed_targets)}")
    ctx.cache["ctf-elapsed-" + label] = time.monotonic() - started
    return rc, out


def c_script(ctx: "Ctx", name: str) -> None:
    script = ctf_env(ctx).root / "ctf" / "tests" / name
    ctf_env(ctx).copy()
    if not script.is_file():
        raise Fail(f"ctf/tests/{name} does not exist")
    p = Problems()
    for item in judge_script_static(script.read_text(errors="replace"), name):
        p.check(False, item)
    p.raise_if()
    rc, out = run_ctf_script(ctx, script, name.removesuffix(".json"))
    elapsed = ctx.cache["ctf-elapsed-" + name.removesuffix(".json")]
    p.check(elapsed <= 180, f"{name} took {elapsed:.2f} s; S8.5 requires completion within 180 s (grading kill limit remains 300 s)")
    p.check(rc == 0, f"run_ctf.sh {name} exited {rc}: {short(out, 900)}")
    p.raise_if()


def judge_run_system_tests(text: str) -> list[str]:
    p = Problems()
    pos = []
    for name in SMOKE + SYSTEM:
        i = text.find(name)
        p.check(i >= 0, f"run_system_tests.sh does not run {name}")
        pos.append(i)
    smoke_last = max(pos[:2]) if all(x >= 0 for x in pos[:2]) else -1
    sys_pos = [x for x in pos[2:]]
    if all(x >= 0 for x in sys_pos) and smoke_last >= 0:
        p.check(smoke_last < min(sys_pos), "run_system_tests.sh does not run the smoke scripts before the system tests")
        p.check(sys_pos == sorted(sys_pos), "run_system_tests.sh does not run the system tests in the order of S8.5")
    p.check("run_ctf.sh" in text, "run_system_tests.sh does not use run_ctf.sh")
    return p.items


def c_run_system_tests_script(ctx: "Ctx") -> None:
    ctx.copy_tree()
    f = ctx.tree / "ctf" / "run_system_tests.sh"
    if not f.is_file():
        raise Fail("ctf/run_system_tests.sh does not exist")
    items = judge_run_system_tests(f.read_text(errors="replace"))
    if items:
        raise Fail("; ".join(items))


LISTENER_PROBE = {
    "test_script_number": "ACC-LISTENER",
    "test_script_name": "acc_listener_probe.json",
    "owner": "review",
    "description": "Telemetry keeps being parsed while command-type and short datagrams arrive on the telemetry port.",
    "requirements": {},
    "ctf_options": {"verify_timeout": 14},
    "test_setup": "",
    "environment": {},
    "functions": {},
    "tests": [
        {"test_number": "ACC-1", "description": "boot", "instructions": [
            {"instruction": "RegisterCfs", "data": {"target": ["cpu1"]}, "wait": 1},
            {"instruction": "StartCfs", "data": {"target": ["cpu1"]}, "wait": 8}]},
        {"test_number": "ACC-2", "description": "fresh ES housekeeping is parsed", "instructions": [
            {"instruction": "ClearTlmPacket", "data": {"target": "cpu1", "mid": "CFE_ES_HK_TLM_MID"}, "wait": 0},
            {"instruction": "CheckTlmValue", "data": {"target": "cpu1", "mid": "CFE_ES_HK_TLM_MID", "args": [
                {"compare": ">=", "variable": "Payload.CommandCounter", "value": 0}]}, "wait": 0}]},
        {"test_number": "ACC-3", "description": "shut down", "instructions": [
            {"instruction": "ShutdownCfs", "data": {"target": "cpu1"}, "wait": 1}]},
    ],
}


def junk_sender(stop: threading.Event) -> None:
    """Command-type verification packets and 3-byte datagrams to UDP 2234 while the probe runs."""
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd = struct.pack(">HHH", spec.verif_mid(0xE8), 0xC000, 33) + bytes(34)
    while not stop.is_set():
        try:
            tx.sendto(cmd, (spec.HOST, 2234))
            tx.sendto(b"\x18\xe8\xc0", (spec.HOST, 2234))
        except OSError:
            pass
        time.sleep(0.05)
    tx.close()


def c_listener_probe(ctx: "Ctx") -> None:
    """Pilot probe: CTF's telemetry listener tolerates command-type and short datagrams on a telemetry port."""
    env = ctf_env(ctx)
    env.setup()
    script = ctx.work / "acc_listener_probe.json"
    script.write_text(json.dumps(LISTENER_PROBE, indent=2))
    rc, out = run_ctf_script(ctx, script, "listener-probe", side=junk_sender)
    if rc != 0:
        raise Fail(f"with command-type and short datagrams arriving on UDP 2234, CTF stopped parsing telemetry "
                   f"(run_ctf.sh exit {rc}): {short(out, 900)}")


def script_instructions(doc: object) -> list[dict]:
    """Collect executable instructions, excluding descriptions and requirement prose."""
    if isinstance(doc, list):
        return [item for child in doc for item in script_instructions(child)]
    if isinstance(doc, dict):
        if isinstance(doc.get("instruction"), str):
            return [doc]
        return [item for key, child in doc.items() if key in ("tests", "instructions", "functions", "cleanup")
                for item in script_instructions(child)]
    return []


def judge_script_content(text: str, name: str) -> list[str]:
    """Fail missing executable parts of S8.6; human review still assesses timing and meaning."""
    p = Problems()
    try:
        instructions = script_instructions(json.loads(text))
    except (ValueError, TypeError):
        return [f"{name}: invalid script JSON"]
    active = [{"instruction": i["instruction"], "data": i.get("data", {})} for i in instructions]
    flat = json.dumps(active)
    names = [i["instruction"] for i in active]
    sends = [i["data"] for i in active if i["instruction"] in ("SendCmdiCommand", "SendLinkCmdiCommand")]
    assertions = [i["data"] for i in active if i["instruction"] in ("CheckTlmValue", "CheckEvent", "WaitForEvent")]
    observed = json.dumps(assertions)
    def require(condition: bool, why: str) -> None:
        p.check(condition, f"{name}: {why}")
    def command(app: str, cc: str, number: int) -> list[dict]:
        return [d for d in sends if d.get("mid") == f"{app}_CMD_MID" and
                d.get("cc") in (number, f"{app}_{cc}_CC")]
    if name not in SYSTEM + SMOKE:
        return p.items
    require(bool(command("MODE", "SET_ENABLE", 2)), "must disable MODE before setup and await initial profile completion")
    require("Enabled" in observed and '"value": 0' in observed, "must observe MODE Enabled 0 through telemetry")
    require(not any(i["instruction"] in ("StartApp", "StopApp") and i["data"].get("app") == "SFI" for i in active),
            "may not restart SFI while MODE remains running")
    if name == "st_mode_fault_response.json":
        require(any(i["instruction"] == "StopApp" and i["data"].get("app") == "GW" for i in active), "must stop GW")
        require(not any(i["instruction"] == "StartApp" and i["data"].get("app") == "GW" for i in active), "manual GW START substitutes for automatic response")
        for term in ("RestartsSent", "Attempts", "BudgetPacketsPerCycle", "PurgeThresholdPercent", "Mode"):
            require(term in observed, f"missing telemetry assertion for {term}")
    if name in ("st_gw_failover.json", "st_safe_mode_node_loss.json"):
        relays = [i["data"] for i in active if i["instruction"] == "OpenGwRelay"]
        want = {(3201, 3101, 3202, 3102), (3211, 3111, 3212, 3112)}
        got = {tuple(d.get(k) for k in ("port_a", "node_a_port", "port_b", "node_b_port")) for d in relays}
        require(got == want, "requires the two exact transparent UDP relays/ports of S8.6")
        require(any(i["instruction"] == "SetGwRelayEnabled" and i["data"].get("enabled") is False for i in active), "requires relay cuts retaining sockets")
        require(any(i["instruction"] == "SetGwRelayEnabled" and i["data"].get("enabled") is True for i in active), "requires relay restoration")
        require('"cpu1"' in observed and '"cpu2"' in observed, "must assert both nodes' telemetry")
        for term in ("PeerLost", "RouteSelected", "Mode") if name == "st_gw_failover.json" else ("PeerLosses", "LinkFaults", "NodeLossFaults", "FaultReports", "ResponsesSent", "RestartsSent", "Transitions", "Attempts"):
            require(term in observed, f"missing integrated {term} observation")
        if name == "st_gw_failover.json":
            require("CheckTlmCounters" in names, "backup delivery must count exactly one unique packet per node")
        else:
            require(not any(i["instruction"] in ("StopApp", "StartApp") for i in active), "SAFE/node loss must use relay cuts with apps running")
            require(bool(command("SFI", "ACK_FAULT", 6)), "must acknowledge inactive latched faults")
            require(len(command("MODE", "SET_MODE", 3)) >= 3, "must exercise guarded SAFE exits")
            require("SelectedProfile" in observed and "PurgeThresholdPercent" in observed, "must inspect actual profile targets")
    if name in ("st_cmdi_replay.json", "st_key_switch.json"):
        require("ReplayCmdiFrame" in names and '"save_frame"' in flat, "must replay saved identical encoded bytes")
        for term in ("ReplayErrors", "HmacErrors", "CommandCounter") if name == "st_key_switch.json" else ("ReplayErrors", "CommandCounter"):
            require(term in observed, f"missing {term} rejection/non-execution assertion")
        require('"source_id": 1' in flat and '"source_id": 2' in flat, "stimulus and observation sources must be separated")
    if name == "st_fault_log_transfer.json":
        require("StartStorGroundCapture" in names and "CheckStorGroundTransfer" in names, "must independently reassemble raw transfer and fault records")
        require(any(i["instruction"] == "CheckStorGroundTransfer" and i["data"].get("fault_log") is True for i in active), "must enable exact binary fault-log validation")
        require(bool(command("STOR", "FILE_INFO", 14)), "must request FILE_INFO CRC/size")
        require("RemoveNodeFile" in names and bool(command("SFI", "ACK_FAULT", 6)), "must clean dump and latch")
    if name == "st_key_switch.json":
        require(names.count("PrepareCmdiKeyTable") >= 2 and names.count("ActivateCmdiKeyTable") >= 2, "requires two prebuilt native staged key images and authenticated activations")
        require(any(i["instruction"] == "SendLinkCmdiCommand" and i["data"].get("mid") == "CMDI_CMD_MID" for i in active), "second switch must enter through GW")
        require("RemoveNodeFile" in names and "CloseLink" in names, "must remove prepared images and close the key link")
        for term in ("KeySwitches", "NextValid", "FramesAccepted", "FramesReceived", "ValTableShared", "RxFrames", "RxPackets"):
            require(term in observed, f"missing exact key-switch observation {term}")
    if name == "st_tlmo_profiles.json":
        require(not command("TLMO", "SELECT_PROFILE", 8), "profiles must be driven by MODE")
        require(bool(command("MODE", "SET_MODE", 3)) and bool(command("MODE", "APPLY_PROFILE", 7)), "requires MODE transitions and profile reapplication")
        counters = [i["data"].get("values") for i in active if i["instruction"] == "CheckTlmCounters"]
        for values in ([1, 3, 5, 7, 9, 11], [1, 4, 7, 10], [1, 3, 5, 7, 9, 11, 13, 15, 17], [1, 4, 7, 10, 13, 16]):
            require(values in counters, f"missing exact raw counter sequence {values}")
        for term in ("StreamCount", "PacketsDecimated", "BudgetPacketsPerCycle", "Decimation", "Decimated", "Queued", "SendErrors"):
            require(term in observed, f"missing full stream/profile assertion {term}")
        require("StopTlmRecording" in names, "must close raw recording")
    return p.items


def c_script_content(ctx: "Ctx") -> None:
    ctx.copy_tree()
    p = Problems()
    for name in SMOKE + SYSTEM:
        path = ctx.tree / "ctf/tests" / name
        if p.check(path.is_file(), f"ctf/tests/{name} is absent"):
            for item in judge_script_content(path.read_text(errors="replace"), name):
                p.check(False, item)
    p.raise_if()


def c_plugin_negative(ctx: "Ctx") -> None:
    """Execute bad inputs and timeouts against production plugins through CTF dispatch."""
    env = ctf_env(ctx)
    env.setup()
    bad_key = {"target": "cpu1", "path": "cf/negative.tbl", "active_key_hex": "01" * 32, "next_key_hex": "02" * 32}
    vectors = [(name, {}, []) for name in INSTRUCTIONS]
    for override in ({"active_key_hex": "zz"}, {"next_key_hex": "00" * 32}, {"next_key_hex": "01" * 32},
                     {"path": "../escape.tbl"}, {"path": "/tmp/escape.tbl"}):
        vectors.append(("PrepareCmdiKeyTable", dict(bad_key, **override), []))
    vectors += [("WaitLinkConnected", {"name": "timeout", "timeout": 0.05},
                 [{"instruction": "OpenTcpServer", "data": {"name": "timeout", "port": 3349}, "wait": 0}]),
                ("CheckLinkPacket", {"name": "timeout", "mid": 0x18E7, "target": "cpu1", "timeout": 0.05},
                 [{"instruction": "OpenUdpLink", "data": {"name": "timeout", "local_port": 3349, "remote_port": 3350}, "wait": 0}]),
                ("CheckTlmCounters", {"target": "cpu1", "name": "absent", "mid": 0x18EA, "counter_offset": 16, "values": [1]}, [])]
    p = Problems()
    for number, (instruction, data, setup) in enumerate(vectors):
        doc = dict(LISTENER_PROBE)
        doc["test_script_name"] = f"negative_{number}.json"
        doc["tests"] = [{"test_number": "NEG", "description": f"reject {instruction}", "instructions":
                         [{"instruction": "RegisterCfs", "data": {"target": ["cpu1"]}, "wait": 0}] + setup +
                         [{"instruction": instruction, "data": data, "wait": 0},
                          {"instruction": "ShutdownCfs", "data": {"target": "cpu1"}, "wait": 0}]}]
        script = ctx.work / f"ctf-negative-{number}.json"
        script.write_text(json.dumps(doc))
        rc, out = run_ctf_script(ctx, script, f"plugin-negative-{number}", limit=15)
        p.check(rc > 0, f"{instruction} accepted invalid arguments or an unsatisfied timeout: {data}")
        p.check(ctx.cache["ctf-elapsed-" + f"plugin-negative-{number}"] <= 18, f"{instruction} negative path exceeded 15 s plus cleanup")
        p.check("Traceback (most recent call last)" not in out, f"{instruction} raised out of the plugin instead of returning failure: {short(out, 500)}")
        p.check(re.search(r"error|invalid|fail|timeout|timed out|missing|unknown|not found", out, re.I) is not None,
                f"{instruction} failed without an actionable logged reason: {short(out, 300)}")
    p.raise_if()


# Each pinned script has a distinct callable for registration and feedback.
def c_smoke_sample_app(ctx: "Ctx") -> None: c_script(ctx, SMOKE[0])
def c_smoke_housekeeping(ctx: "Ctx") -> None: c_script(ctx, SMOKE[1])
def c_gw_commands(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[0])
def c_cmdval_hmac(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[1])
def c_tlmo_priority(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[2])
def c_gw_udp(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[3])
def c_gw_tcp(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[4])
def c_gw_serial(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[5])
def c_sfi_heartbeat(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[6])
def c_stor_transfer(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[7])
def c_mode_fault_response(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[8])
def c_gw_failover(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[9])
def c_cmdi_replay(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[10])
def c_fault_log_transfer(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[11])
def c_safe_mode_node_loss(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[12])
def c_key_switch(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[13])
def c_tlmo_profiles(ctx: "Ctx") -> None: c_script(ctx, SYSTEM[14])


PLUGIN_FAULT_SHIM = r'''
import json, os, socket, struct
_trace = os.environ.get("T5_PLUGIN_SEND_TRACE")
_fault = os.environ.get("T5_PLUGIN_RAW_FAULT")
_send = socket.socket.sendto
_recv = socket.socket.recvfrom

def _sendto(self, data, *args, **kwargs):
    result = _send(self, data, *args, **kwargs)
    if _trace and len(data) >= 50 and data[:2] == b"CV":
        destination = args[-1] if args else kwargs.get("address")
        if isinstance(destination, tuple) and len(destination) == 2:
            row = {"port": destination[1], "source": struct.unpack_from(">H", data, 4)[0],
                   "sequence": struct.unpack_from(">I", data, 12)[0], "frame": data.hex()}
            with open(_trace, "a", encoding="utf-8") as stream: stream.write(json.dumps(row) + "\n")
    return result

def _recvfrom(self, *args, **kwargs):
    if _fault and os.path.exists(_fault) and self.getsockname()[1] == 2234:
        raise OSError("review injected raw telemetry receive failure")
    return _recv(self, *args, **kwargs)

socket.socket.sendto = _sendto
socket.socket.recvfrom = _recvfrom
'''


def plugin_tlm(mid: int, payload: bytes) -> bytes:
    """Review-owned telemetry packets for plugin boundary tests, never sent onto SB."""
    header = struct.pack(">HHHI", mid, 0xC000, 16 + len(payload) - 7, 1000000) + bytes(6)
    assert len(header) == 16
    return header + payload


def plugin_transfer_packets(content: bytes, *, transfer: int = 999, corrupt_crc: bool = False) -> list[bytes]:
    import zlib
    count = max(1, (len(content) + 511) // 512)
    crc = (zlib.crc32(content) ^ int(corrupt_crc)) & 0xFFFFFFFF
    out = []
    for index in range(count):
        block = content[index * 512:(index + 1) * 512]
        body = struct.pack(spec.XFER_SEG_FMT, transfer, 1, 0, 0, 0, index, count,
                           len(content), crc, index * 512, len(block), 0,
                           b"review/ground.bin".ljust(64, b"\0"), block.ljust(512, b"\0"))
        out.append(plugin_tlm(spec.app_mid("STOR", "XFER_TLM"), body))
    return out


def plugin_serial_errors(trace: list[dict]) -> list[str]:
    """Audit the actual bytes sent by every Python command producer in one CTF process."""
    histories: dict[tuple[int, int], int] = {}
    errors = []
    for row in trace:
        frame = bytes.fromhex(row["frame"])
        source = struct.unpack_from(">H", frame, 4)[0]
        sequence = struct.unpack_from(">I", frame, 12)[0]
        node = 2 if row["port"] == 1235 else 1
        key = node, source
        previous = histories.get(key)
        if previous is not None and not 0 < ((sequence - previous) & 0xFFFFFFFF) < 0x80000000:
            errors.append(f"cpu{node}/source{source} serial {sequence} did not advance after {previous}")
        histories[key] = sequence
    return errors


def c_plugin_adversarial(ctx: "Ctx") -> None:
    """Production CTF dispatch plus independent raw-tap and native-image fault fixtures."""
    import zlib
    import tables as tb
    env = ctf_env(ctx)
    env.setup()
    directory = ctx.work / "plugin-adversarial"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(PLUGIN_FAULT_SHIM)
    p = Problems()
    def ins(op: str, **data) -> dict:
        return {"instruction": op, "data": data, "wait": 0}
    def execute(label: str, instructions: list[dict], *, success: bool = False, side=None,
                extra: dict[str, str] | None = None, limit: float = 25) -> str:
        doc = dict(LISTENER_PROBE)
        doc["tests"] = [{"test_number": label, "description": "review-owned plugin contract vector", "instructions":
                          [ins("RegisterCfs", target=["cpu1"])] + instructions + [ins("ShutdownCfs", target="cpu1")]}]
        path = directory / f"{label}.json"
        path.write_text(json.dumps(doc))
        rc, output = run_ctf_script(ctx, path, "plugin-negative-" + label, side=side, limit=limit, extra_env=extra)
        p.check(rc == 0 if success else rc > 0,
                f"plugin adversarial {label}: expected {'success' if success else 'bounded rejection'}, got {rc}: {short(output, 500)}")
        p.check("Traceback (most recent call last)" not in output, f"plugin adversarial {label} escaped with an exception: {short(output, 400)}")
        if not success:
            p.check(re.search(r"error|invalid|fail|timeout|missing|duplicate|mismatch|short|closed|receive", output, re.I) is not None,
                    f"plugin adversarial {label} omitted a logged reason")
        return output
    def boot() -> list[dict]:
        return [dict(ins("StartCfs", target=["cpu1"]), wait=8),
                dict(ins("SendCmdiCommand", target="cpu1", mid="MODE_CMD_MID", cc="MODE_SET_ENABLE_CC",
                         args={"Payload": {"Enabled": 0, "Spare": [0, 0, 0]}}, source_id=2), wait=2)]
    trigger_path = "cf/adversarial_trigger.tbl"
    trigger_data = dict(target="cpu1", path=trigger_path,
                        active_key_hex=b"cFS-cmdval-test-key-0123456789ab".hex(), next_key_hex="01" * 32)
    def captured(label: str, operation: str, name: str) -> list[dict]:
        return [ins("PrepareCmdiKeyTable", **trigger_data)] + boot() + [ins(operation, target="cpu1", name=name),
                dict(ins("RemoveNodeFile", target="cpu1", path=trigger_path), wait=1)]
    def sender(label: str, packets: list[bytes], *, marker: Path | None = None):
        def send(stop: threading.Event) -> None:
            trigger = ctx.work / "ctf-run" / ("plugin-negative-" + label) / "cpu1" / trigger_path
            seen = False
            while not stop.wait(0.02):
                if trigger.is_file(): seen = True
                elif seen: break
            if stop.is_set(): return
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for packet in packets:
                    sock.sendto(packet, (spec.HOST, 2234))
                    if stop.wait(0.1):
                        return
            if marker is not None and not stop.wait(2.5):
                marker.touch()
        return send
    counters = lambda values: ins("CheckTlmCounters", target="cpu1", name="adversarial", mid=0x18EA,
                                  counter_offset=16, values=values)
    def counter_packet(value: int, short: bool = False) -> bytes:
        return (struct.pack(">HHH", 0x18EA, 0xC000, 1) + bytes(2)) if short else \
            fr_packet(value)
    def fr_packet(value: int) -> bytes:
        from frames import build_verification
        return build_verification(0x18EA, 20, counter=value)
    counter_cases = (("counters-valid", [1, 2], [1, 2], False),
                     ("counters-missing", [1], [1, 2], False),
                     ("counters-extra", [1, 2, 3], [1, 2], False),
                     ("counters-order", [2, 1], [1, 2], False),
                     ("counters-duplicate", [1, 1, 2], [1, 2], False),
                     ("counters-short", [1], [1], True))
    for label, actual, expected, short_packet in counter_cases:
        instructions = captured(label, "StartTlmRecording", "adversarial") + [counters(expected)]
        execute(label, instructions, success=label == "counters-valid",
                side=sender(label, [counter_packet(value, short_packet) for value in actual]))
    marker = directory / "raw-failure"
    marker.unlink(missing_ok=True)
    instructions = captured("counters-raw-error", "StartTlmRecording", "adversarial") + [dict(counters([1, 2]), wait=3), counters([1, 2])]
    output = execute("counters-raw-error", instructions, side=sender("counters-raw-error", [counter_packet(1), counter_packet(2)], marker=marker),
                    extra={"PYTHONPATH": str(directory), "T5_PLUGIN_RAW_FAULT": str(marker)})
    p.check(re.search(r"raw|tap|listener|receive|socket", output, re.I) is not None,
            "CheckTlmCounters raw I/O error rejection must identify the raw tap failure")
    content = b"independent raw ground-transfer fixture"
    valid = plugin_transfer_packets(content)
    transfers = (("transfer-valid", valid, False),
                 ("transfer-crc", plugin_transfer_packets(content, corrupt_crc=True), False),
                 ("transfer-missing", plugin_transfer_packets(bytes(700))[:1], False),
                 ("transfer-duplicate", valid * 2, False),
                 ("transfer-malformed-log", plugin_transfer_packets(b"malformed fault log"), True))
    for label, packets, fault_log in transfers:
        instructions = captured(label, "StartStorGroundCapture", "ground_adversarial") + [ins("CheckStorGroundTransfer", target="cpu1", name="ground_adversarial", timeout=3,
                                     **({"fault_log": True} if fault_log else {"expected_hex": content.hex()}))]
        execute(label, instructions, success=label == "transfer-valid", side=sender(label, packets))
        artifacts = list((ctx.work / "ctf-results" / ("plugin-negative-" + label)).rglob("ground_adversarial.bin"))
        if label == "transfer-valid":
            p.check(len(artifacts) == 1 and artifacts[0].read_bytes() == content,
                    "StartStorGroundCapture did not save the exact independently reconstructed ground artifact")
        else:
            p.check(not artifacts, f"{label} left a success artifact for malformed/incomplete transfer")
    key_data = dict(target="cpu1", path="cf/review_key.tbl", active_key_hex=spec.DEFAULT_KEY.hex() if hasattr(spec, "DEFAULT_KEY") else
                    b"cFS-cmdval-test-key-0123456789ab".hex(), next_key_hex="01" * 32)
    execute("key-existing", [ins("PrepareCmdiKeyTable", **dict(key_data, path="cf/cmdi_tbl.tbl"))])
    execute("key-running", boot() + [ins("PrepareCmdiKeyTable", **key_data)])
    execute("key-native-image", [ins("PrepareCmdiKeyTable", **key_data)], success=True)
    run = ctx.work / "ctf-run/plugin-negative-key-native-image/cpu1"
    prepared = run / key_data["path"]
    if p.check(prepared.is_file(), "PrepareCmdiKeyTable omitted native table image"):
        try:
            header, body = tb.split_image(prepared.read_bytes(), "CMDI")
            default_header, default_body = tb.split_image((run / "cf/cmdi_tbl.tbl").read_bytes(), "CMDI")
            expected = tb.CmdiTable(next_key=bytes([1]) * 32, next_valid=1).pack()
            p.check(body == expected, "prepared key image has wrong native ACTIVE/NEXT/defaults/padding layout")
            p.check(header[:8] == default_header[:8] and len(header) == len(default_header), "prepared key table has wrong native cFE file header")
            p.check(default_body == tb.CmdiTable().pack(), "PrepareCmdiKeyTable rewrote the default disk table")
        except tb.TableImageError as exc:
            p.check(False, f"PrepareCmdiKeyTable image is not a native CMDI.ValTable image: {exc}")
        prepared.unlink()
    activate = lambda timeout: ins("ActivateCmdiKeyTable", target="cpu1", path=key_data["path"],
                                   key_hex=key_data["active_key_hex"], source_id=2, timeout=timeout)
    execute("key-activate-timeout", [ins("PrepareCmdiKeyTable", **key_data)] + boot() +
            [dict(activate(0.2), data=dict(activate(0.2)["data"], key_hex="ff" * 32))])
    corruption_errors = []
    def corrupt_prepared(stop: threading.Event) -> None:
        path = ctx.work / "ctf-run/plugin-negative-key-activate-rejection/cpu1" / key_data["path"]
        while not path.is_file():
            if stop.wait(0.02): return
        try:
            raw = path.read_bytes()
            _header, body = tb.split_image(raw, "CMDI")
            damaged = bytearray(body); damaged[88] = 2
            path.write_bytes(tb.with_body(raw, "CMDI", bytes(damaged)))
        except (OSError, tb.TableImageError) as exc:
            corruption_errors.append(str(exc))
    execute("key-activate-rejection", [dict(ins("PrepareCmdiKeyTable", **key_data), wait=2)] + boot() + [activate(3)],
            side=corrupt_prepared)
    p.check(not corruption_errors, f"review could not prepare native TBL validation rejection fixture: {corruption_errors}")
    # Interpose only on the CTF process's send calls to compare actual shared serials.
    trace_path = directory / "send-trace.jsonl"
    trace_path.unlink(missing_ok=True)
    serial_instructions = [ins("PrepareCmdiKeyTable", **key_data)] + boot() + [activate(10),
        ins("SendCmdiCommand", target="cpu1", mid="SAMPLE_APP_CMD_MID", cc="SAMPLE_APP_NOOP_CC", args={}, source_id=1, sequence=100),
        ins("StopApp", target="cpu1", app="GW"),
        ins("StartApp", target="cpu1", app="GW", entry="GW_AppMain", file="/cf/gw.so", priority=70, stack_size=131072),
        ins("OpenUdpLink", name="serials", local_port=3361, remote_port=3362),
        ins("SendCmdiCommand", target="cpu1", mid="GW_CMD_MID", cc="GW_ADD_CONN_CC", args={"Payload": {"Index": 0, "Spare": [0, 0, 0], "Conn": {
            "Interface": 1, "Enabled": 1, "LocalPort": 3362, "RemoteAddr": "127.0.0.1", "RemotePort": 3361,
            "Spare": 0, "DevicePath": "", "KeepAliveEnabled": 0, "PeerNode": 0, "Spare2": 0, "Subs": [0] * 16}}}, source_id=1),
        ins("SendLinkCmdiCommand", name="serials", target="cpu1", mid="SAMPLE_APP_CMD_MID", cc="SAMPLE_APP_NOOP_CC", args={}, source_id=1),
        ins("SendCmdiCommand", target="cpu1", mid="SAMPLE_APP_CMD_MID", cc="SAMPLE_APP_NOOP_CC", args={}, source_id=1),
        ins("CloseLink", name="serials"), ins("RemoveNodeFile", target="cpu1", path=key_data["path"])]
    execute("shared-serials", serial_instructions, success=True, limit=40,
            extra={"PYTHONPATH": str(directory), "T5_PLUGIN_SEND_TRACE": str(trace_path)})
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()] if trace_path.is_file() else []
    p.check(len(trace) >= 10 and any(row["port"] == 3362 for row in trace), "shared serial probe did not observe cmdi/ES/link/native-TBL producers")
    for error in plugin_serial_errors(trace): p.check(False, error)
    p.raise_if()
