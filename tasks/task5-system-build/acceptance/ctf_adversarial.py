"""Adversarial CTF instruction cases shared by the acceptance check and its selftests."""
from __future__ import annotations

import struct
import zlib

ADAPTER_SOURCE = r'''"""Acceptance-only CTF instruction bridge for semantic plugin probes."""
import socket
import struct
import binascii
import hashlib
import hmac
import io
import json
import logging
import time
import zlib
import sys
from lib.plugin_manager import Plugin, ArgTypes
from lib.ctf_global import Global
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, __ACCEPTANCE_PACK__)


class AcceptanceDispatchPlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.name = "AcceptanceDispatchPlugin"
        self.description = "Dispatches acceptance probes through registered instructions"
        self.native_state = None
        self.prepare_snapshots = {}
        self.command_map = {"AcceptanceDispatch": (self.dispatch, [ArgTypes.other] * 5)}

    def dispatch(self, case_id, instruction, data, expected, inject_hex=None):
        try:
            data = dict(data) if isinstance(data, dict) else data
            if instruction == "AcceptanceSerialProbe":
                result = self.serial_probe()
            elif instruction == "AcceptanceModeDisabled":
                result = self.mode_disabled(**data)
            elif instruction == "AcceptanceNativePrepare":
                from ctf_native_probe import prepare_native_probe
                run_dir = Global.config.get(data["target"], "cfs_run_dir")
                self.native_state = prepare_native_probe(
                    Global.plugin_manager.find_plugin_for_command_and_execute, Global.config, run_dir,
                    target=data["target"])
                result = True
            elif instruction == "AcceptanceNativeRun":
                from ctf_native_probe import run_native_probe
                if self.native_state is None:
                    raise RuntimeError("native probe has no prepared state")
                run_dir = Global.config.get(data["target"], "cfs_run_dir")
                report = run_native_probe(Global.plugin_manager.find_plugin_for_command_and_execute,
                                          Global.config, run_dir, prepared=self.native_state,
                                          target=data["target"])
                print("NATIVE_PROBE_REPORT|{}|{}".format(case_id, json.dumps(report, sort_keys=True)), flush=True)
                result = bool(report["ok"])
            else:
                tap_error = bool(data.pop("_acceptance_tap_error", False))
                prepare_state = data.pop("_acceptance_prepare_state", None)
                if inject_hex:
                    port = int(Global.config.get("cpu1", "tlm_udp_port"))
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                        for encoded in inject_hex:
                            sender.sendto(bytes.fromhex(encoded), ("127.0.0.1", port))
                dispatch = Global.plugin_manager.find_plugin_for_command_and_execute
                from lib.logger import logger
                log_stream = io.StringIO()
                log_handler = logging.StreamHandler(log_stream)
                logger.addHandler(log_handler)
                try:
                    if tap_error:
                        from core_plugins.cfs.pycfs.tlm_listener import TlmListener
                        def fail_tap(_listener):
                            print("ADVERSARIAL_TAP_REACHED|{}".format(case_id), flush=True)
                            raise OSError("acceptance telemetry tap fault")
                        with patch.object(TlmListener, "read_socket", fail_tap):
                            result = dispatch({"instruction": instruction, "data": data})
                    else:
                        result = dispatch({"instruction": instruction, "data": data})
                finally:
                    logger.removeHandler(log_handler)
                failure_log = log_stream.getvalue().strip()
                if result is False and failure_log:
                    print("ADVERSARIAL_LOG|{}|{}".format(case_id, json.dumps(failure_log)), flush=True)
                if instruction == "PrepareCmdiKeyTable" and "target" in data and "path" in data:
                    root = Path(Global.config.get(data["target"], "cfs_run_dir"))
                    destination = root / data["path"]
                    key = str(destination)
                    if result is True and destination.is_file():
                        with destination.open("rb") as image_stream:
                            self.prepare_snapshots[key] = image_stream.read()
                    if prepare_state is not None:
                        if prepare_state == "unchanged":
                            okay = destination.is_file() and self.prepare_snapshots.get(key) == destination.read_bytes()
                        elif prepare_state == "absent":
                            okay = key not in self.prepare_snapshots and not destination.exists()
                        else:
                            raise ValueError("unknown prepare-state assertion")
                        print("ADVERSARIAL_PREPARE_STATE|{}|{}|{}".format(case_id, prepare_state, okay), flush=True)
                if instruction == "CheckStorGroundTransfer" and isinstance(data, dict) and isinstance(data.get("name"), str):
                    log_dir = Global.current_script_log_dir or Global.test_log_dir
                    artifact = Path(log_dir) / (data["name"] + ".bin")
                    print("ADVERSARIAL_STOR_ARTIFACT|{}|{}".format(case_id, artifact.exists()), flush=True)
                    if result is True:
                        from lib import ctf_utility
                        expected_bytes = bytes.fromhex(data["expected_hex"])
                        artifact_ok = artifact.is_file() and artifact.read_bytes() == expected_bytes
                        variables_ok = (ctf_utility.get_variable(data["name"] + "_size") == len(expected_bytes) and
                                        ctf_utility.get_variable(data["name"] + "_crc") == zlib.crc32(expected_bytes))
                        print("ADVERSARIAL_STOR_SUCCESS|{}|{}|{}".format(case_id, artifact_ok, variables_ok), flush=True)
            if not isinstance(result, bool):
                raise TypeError("instruction dispatch did not return a bool")
            print("ADVERSARIAL_RESULT|{}|{}|{}".format(case_id, instruction, bool(result)), flush=True)
            return bool(result) == bool(expected)
        except Exception as error:
            print("ADVERSARIAL_EXCEPTION|{}|{}|{}".format(case_id, type(error).__name__, error), flush=True)
            return False

    @staticmethod
    def mode_disabled(target, timeout=5):
        if not 0 < timeout <= 10:
            raise ValueError("MODE isolation timeout must be in (0,10]")
        dispatched = Global.plugin_manager.find_plugin_for_command_and_execute
        end = time.monotonic() + timeout
        setup = [
            {"instruction": "SendCmdiCommand", "data": {
                "target": target, "mid": "MODE_CMD_MID", "cc": "MODE_SET_ENABLE_CC",
                "args": {"Payload": {"Enabled": 0}}, "source_id": 2}},
            {"instruction": "ClearTlmPacket", "data": {"target": target, "mid": "MODE_HK_TLM_MID"}},
            {"instruction": "SendCmdiCommand", "data": {
                "target": target, "mid": "MODE_SEND_HK_MID", "cc": "MODE_SEND_HK_CC", "args": {}, "source_id": 2}},
        ]
        for item in setup:
            if dispatched(item) is not True:
                print("ADVERSARIAL_MODE_SETUP|{}|{} failed before fresh MODE HK acknowledgement".format(
                    target, item["instruction"]), flush=True)
                return False
        check = {"instruction": "CheckTlmValue", "data": {
            "target": target, "mid": "MODE_HK_TLM_MID",
            "args": [{"compare": "==", "variable": "Payload.Enabled", "value": 0}]}}
        while True:
            Global.time_manager.pre_command()
            if dispatched(check) is True:
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                print("ADVERSARIAL_MODE_SETUP|{}|no fresh MODE HK Enabled 0 within {} s after authenticated disable".format(
                    target, timeout), flush=True)
                return False
            time.sleep(min(0.1, remaining))

    @staticmethod
    def serial_probe():
        key = bytes.fromhex("6346532d636d6476616c2d746573742d6b65792d303132333435363738396162")
        frames = []
        original_sendto = socket.socket.sendto
        original_send = socket.socket.send
        def recording_sendto(sock, data, *args):
            packet = bytes(data)
            if packet.startswith(b"CV\x01\x00"):
                frames.append(packet)
            return original_sendto(sock, data, *args)
        def recording_send(sock, data, *args):
            packet = bytes(data)
            if packet.startswith(b"CV\x01\x00"):
                frames.append(packet)
            return original_send(sock, data, *args)

        socket.socket.sendto = recording_sendto
        socket.socket.send = recording_send
        try:
            packet = struct.pack(">HHHBB", 0x1801, 0xC000, 1, 0, 0)
            checksum = 0xFF
            for byte in packet:
                checksum ^= byte
            packet = packet[:7] + bytes([checksum])
            dispatched = Global.plugin_manager.find_plugin_for_command_and_execute
            first = dispatched({"instruction": "SendCmdiRawPacket", "data": {
                "target": "cpu1", "mid": 0x1801, "payload_hex": "", "cc": 0,
                "sequence": 100, "save_frame": "acceptance_saved_frame"}})
            es_stop = dispatched({"instruction": "StopApp", "data": {
                "target": "cpu1", "app": "GW", "timeout": 12}})
            es_start = dispatched({"instruction": "StartApp", "data": {
                "target": "cpu1", "app": "GW", "entry": "GW_AppMain", "file": "/cf/gw.so",
                "priority": 70, "stack_size": 131072, "timeout": 10}})
            link_result = dispatched({"instruction": "OpenUdpLink", "data": {
                "name": "acceptance_serial_link", "local_port": 3349, "remote_port": 3350}})
            link = dispatched({"instruction": "SendLinkCmdiCommand", "data": {
                "name": "acceptance_serial_link", "target": "cpu1", "packet_hex": packet.hex()}})
            replay = dispatched({"instruction": "ReplayCmdiFrame", "data": {
                "target": "cpu1", "variable": "acceptance_saved_frame"}})
            automatic = dispatched({"instruction": "SendCmdiRawPacket", "data": {
                "target": "cpu1", "mid": 0x1801, "payload_hex": "", "cc": 0}})
            dispatched({"instruction": "CloseLink", "data": {"name": "acceptance_serial_link"}})
        finally:
            socket.socket.sendto = original_sendto
            socket.socket.send = original_send
        serials = [struct.unpack_from(">I", frame, 12)[0] for frame in frames]
        valid = True
        for frame in frames:
            length = struct.unpack_from(">H", frame, 6)[0]
            body_end = 16 + length
            valid = valid and len(frame) == length + 50 and frame[:4] == b"CV\x01\x00"
            valid = valid and hmac.compare_digest(frame[body_end:-2],
                    hmac.new(key, frame[:body_end], hashlib.sha256).digest())
            valid = valid and binascii.crc_hqx(frame[:-2], 0xFFFF) == struct.unpack_from(">H", frame, len(frame)-2)[0]
        return (first is True and es_stop is True and es_start is True and
                link_result is True and link is True and replay is True and automatic is True and
                serials == [100, 101, 102, 103, 100, 104] and frames[4] == frames[0] and valid)
'''

