"""Public-dispatch probes for S8.3 native CMDI key-table preparation/activation.

Only socket.sendto/socket.send are intercepted. Completion packets enter the ordinary UDP
telemetry listener; no production plugin module or private state is consulted.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import re
import socket
import struct
import threading
import time
from unittest.mock import patch

import frames
import spec
import tables

ACTIVE_KEY = bytes([0x71]) * 32
NEXT_KEY = bytes([0x92]) * 32
SOURCE_ID = 2
PREPARED_PATH = "cf/acceptance-native-key.tbl"
TABLE_NAME = "CMDI.ValTable"
# Pinned cFE cfe_tbl_eventids.h: file-open failure and activation rejection.
LOAD_ERROR_EID = 53
ACTIVATE_ERROR_EID = 70
STAGES = (("LOAD", spec.CFE_TBL_LOAD_CC, spec.CFE_TBL_FILE_LOADED_INF_EID),
          ("VALIDATE", spec.CFE_TBL_VALIDATE_CC, spec.CFE_TBL_VALIDATION_INF_EID),
          ("ACTIVATE", spec.CFE_TBL_ACTIVATE_CC, spec.CFE_TBL_UPDATE_SUCCESS_INF_EID))


class NativeProbeFailure(Exception):
    """An observed instruction violated the public native-table contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NativeProbeFailure(message)


def _dispatch(dispatch, instruction: str, data: dict, actions: list) -> bool:
    start = time.monotonic()
    try:
        result = dispatch({"instruction": instruction, "data": data})
    except Exception as error:
        actions.append({"instruction": instruction, "exception": repr(error)})
        raise NativeProbeFailure(f"{instruction} raised through public dispatch: {error!r}") from error
    actions.append({"instruction": instruction, "result": result,
                    "elapsed": time.monotonic() - start})
    _require(type(result) is bool, f"{instruction} returned a non-boolean result: {result!r}")
    return result


def _configured_key(config, target: str) -> str:
    return config.get(target, "cmdi_key_hex", fallback=config.get("cfs", "cmdi_key_hex", fallback=frames.DEFAULT_KEY.hex()))


def validate_prepared_image(image: bytes, default: bytes) -> None:
    """Check native headers and every payload byte, including defaults and spares."""
    head, body = tables.split_image(image, "CMDI")
    tables.split_image(default, "CMDI")
    _require(tables.header_num_bytes(image) == spec.CMDI_TABLE_SIZE,
             "prepared native header has an incorrect NumBytes")
    _require(struct.unpack_from(">III", head) == (0x63464531, 8, spec.CFE_FS_HDR),
             "prepared cFE file header has incorrect content type, table subtype or header length")
    _require(head[spec.CFE_FS_HDR:spec.CFE_FS_HDR + 8] == bytes(8),
             "prepared native table header Reserved/Offset must be zero")
    _require(head[spec.CFE_FS_HDR + 12:] == TABLE_NAME.encode().ljust(spec.TBL_NAME_LEN, b"\0"),
             "prepared native table name/spare bytes are invalid")
    expected = tables.CmdiTable(key=ACTIVE_KEY, next_key=NEXT_KEY, next_valid=1).pack()
    _require(body == expected, "prepared image payload differs from the S3.3 default with only ACTIVE/NEXT/NextValid changed")


