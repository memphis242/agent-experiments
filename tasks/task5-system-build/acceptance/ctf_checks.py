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
            data = ins.get("data", {})
            instruction = ins["instruction"]
            if instruction == "AcceptanceDispatch" and data.get("instruction") == "StartCfs":
                data = data.get("data", {})
                instruction = "StartCfs"
            if instruction == "StartCfs":
                targets = data.get("target", [])
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


def c_plugin_adversarial(ctx: "Ctx") -> None:
    """Drive adversarial plugin boundary cases through production CTF dispatch."""
    from ctf_adversarial import ADAPTER_SOURCE, adversarial_vectors, judge_adversarial_outcome

    env = ctf_env(ctx)
    env.setup()
    adapter = env.ctf / "plugins" / "acceptance_dispatch" / "acceptance_dispatch_plugin.py"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    (adapter.parent / "__init__.py").write_text("")
    adapter.write_text(ADAPTER_SOURCE.replace("__ACCEPTANCE_PACK__", repr(str(PACK))))
    p = Problems()
    vectors = [{"id": f"missing-required-{name}", "instruction": name, "data": {}, "setup": [],
                "expected": False} for name in INSTRUCTIONS] + adversarial_vectors()
    for number, vector in enumerate(vectors):
        doc = dict(LISTENER_PROBE)
        doc["test_script_name"] = f"adversarial_{number}.json"
        def dispatch(case_id, instruction, data, expected, wait=0, inject_hex=None):
            return {"instruction": "AcceptanceDispatch", "data": {"case_id": case_id,
                    "instruction": instruction, "data": data, "expected": expected,
                    "inject_hex": inject_hex or []}, "wait": wait}
        instructions = [{"instruction": "RegisterCfs", "data": {"target": ["cpu1"]}, "wait": 0}]
        setup_ids = []
        if vector.get("prepare_before_start"):
            for i, item in enumerate(vector["setup"]):
                setup_id = f"{vector['id']}-setup-{i}"
                setup_ids.append((setup_id, item["instruction"]))
                instructions.append(dispatch(setup_id, item["instruction"], item["data"], True))
        if (vector.get("start_before") or vector.get("native_probe") or
                any(item["instruction"] == "StartTlmRecording" for item in vector["setup"])):
            start_id = f"{vector['id']}-start-cfs"
            instructions.append(dispatch(start_id, "StartCfs", {"target": ["cpu1"]}, True, wait=3))
            setup_ids.append((start_id, "StartCfs"))
        if not vector.get("prepare_before_start"):
            for i, item in enumerate(vector["setup"]):
                setup_id = f"{vector['id']}-setup-{i}"
                setup_ids.append((setup_id, item["instruction"]))
                instructions.append(dispatch(setup_id, item["instruction"], item["data"], True,
                                              inject_hex=[packet.hex() for packet in item.get("packets", [])]))
        instructions.append(dispatch(vector["id"], vector["instruction"], vector["data"], vector["expected"],
                                     vector.get("stimulus_wait", 0),
                                     [packet.hex() for packet in vector.get("packets", [])]))
        if (any(item["instruction"] == "StartTlmRecording" for item in vector["setup"]) or
                vector.get("start_before") or vector.get("native_probe")):
            instructions.append({"instruction": "ShutdownCfs", "data": {"target": "cpu1"}, "wait": 1})
        doc["tests"] = [{"test_number": vector["id"], "description": vector["id"], "instructions": instructions}]
        script = ctx.work / f"ctf-adversarial-{number}.json"
        script.write_text(json.dumps(doc))
        rc, out = run_ctf_script(ctx, script, f"plugin-adversarial-{number}", limit=20)
        elapsed = ctx.cache["ctf-elapsed-plugin-adversarial-" + str(number)]
        p.check(elapsed <= 23, f"{vector['id']} exceeded its timeout plus cleanup")
        p.check(rc == 0, f"{vector['id']} CTF process aborted with exit {rc}: {short(out, 700)}")
        p.check("Traceback (most recent call last)" not in out,
                f"{vector['id']} raised out of the plugin: {short(out, 500)}")
        p.check(f"ADVERSARIAL_EXCEPTION|{vector['id']}|" not in out,
                f"{vector['id']} raised through CTF dispatch: {short(out, 500)}")
        for setup_id, setup_instruction in setup_ids:
            p.check(judge_adversarial_outcome(out, setup_id, setup_instruction, True),
                    f"{setup_id} did not succeed through public dispatch: {short(out, 500)}")
        p.check(judge_adversarial_outcome(out, vector["id"], vector["instruction"], vector["expected"]),
                f"{vector['id']} did not report the expected instruction-level outcome (process exit {rc}): {short(out, 500)}")
        if vector["expected"] is False:
            p.check(any(line.startswith(f"ADVERSARIAL_LOG|{vector['id']}|") for line in out.splitlines()),
                    f"{vector['id']} returned False without a scoped actionable plugin log: {short(out, 500)}")
        if vector["instruction"] == "CheckStorGroundTransfer" and isinstance(vector["data"].get("name"), str):
            artifact_state = bool(vector["expected"])
            p.check(f"ADVERSARIAL_STOR_ARTIFACT|{vector['id']}|{artifact_state}" in out,
                    f"{vector['id']} artifact state did not match its instruction result: {short(out, 500)}")
            if vector["expected"] is True:
                p.check(f"ADVERSARIAL_STOR_SUCCESS|{vector['id']}|True|True" in out,
                        f"{vector['id']} did not create byte-exact output and matching size/CRC variables: {short(out, 600)}")
        prepare_state = vector["data"].get("_acceptance_prepare_state")
        if prepare_state is not None:
            p.check(f"ADVERSARIAL_PREPARE_STATE|{vector['id']}|{prepare_state}|True" in out,
                    f"{vector['id']} changed or created a prepared image after rejection: {short(out, 500)}")
        if vector.get("require_tap_fault_marker"):
            p.check(f"ADVERSARIAL_TAP_REACHED|{vector['id']}" in out,
                    f"{vector['id']} did not reach the injected telemetry tap failure: {short(out, 500)}")
        if vector.get("native_probe"):
            reports = [line.split("|", 2)[2] for line in out.splitlines()
                       if line.startswith(f"NATIVE_PROBE_REPORT|{vector['id']}|")]
            try:
                report = json.loads(reports[0]) if len(reports) == 1 else {}
            except ValueError:
                report = {}
            p.check(report.get("ok") is True and len(report.get("cases", [])) == 5 and not report.get("errors"),
                    f"{vector['id']} did not complete the five native activation cases: {short(out, 900)}")
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