def adversarial_vectors() -> list[dict]:
    """Return instruction-level failures that can be induced with public CTF instructions."""
    return [
        {"id": "missing-raw-recording", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "absent", "mid": 0x18EA, "counter_offset": 16, "values": [1]},
         "setup": [], "expected": False},
        {"id": "malformed-raw-counter-offset", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "bad-offset", "mid": 0x18EA, "counter_offset": -1, "values": [1]},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "bad-offset"}, "wait": 0}],
         "packets": [telemetry_packet(0x18EA, b"\x01\x02\x03\x04")], "expected": False, "start_before": True},
        {"id": "raw-counter-no-recorded-values", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "empty", "mid": 0x18EA, "counter_offset": 16, "values": [1]},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "empty"}, "wait": 0}],
         "expected": False},
        {"id": "duplicate-raw-recording-tap", "instruction": "StartTlmRecording",
         "data": {"target": "cpu1", "name": "duplicate-tap"},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "duplicate-tap"}, "wait": 0}],
         "expected": False},
        {"id": "malformed-raw-counter-values", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "bad-values", "mid": 0x08EA, "counter_offset": 16, "values": "42"},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "bad-values"}, "wait": 0}],
         "expected": False, "start_before": True},
        {"id": "raw-counter-tap-error", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "tap-error", "mid": 0x08EA, "counter_offset": 16,
                  "values": [42], "_acceptance_tap_error": True},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "tap-error"}, "wait": 0}],
         "expected": False, "start_before": True, "require_tap_fault_marker": True},
        {"id": "raw-counter-exact-positive", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "raw-positive", "mid": 0x08EA, "counter_offset": 16, "values": [42]},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "raw-positive"}, "wait": 0}],
         "packets": [telemetry_packet(0x08EA, struct.pack("<I", 42))], "expected": True, "start_before": True,
         "stimulus_wait": 1},
        {"id": "raw-counter-extra-value", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "raw-extra", "mid": 0x08EA, "counter_offset": 16, "values": [42]},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "raw-extra"}, "wait": 0}],
         "packets": [telemetry_packet(0x08EA, struct.pack("<I", 42)), telemetry_packet(0x08EA, struct.pack("<I", 43))],
         "expected": False, "start_before": True, "stimulus_wait": 1},
        {"id": "raw-counter-order-error", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "raw-order", "mid": 0x08EA, "counter_offset": 16, "values": [42, 43]},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "raw-order"}, "wait": 0}],
         "packets": [telemetry_packet(0x08EA, struct.pack("<I", 43)), telemetry_packet(0x08EA, struct.pack("<I", 42))],
         "expected": False, "start_before": True, "stimulus_wait": 1},
        {"id": "raw-counter-short-packet", "instruction": "CheckTlmCounters",
         "data": {"target": "cpu1", "name": "raw-short", "mid": 0x08EA, "counter_offset": 16, "values": []},
         "setup": [{"instruction": "StartTlmRecording", "data": {"target": "cpu1", "name": "raw-short"}, "wait": 0}],
         "packets": [telemetry_packet(0x08EA, b"\x01\x02")], "expected": False, "start_before": True,
         "stimulus_wait": 1},
        {"id": "missing-stor-capture", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "absent", "timeout": 0.05}, "setup": [], "expected": False},
        {"id": "malformed-stor-transfer-arguments", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "absent", "timeout": -1, "expected_hex": "xyz"},
         "setup": [], "expected": False},
        {"id": "malformed-fault-log-arguments", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "absent", "timeout": -1, "fault_log": "yes"},
         "setup": [], "expected": False},
        {"id": "malformed-fault-log-header", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-log", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-log"}, "wait": 0}],
         "packets": [stor_segment(b"\0" * 16)], "expected": False, "start_before": True},
        {"id": "malformed-fault-log-record-count", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-log-count", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-log-count"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(1, True), fault_report(2, False)], count=3)),
                     fault_report_packet(1, True), fault_report_packet(2, False)],
         "expected": False, "start_before": True},
        {"id": "fault-log-record-order-error", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-log-order", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-log-order"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(2, False), fault_report(1, True)])),
                     fault_report_packet(1, True), fault_report_packet(2, False)],
         "expected": False, "start_before": True},
        {"id": "fault-log-missing-clear-report", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-log-clear", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-log-clear"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(1, True)])), fault_report_packet(1, True)],
         "expected": False, "start_before": True},
        {"id": "fault-log-over-capacity", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "over-capacity-log", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "over-capacity-log"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(i, bool(i % 2)) for i in range(1, 66)]),
                                  index=index, count=7) for index in range(7)] +
                    [fault_report_packet(1, True), fault_report_packet(2, False)],
         "expected": False, "start_before": True},
        {"id": "fault-log-captured-payload-mismatch", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "mismatched-report-log", "timeout": 4, "fault_log": True},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "mismatched-report-log"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(1, True),
                     fault_report(2, False)[:40] + struct.pack("<II", 102, 3)])),
                     fault_report_packet(1, True), fault_report_packet(2, False)],
         "expected": False, "start_before": True},
        {"id": "fault-log-valid-positive", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "valid-fault-log", "timeout": 4, "fault_log": True,
                  "expected_hex": fault_log_bytes([fault_report(1, True), fault_report(2, False)]).hex()},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "valid-fault-log"}, "wait": 0}],
         "packets": [stor_segment(fault_log_bytes([fault_report(1, True), fault_report(2, False)])),
                     fault_report_packet(1, True), fault_report_packet(2, False)],
         "expected": True, "start_before": True},
        {"id": "stor-valid-ground-transfer", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "valid-transfer", "timeout": 4, "expected_hex": "4142"},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "valid-transfer"}, "wait": 0}],
         "packets": [stor_segment(b"AB")], "expected": True, "start_before": True},
        {"id": "stor-valid-two-segment-transfer", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "valid-two-segment", "timeout": 4, "expected_hex": ("41" * 512) + ("42" * 512)},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "valid-two-segment"}, "wait": 0}],
         "packets": [stor_segment(b"A" * 512 + b"B" * 512, index=0, count=2),
                     stor_segment(b"A" * 512 + b"B" * 512, index=1, count=2)],
         "expected": True, "start_before": True},
        {"id": "stor-duplicate-segment", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "duplicate", "timeout": 4, "expected_hex": "4142"},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "duplicate"}, "wait": 0}],
         "packets": [stor_segment(b"AB"), stor_segment(b"AB")], "expected": False, "start_before": True},
        {"id": "stor-duplicate-before-completion", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "duplicate-before-completion", "timeout": 4},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "duplicate-before-completion"}, "wait": 0}],
         "packets": [stor_segment(b"A" * 512 + b"B" * 512, index=0, count=2),
                     stor_segment(b"A" * 512 + b"B" * 512, index=0, count=2)],
         "expected": False, "start_before": True},
        {"id": "stor-conflicting-duplicate-segment", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "conflicting-duplicate", "timeout": 4},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "conflicting-duplicate"}, "wait": 0}],
         "packets": [stor_segment(b"A" * 512 + b"B" * 512, index=0, count=2),
                     stor_segment(b"A" * 512 + b"B" * 512, index=1, count=2,
                                  crc=zlib.crc32(b"A" * 512 + b"B" * 512) ^ 1)],
         "expected": False, "start_before": True},
        {"id": "stor-corrupted-file-crc", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-crc", "timeout": 4},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-crc"}, "wait": 0}],
         "packets": [stor_segment(b"AB", crc=0)], "expected": False, "start_before": True},
        {"id": "stor-invalid-offset", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-offset", "timeout": 4},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-offset"}, "wait": 0}],
         "packets": [stor_segment(b"AB", offset=512)], "expected": False, "start_before": True},
        {"id": "stor-nonzero-padding", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "bad-padding", "timeout": 4},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "bad-padding"}, "wait": 0}],
         "packets": [stor_segment(b"AB", padding=b"X")], "expected": False, "start_before": True},
        {"id": "stor-missing-segment", "instruction": "CheckStorGroundTransfer",
         "data": {"target": "cpu1", "name": "partial", "timeout": 0.2},
         "setup": [{"instruction": "StartStorGroundCapture", "data": {"target": "cpu1", "name": "partial"}, "wait": 0}],
         "packets": [stor_segment(b"A" * 1024, count=2)], "expected": False, "start_before": True},
        {"id": "prepare-invalid-key-hex", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/adversarial.tbl", "active_key_hex": "zz",
                  "next_key_hex": "02" * 32}, "setup": [], "expected": False},
        {"id": "prepare-zero-next-key", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/adversarial-zero.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "00" * 32}, "setup": [], "expected": False},
        {"id": "prepare-equal-keys", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/adversarial-equal.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "01" * 32}, "setup": [], "expected": False},
        {"id": "prepare-path-traversal", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "../escape.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "02" * 32}, "setup": [], "expected": False},
        {"id": "prepare-absolute-path", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "/tmp/escape.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "02" * 32}, "setup": [], "expected": False},
        {"id": "prepare-native-image-write-error", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/cmdi_tbl.tbl/child", "active_key_hex": "01" * 32,
                  "next_key_hex": "02" * 32}, "setup": [], "expected": False},
        {"id": "prepare-existing-destination", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/adversarial-existing.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "02" * 32, "_acceptance_prepare_state": "unchanged"},
         "setup": [{"instruction": "PrepareCmdiKeyTable", "data": {"target": "cpu1", "path": "cf/adversarial-existing.tbl",
                    "active_key_hex": "01" * 32, "next_key_hex": "02" * 32}, "wait": 0}], "expected": False,
         "start_after_setup": False},
        {"id": "prepare-running-target", "instruction": "PrepareCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/adversarial-running.tbl", "active_key_hex": "01" * 32,
                  "next_key_hex": "02" * 32, "_acceptance_prepare_state": "absent"},
         "setup": [], "expected": False, "start_before": True},
        {"id": "activate-missing-native-image", "instruction": "ActivateCmdiKeyTable",
         "data": {"target": "cpu1", "path": "cf/no-such-adversarial.tbl", "key_hex": "01" * 32,
                  "source_id": 1, "timeout": 1}, "setup": [], "expected": False, "start_before": True},
        {"id": "native-image-activation-stage-faults", "instruction": "AcceptanceNativeRun",
         "data": {"target": "cpu1"},
         "setup": [{"instruction": "AcceptanceNativePrepare", "data": {"target": "cpu1"}, "wait": 0}],
         "expected": True, "prepare_before_start": True, "native_probe": True},
        {"id": "cross-plugin-shared-cmdi-serials", "instruction": "AcceptanceSerialProbe",
         "data": {}, "setup": [
             {"instruction": "AcceptanceModeDisabled", "data": {"target": "cpu1", "timeout": 5}},
         ], "expected": True, "start_before": True, "process_limit": 45},
        {"id": "link-connect-timeout", "instruction": "WaitLinkConnected",
         "data": {"name": "never-connected", "timeout": 0.05},
         "setup": [{"instruction": "OpenTcpServer", "data": {"name": "never-connected", "port": 3349}, "wait": 0}],
         "expected": False},
        {"id": "link-packet-timeout", "instruction": "CheckLinkPacket",
         "data": {"name": "silent-link", "mid": 0x18E7, "target": "cpu1", "timeout": 0.05},
         "setup": [{"instruction": "OpenUdpLink", "data": {"name": "silent-link", "local_port": 3349,
                                                                    "remote_port": 3350}, "wait": 0}],
         "expected": False},
    ] + [{"id": "prepare-valid-native-image", "instruction": "PrepareCmdiKeyTable",
          "data": {"target": "cpu1", "path": "cf/adversarial-valid.tbl", "active_key_hex": "01" * 32,
                   "next_key_hex": "02" * 32}, "setup": [], "expected": True}]