def prepare_native_probe(dispatch, config, run_dir, *, target: str = "cpu1", action_log=None) -> dict:
    """Call after RegisterCfs, before StartCfs; return state for run_native_probe.

    The caller should report exceptions as probe failures, never expected plugin
    rejections. Both functions accept the public dispatch bound method directly.
    """
    _require(target == "cpu1", "native probe requires cpu1/UDP2234")
    actions = [] if action_log is None else action_log
    root = Path(run_dir)
    default = (root / "cf/cmdi_tbl.tbl").read_bytes()
    configured = _configured_key(config, target)
    _require(not (root / PREPARED_PATH).exists(), "native probe destination already exists")
    try:
        _require(_dispatch(dispatch, "PrepareCmdiKeyTable", {
            "target": target, "path": PREPARED_PATH, "active_key_hex": ACTIVE_KEY.hex(),
            "next_key_hex": NEXT_KEY.hex()}, actions), "valid native key-image preparation failed")
        image = (root / PREPARED_PATH).read_bytes()
        validate_prepared_image(image, default)
        _require((root / "cf/cmdi_tbl.tbl").read_bytes() == default, "PrepareCmdiKeyTable rewrote the default image")
        _require(_configured_key(config, target) == configured, "PrepareCmdiKeyTable changed the configured key")
    except Exception:
        if (root / PREPARED_PATH).exists():
            _require(_dispatch(dispatch, "RemoveNodeFile", {"target": target, "path": PREPARED_PATH}, actions),
                     "failed preparation also failed RemoveNodeFile cleanup")
            _require(not (root / PREPARED_PATH).exists(), "failed preparation left its native image behind")
        raise
    actions.append({"image": PREPARED_PATH, "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                    "native_image_validated": True})
    return {"default_hex": default.hex(), "configured_key": configured, "actions": actions}


def _telemetry(mid: int, body: bytes, sequence: int = 0) -> bytes:
    packet = bytearray(spec.TLM_HDR + len(body))
    struct.pack_into(">HHHI", packet, 0, mid, 0xC000 | (sequence & 0x3FFF), len(packet) - 7, 1000)
    packet[spec.TLM_HDR:] = body
    return bytes(packet)


def _event(eid: int, severity: int, message: str, sequence: int = 0) -> bytes:
    # CFE_EVS_LongEventTlm: AppName[20], EventID, EventType, SpacecraftID,
    # ProcessorID, Message[122]. All native payload integers are little endian.
    body = struct.pack("<20sHHII122s2x", b"CFE_TBL", eid, severity, 0x42, 1, message.encode())
    return _telemetry(spec.evs_long_mid(1), body, sequence)


def _housekeeping(app: str, sequence: int) -> bytes:
    layout = spec.HK_LAYOUTS[app]
    values = [0] * len(layout.fields)
    values[layout.fields.index("NextValid" if app == "CMDI" else "ValTableShared")] = 1
    return _telemetry(spec.app_mid(app, "HK_TLM", 1), struct.pack(layout.fmt, *values), sequence)


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record) -> None:
        if record.levelno >= logging.INFO:
            self.messages.append(record.getMessage())


class NativeTransport:
    """Capture authenticated public-dispatch commands and inject native events.

    Transport errors are retained and raised on the dispatch thread. The emitter
    thread uses the saved sendto method, keeping injected packets on the real
    listener path while all activation commands remain off the flight node.
    """
    def __init__(self, case: str, address: tuple[str, int], *, delay: float = .12,
                 timeout: float = .35, emit=None) -> None:
        self.case = case
        self.address = address
        self.delay = delay
        self.timeout = timeout
        self.emit = emit
        self.actions: list[dict] = []
        self.frames: list[frames.Frame] = []
        self.errors: list[str] = []
        self.tasks: list[threading.Thread] = []
        self.completed: set[str] = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.original = socket.socket.sendto
        self.original_send = socket.socket.send
        # Pinned default_cfe_tbl_msgdefs.h: payload includes a 32-bit resource
        # handle and two implicit alignment bytes before MemPoolHandle.
        self.tbl_payload = bytearray(284)
        struct.pack_into("<H", self.tbl_payload, 2, 1)
        self.observer_key: bytes | None = None
        self.last_serial: int | None = None

    def _send_packet(self, packet: bytes) -> None:
        if self.emit is not None:
            self.emit(packet)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                _require(self.original(tx, packet, (spec.HOST, 2234)) == len(packet),
                         "short completion telemetry UDP write")

    def begin(self) -> None:
        """Publish a fresh native TBL telemetry baseline before public dispatch."""
        self._send_packet(_telemetry(spec.named_tlm_mid("CFE_TBL_HK_TLM_MID"), bytes(self.tbl_payload)))
        self.actions.append({"tbl_hk_baseline": True, "at": time.monotonic()})
        time.sleep(.03)

    def _table_housekeeping(self, stage: str, rejected: bool) -> bytes:
        payload = self.tbl_payload
        index = next(i for i, row in enumerate(STAGES) if row[0] == stage)
        payload[0] = index + 1 - int(rejected and stage != "VALIDATE")
        payload[1] = int(rejected and stage != "VALIDATE")
        if stage == "LOAD" and not rejected:
            payload[116:180] = ("/" + PREPARED_PATH).encode().ljust(64, b"\0")
            payload[244:284] = TABLE_NAME.encode().ljust(40, b"\0")
        if stage == "VALIDATE":
            struct.pack_into("<H", payload, 6, 1)  # ValidationCounter
            struct.pack_into("<i", payload, 12, -1 if rejected else 0)
            payload[16] = 0  # inactive buffer
            payload[17:57] = TABLE_NAME.encode().ljust(40, b"\0")
            payload[57] = int(not rejected)  # SuccessValCounter
            payload[58] = int(rejected)  # FailedValCounter
            payload[59] = 1  # NumValRequests
        if stage == "ACTIVATE" and not rejected:
            struct.pack_into("<II", payload, 68, 1000, 0)  # LastUpdateTime
            payload[76:116] = TABLE_NAME.encode().ljust(40, b"\0")
        assert len(payload) == 284
        return _telemetry(spec.named_tlm_mid("CFE_TBL_HK_TLM_MID"), bytes(payload), index + 1)

    def _completion(self, stage: str, success_id: int) -> None:
        try:
            if self.stop.wait(self.delay):
                return
            reject = self.case == stage.lower() + "-reject"
            if self.case == "whole-timeout" and stage == "ACTIVATE":
                return
            errors = {"LOAD": (LOAD_ERROR_EID, "Unable to open, Status = -1"),
                      "VALIDATE": (spec.CFE_TBL_VALIDATION_ERR_EID, "validation failed, Status=0xFFFFFFFF"),
                      "ACTIVATE": (ACTIVATE_ERROR_EID, "Cannot activate table, No Inactive image available")}
            eid, detail = errors[stage] if reject else (success_id, {
                "LOAD": "Successful load into working buffer",
                "VALIDATE": "validation successful for Inactive",
                "ACTIVATE": "Successfully Updated"}[stage])
            if stage == "LOAD":
                message = f"Load,file=/{PREPARED_PATH},tbl={'' if reject else TABLE_NAME}:{detail}"
            elif stage == "ACTIVATE" and not reject:
                message = f"CMDI Successfully Updated '{TABLE_NAME}'"
            else:
                message = f"{TABLE_NAME}: {detail}"
            with self.lock:
                self._send_packet(_event(eid, 3 if reject else 2, message))
                self._send_packet(self._table_housekeeping(stage, reject))
                self.actions.append({"event": stage, "id": eid, "success": not reject,
                                     "at": time.monotonic(), "message": message,
                                     "tbl_hk": {"CommandCounter": self.tbl_payload[0],
                                                "CommandErrorCounter": self.tbl_payload[1],
                                                "ValidationCounter": struct.unpack_from("<H", self.tbl_payload, 6)[0],
                                                "LastValStatus": struct.unpack_from("<i", self.tbl_payload, 12)[0]}})
                if not reject:
                    self.completed.add(stage)
            if stage == "ACTIVATE" and not reject:
                for index in range(3):
                    if self.stop.wait(.11):
                        return
                    self._send_packet(_housekeeping("CMDI", index))
                    self._send_packet(_housekeeping("GW", index))
                    with self.lock:
                        self.actions.append({"hk_barrier": index, "at": time.monotonic()})
        except Exception as error:
            with self.lock:
                self.errors.append(f"completion emitter failed: {error!r}")

    def sendto(self, sock, data: bytes, *args) -> int:
        address = args[-1]
        if tuple(address[:2]) != self.address:
            return self.original(sock, data, *args)
        return self._capture(data)

    def send(self, sock, data: bytes, *args) -> int:
        try:
            address = sock.getpeername()
        except OSError:
            return self.original_send(sock, data, *args)
        if tuple(address[:2]) != self.address:
            return self.original_send(sock, data, *args)
        return self._capture(data)

    def _capture(self, data: bytes) -> int:
        try:
            if self.observer_key is not None:
                parsed = frames.parse_frame(bytes(data), self.observer_key)
                _require(parsed.source_id == SOURCE_ID, "configured-key observation used an incorrect source")
                _require(frames.packet_mid(parsed.command) == 0x18E8 and
                         len(parsed.command) == spec.CMD_HDR and parsed.command[6] == 0 and
                         _xor(parsed.command) == 0xFF,
                         "configured-key observation did not preserve the requested raw command")
                self._serial(parsed.sequence)
                self.actions.append({"configured_key_frame": True, "sequence": parsed.sequence})
                return len(data)
            parsed = frames.parse_frame(bytes(data), ACTIVE_KEY)
            _require(parsed.source_id == SOURCE_ID, "native TBL frame used an incorrect source")
            self._serial(parsed.sequence)
            packet = parsed.command
            _require(frames.packet_mid(packet) == spec.tbl_cmd_mid(1), "activation did not send CFE_TBL_CMD_MID")
            _require(_xor(packet) == 0xFF, "native TBL command checksum is invalid")
            index = len(self.frames)
            _require(index < len(STAGES), "activation sent extra native TBL commands")
            stage, cc, eid = STAGES[index]
            _require(packet[6] == cc, f"native activation stage {index} sent CC {packet[6]}, expected {cc}")
            expected = (spec.tbl_load_payload("/" + PREPARED_PATH),
                        spec.tbl_validate_payload(TABLE_NAME, active=False),
                        spec.tbl_activate_payload(TABLE_NAME))[index]
            _require(packet[8:] == expected, f"{stage} payload differs from the pinned native payload")
            with self.lock:
                if index:
                    _require(STAGES[index - 1][0] in self.completed,
                             f"{stage} sent before successful {STAGES[index - 1][0]} completion")
                self.frames.append(parsed)
                self.actions.append({"send": stage, "at": time.monotonic(), "sequence": parsed.sequence,
                                     "source": parsed.source_id, "command_hex": packet.hex()})
            task = threading.Thread(target=self._completion, args=(stage, eid), daemon=True)
            self.tasks.append(task)
            task.start()
        except Exception as error:
            self.errors.append(f"native transport contract: {error!r}")
        return len(data)

    def _serial(self, serial: int) -> None:
        if self.last_serial is not None:
            _require(0 < ((serial - self.last_serial) & 0xFFFFFFFF) < 0x80000000,
                     "native frames did not use a shared monotonic serial allocator")
        self.last_serial = serial

    def close(self) -> None:
        self.stop.set()
        for task in self.tasks:
            task.join(1)
            _require(not task.is_alive(), "completion emitter did not stop")


def _xor(data: bytes) -> int:
    result = 0
    for value in data:
        result ^= value
    return result


def run_native_probe(dispatch, config, run_dir, *, prepared: dict, target: str = "cpu1", action_log=None) -> dict:
    """Run with cpu1 started and its public telemetry listener active.

    Return a serializable report; ok is false for dispatch exceptions, missing
    reasons, wrong frames, early progression, wrong results or failed cleanup.
    """
    actions = prepared["actions"] if action_log is None else action_log
    cases: list[dict] = []
    errors: list[str] = []
    root = Path(run_dir)
    configured = prepared["configured_key"]
    default = bytes.fromhex(prepared["default_hex"])
    address = (config.get(target, "cfs_target_ip", fallback=spec.HOST),
               config.getint(target, "cmd_udp_port", fallback=1234))
    previous_serial = None
    try:
        for case in ("load-reject", "validate-reject", "activate-reject", "whole-timeout", "success"):
            transport = NativeTransport(case, address)
            transport.last_serial = previous_serial
            messages = _Messages()
            logging.getLogger().addHandler(messages)
            started = time.monotonic()
            case_errors: list[str] = []
            result = None
            try:
                with patch.object(socket.socket, "sendto", lambda sock, data, *args: transport.sendto(sock, data, *args)), \
                     patch.object(socket.socket, "send", lambda sock, data, *args: transport.send(sock, data, *args)):
                    transport.begin()
                    started = time.monotonic()
                    result = _dispatch(dispatch, "ActivateCmdiKeyTable", {
                        "target": target, "path": PREPARED_PATH, "key_hex": ACTIVE_KEY.hex(),
                        "source_id": SOURCE_ID, "timeout": transport.timeout if case == "whole-timeout" else 2}, actions)
                    elapsed = time.monotonic() - started
                    _require(result is (case == "success"), f"{case} returned {result!r}")
                    expected_count = {"load-reject": 1, "validate-reject": 2, "activate-reject": 3,
                                      "whole-timeout": 3, "success": 3}[case]
                    _require(len(transport.frames) == expected_count,
                             f"{case} sent {len(transport.frames)} stages, expected {expected_count}")
                    if case == "whole-timeout":
                        _require(transport.timeout - .05 <= elapsed <= transport.timeout + .15,
                                 f"whole-instruction timeout {transport.timeout}s took {elapsed:.3f}s")
                    if case == "success":
                        _require("ACTIVATE" in transport.completed, "success returned before activation completion")
                        activation = next(a["at"] for a in transport.actions
                                          if a.get("event") == "ACTIVATE" and a["success"])
                        _require(any("hk_barrier" in a for a in transport.actions),
                                 "success returned before fresh CMDI/GW HK")
                        _require(time.monotonic() - activation >= .19,
                                 "success returned before two 10Hz GW wakeups after activation")
                    else:
                        reason = " ".join(messages.messages)
                        patterns = {
                            "load-reject": r"unable.*open|load.*(?:reject|error|fail)|file.*(?:access|open)",
                            "validate-reject": r"validat(?:ion|e).*(?:fail|reject|error)",
                            "activate-reject": r"cannot activate|no inactive|activat.*(?:reject|error|fail)|unvalidated",
                            "whole-timeout": r"timeout|timed out|did not complete|deadline"}
                        pattern = patterns[case]
                        _require(re.search(pattern, reason, re.I) is not None,
                                 f"{case} failed without an actionable operation reason: {reason!r}")
                    transport.observer_key = bytes.fromhex(configured)
                    _require(_dispatch(dispatch, "SendCmdiRawPacket", {"target": target, "mid": 0x18E8,
                        "cc": 0, "payload_hex": "", "source_id": SOURCE_ID}, actions),
                        f"{case}: configured-key command failed after activation")
            except Exception as error:
                case_errors.append(repr(error))
            finally:
                logging.getLogger().removeHandler(messages)
                transport.close()
            previous_serial = transport.last_serial
            case_errors.extend(transport.errors)
            if (root / "cf/cmdi_tbl.tbl").read_bytes() != default:
                case_errors.append("activation rewrote the installed default image")
            if _configured_key(config, target) != configured:
                case_errors.append("activation changed the configured key")
            cases.append({"case": case, "result": result, "elapsed": time.monotonic() - started,
                          "actions": transport.actions, "logged_errors": messages.messages, "errors": case_errors})
            errors.extend(f"{case}: {item}" for item in case_errors)
    except Exception as error:
        errors.append(f"native probe raised: {error!r}")
    finally:
        try:
            _require(_dispatch(dispatch, "RemoveNodeFile", {"target": target, "path": PREPARED_PATH}, actions),
                     "RemoveNodeFile failed for prepared image")
            _require(not (root / PREPARED_PATH).exists(), "prepared image remains after RemoveNodeFile")
        except Exception as error:
            errors.append(f"native probe cleanup: {error!r}")
    return {"ok": not errors, "cases": cases, "actions": actions, "errors": errors}