def telemetry_packet(mid: int, tail: bytes) -> bytes:
    packet = bytearray(16 + len(tail))
    struct.pack_into(">HHH", packet, 0, mid, 0, len(packet) - 7)
    packet[16:16 + len(tail)] = tail
    return bytes(packet)


def stor_segment(content: bytes, *, index: int = 0, count: int = 1, crc: int | None = None,
                 offset: int | None = None, padding: bytes = b"") -> bytes:
    start = index * 512
    length = min(512, max(0, len(content) - start))
    full = content[start:start + 512].ljust(512, b"\0")
    if padding:
        full = full[:length] + padding[:512 - length].ljust(512 - length, b"\0")
    body = struct.pack("<IBBBBHHIIIHH64s512s", 1, 1, 0, 0, 0, index, count, len(content),
                       zlib.crc32(content) if crc is None else crc, start if offset is None else offset,
                       length, 0, b"x\0".ljust(64, b"\0"), full)
    return telemetry_packet(0x08C7, body)


def fault_report(sequence: int, active: bool) -> bytes:
    onset = 100 + 2 * ((sequence - 1) // 2)
    return struct.pack("<IBBBBBB2x20sIIII", sequence, 1, 1, 1, int(active), 1, 1,
                       b"CMDI", onset, 0x80000000, onset if active else onset + 1, 0x80000000 if active else 3)


def fault_report_packet(sequence: int, active: bool) -> bytes:
    return telemetry_packet(0x08DE, fault_report(sequence, active))


def fault_log_bytes(rows: list[bytes], *, count: int | None = None) -> bytes:
    actual_count = len(rows) if count is None else count
    last_sequence = struct.unpack_from("<I", rows[-1])[0] if rows else 0
    return struct.pack("<IHHII", 0x5346494C, 1, 48, actual_count, last_sequence) + b"".join(rows)


def judge_adversarial_outcome(output: str, case_id: str, instruction: str, expected: bool) -> bool:
    """Match the explicit result emitted by the adapter around CTF's public dispatch API."""
    prefix = f"ADVERSARIAL_RESULT|{case_id}|{instruction}|"
    results = [line.strip() for line in output.splitlines() if line.strip().startswith(prefix)]
    return results == [prefix + str(expected)]
