from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import logging
import binascii
import hashlib
import hmac
import sys
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import ctf_adversarial as adversarial


class AdversarialPluginCases(unittest.TestCase):
    def test_case_catalog_covers_parser_and_native_image_boundaries(self) -> None:
        cases = {case["id"]: case for case in adversarial.adversarial_vectors()}
        required = {"stor-duplicate-segment", "stor-duplicate-before-completion", "stor-conflicting-duplicate-segment", "stor-missing-segment",
                    "stor-corrupted-file-crc", "stor-invalid-offset", "stor-nonzero-padding",
                    "stor-valid-ground-transfer", "stor-valid-two-segment-transfer", "malformed-fault-log-header",
                    "malformed-fault-log-arguments", "malformed-fault-log-record-count",
                    "fault-log-record-order-error", "fault-log-missing-clear-report", "fault-log-valid-positive",
                    "raw-counter-extra-value", "raw-counter-order-error", "raw-counter-short-packet",
                    "duplicate-raw-recording-tap", "malformed-raw-counter-values", "raw-counter-tap-error",
                    "malformed-raw-counter-offset", "missing-raw-recording",
                    "prepare-existing-destination", "prepare-running-target", "activate-missing-native-image",
                    "prepare-native-image-write-error", "prepare-valid-native-image", "cross-plugin-shared-cmdi-serials",
                    "native-image-activation-stage-faults", "prepare-zero-next-key", "prepare-equal-keys",
                    "prepare-path-traversal", "prepare-absolute-path",
                    "raw-counter-exact-positive", "stor-valid-ground-transfer"}
        self.assertTrue(required.issubset(cases))
        self.assertTrue(cases["prepare-valid-native-image"]["expected"])
        self.assertFalse(cases["prepare-existing-destination"]["expected"])
        self.assertEqual(cases["prepare-native-image-write-error"]["data"]["path"], "cf/cmdi_tbl.tbl/child")

    def test_adapter_dispatches_registered_instructions_and_separates_exceptions(self) -> None:
        source = adversarial.ADAPTER_SOURCE
        self.assertIn("Global.plugin_manager.find_plugin_for_command_and_execute", source)
        self.assertIn("ArgTypes.other", source)
        self.assertIn("ADVERSARIAL_EXCEPTION|", source)
        self.assertIn("ADVERSARIAL_RESULT|", source)
        self.assertIn("did not return a bool", source)
        self.assertIn("serials == [100, 101, 102, 103, 100, 104]", source)
        self.assertNotIn("process_command(", source)
        compile(source, "acceptance_dispatch_plugin.py", "exec")

    def test_multisegment_stor_builder_keeps_file_crc_and_slice_metadata_consistent(self) -> None:
        content = b"A" * 512 + b"B" * 512
        first = adversarial.stor_segment(content, index=0, count=2)
        second = adversarial.stor_segment(content, index=1, count=2)
        self.assertEqual(int.from_bytes(first[16 + 12:16 + 16], "little"), 1024)
        self.assertEqual(int.from_bytes(first[16 + 24:16 + 26], "little"), 512)
        self.assertEqual(int.from_bytes(second[16 + 20:16 + 22], "little"), 512)
        self.assertEqual(second[16 + 92:], b"B" * 512)

    def test_generated_adapter_uses_dispatch_and_keeps_injection_and_failure_results_scoped(self) -> None:
        events = []

        class FakeSocket:
            def __init__(self, *_args):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def sendto(self, packet, address):
                events.append(("send", packet, address))
                return len(packet)
            def send(self, packet, *args):
                events.append(("send", packet, args))
                return len(packet)

        socket_module = types.ModuleType("socket")
        socket_module.AF_INET = 2
        socket_module.SOCK_DGRAM = 2
        socket_module.socket = FakeSocket

        class FakePlugin:
            def __init__(self):
                pass

        class FakeManager:
            result = False
            error = None
            log_failure = False
            logger = None
            serial_mode = False
            serial_mutation = ""
            shared_serial = 0
            variables = {}

            def emit_frame(self, serial, key=b"cFS-cmdval-test-key-0123456789ab"):
                command = b"testcmd!"
                body = struct.pack(">HBBHHII", 0x4356, 1, 0, 1, len(command), 1000, serial) + command
                frame = body + hmac.new(key, body, hashlib.sha256).digest()
                return frame + struct.pack(">H", binascii.crc_hqx(frame, 0xFFFF))

            def find_plugin_for_command_and_execute(self, command):
                events.append(("dispatch", command))
                if self.error is not None:
                    raise self.error
                if self.log_failure:
                    self.logger.error("target rejected: malformed input")
                if self.serial_mode:
                    name, data = command["instruction"], command["data"]
                    if name == "SendCmdiRawPacket" and data.get("sequence") is not None:
                        serial = data["sequence"]
                        self.shared_serial = serial
                        frame = self.emit_frame(serial)
                        if data.get("save_frame"):
                            self.variables[data["save_frame"]] = frame
                    elif name in ("StopApp", "StartApp", "SendLinkCmdiCommand"):
                        if self.serial_mutation == "per-plugin" and name != "StopApp":
                            serial = 1
                        else:
                            self.shared_serial += 1
                            serial = self.shared_serial
                        frame = self.emit_frame(serial)
                    elif name == "ReplayCmdiFrame":
                        frame = self.variables[data["variable"]]
                        if self.serial_mutation == "replay-changes":
                            frame = frame[:-1] + bytes([frame[-1] ^ 1])
                    elif name == "SendCmdiRawPacket":
                        self.shared_serial += 1
                        frame = self.emit_frame(self.shared_serial)
                    elif name in ("OpenUdpLink", "CloseLink"):
                        return True
                    else:
                        return False
                    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM) as sender:
                        sender.sendto(frame, ("127.0.0.1", 1234))
                    return True
                return self.result

        class FakeConfig:
            @staticmethod
            def get(*_args):
                return "2234"

        manager = FakeManager()
        manager.logger = logging.getLogger("adversarial-adapter-selftest")
        temporary = tempfile.TemporaryDirectory(prefix="adversarial-adapter-")
        self.addCleanup(temporary.cleanup)
        class FakeGlobal:
            plugin_manager = manager
            config = FakeConfig()
            current_script_log_dir = temporary.name
            test_log_dir = temporary.name

        lib = types.ModuleType("lib")
        lib.__path__ = []
        plugin_manager = types.ModuleType("lib.plugin_manager")
        plugin_manager.Plugin = FakePlugin
        plugin_manager.ArgTypes = types.SimpleNamespace(other="other")
        ctf_global = types.ModuleType("lib.ctf_global")
        ctf_global.Global = FakeGlobal
        logger_module = types.ModuleType("lib.logger")
        logger_module.logger = manager.logger
        modules = {"socket": socket_module, "lib": lib, "lib.plugin_manager": plugin_manager,
                   "lib.ctf_global": ctf_global, "lib.logger": logger_module}
        namespace = {}
        source = adversarial.ADAPTER_SOURCE.replace("__ACCEPTANCE_PACK__", repr(str(PACK)))
        with patch.dict(sys.modules, modules):
            exec(compile(source, "acceptance_dispatch_plugin.py", "exec"), namespace)
            plugin = namespace["AcceptanceDispatchPlugin"]()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertTrue(plugin.dispatch("false-case", "CheckTlmCounters", {}, False, ["0102"]))
            self.assertEqual([event[0] for event in events], ["send", "dispatch"])
            self.assertIn("ADVERSARIAL_RESULT|false-case|CheckTlmCounters|False", output.getvalue())
            self.assertTrue(adversarial.judge_adversarial_outcome(output.getvalue(), "false-case", "CheckTlmCounters", False))

            events.clear()
            manager.result = True
            mismatch = io.StringIO()
            with redirect_stdout(mismatch):
                self.assertFalse(plugin.dispatch("wrong-bool", "CheckTlmCounters", {}, False))
            self.assertFalse(adversarial.judge_adversarial_outcome(mismatch.getvalue(), "wrong-bool", "CheckTlmCounters", False))

            manager.result = False
            manager.log_failure = True
            scoped_log = io.StringIO()
            with redirect_stdout(scoped_log):
                self.assertTrue(plugin.dispatch("logged-false", "CheckTlmCounters", {}, False))
            self.assertIn("ADVERSARIAL_LOG|logged-false|", scoped_log.getvalue())

            artifact = Path(temporary.name) / "transfer.bin"
            no_artifact = io.StringIO()
            with redirect_stdout(no_artifact):
                self.assertTrue(plugin.dispatch("stor-false", "CheckStorGroundTransfer", {"name": "transfer"}, False))
            self.assertIn("ADVERSARIAL_STOR_ARTIFACT|stor-false|False", no_artifact.getvalue())
            artifact.write_bytes(b"unexpected")
            with redirect_stdout(no_artifact):
                self.assertTrue(plugin.dispatch("stor-artifact", "CheckStorGroundTransfer", {"name": "transfer"}, False))
            self.assertIn("ADVERSARIAL_STOR_ARTIFACT|stor-artifact|True", no_artifact.getvalue())

            manager.error = RuntimeError("dispatch exploded")
            raised = io.StringIO()
            with redirect_stdout(raised):
                self.assertFalse(plugin.dispatch("raised", "CheckTlmCounters", {}, False))
            self.assertIn("ADVERSARIAL_EXCEPTION|raised|RuntimeError|", raised.getvalue())
            self.assertFalse(adversarial.judge_adversarial_outcome(raised.getvalue(), "raised", "CheckTlmCounters", False))

            manager.error = None
            manager.result = None
            nonboolean = io.StringIO()
            with redirect_stdout(nonboolean):
                self.assertFalse(plugin.dispatch("none-result", "CheckTlmCounters", {}, False))
            self.assertIn("did not return a bool", nonboolean.getvalue())

            manager.error = None
            manager.log_failure = False
            manager.result = True
            manager.serial_mode = True
            manager.shared_serial = 0
            manager.variables = {}
            serial_output = io.StringIO()
            with redirect_stdout(serial_output):
                self.assertTrue(plugin.dispatch("serial-good", "AcceptanceSerialProbe", {}, True))
            self.assertIn("ADVERSARIAL_RESULT|serial-good|AcceptanceSerialProbe|True", serial_output.getvalue())

            manager.serial_mutation = "per-plugin"
            manager.shared_serial = 0
            manager.variables = {}
            serial_mutation = io.StringIO()
            with redirect_stdout(serial_mutation):
                self.assertFalse(plugin.dispatch("serial-mutated", "AcceptanceSerialProbe", {}, True))
            self.assertIn("ADVERSARIAL_RESULT|serial-mutated|AcceptanceSerialProbe|False", serial_mutation.getvalue())

    def test_stor_vectors_are_full_telemetry_datagrams_with_valid_crc_metadata(self) -> None:
        packet = adversarial.stor_segment(b"AB")
        self.assertEqual(int.from_bytes(packet[:2], "big"), 0x08C7)
        self.assertEqual(len(packet), 16 + 604)
        malformed_log = next(case for case in adversarial.adversarial_vectors()
                             if case["id"] == "malformed-fault-log-header")
        self.assertEqual(malformed_log["packets"][0], adversarial.stor_segment(bytes(16)))

    def test_fault_log_vectors_pair_exact_reports_and_record_rows(self) -> None:
        cases = {case["id"]: case for case in adversarial.adversarial_vectors()}
        valid = cases["fault-log-valid-positive"]["packets"]
        self.assertEqual(valid[1][16:], adversarial.fault_report(1, True))
        self.assertEqual(valid[2][16:], adversarial.fault_report(2, False))
        self.assertEqual(len(valid[0]), 16 + 604)
        self.assertFalse(cases["fault-log-missing-clear-report"]["expected"])

    def test_over_capacity_log_has_complete_valid_transfer_and_65_rows(self) -> None:
        import struct
        import zlib
        cases = {case["id"]: case for case in adversarial.adversarial_vectors()}
        packets = cases["fault-log-over-capacity"]["packets"][:7]
        raw = b"".join(packet[108:108 + int.from_bytes(packet[40:42], "little")] for packet in packets)
        self.assertEqual(len(raw), 16 + 65 * 48)
        self.assertEqual(struct.unpack_from("<IHHII", raw), (0x5346494C, 1, 48, 65, 65))
        self.assertEqual(int.from_bytes(packets[0][32:36], "little"), zlib.crc32(raw))
        self.assertEqual([int.from_bytes(raw[offset:offset + 4], "little")
                          for offset in range(16, len(raw), 48)], list(range(1, 66)))
        self.assertFalse(cases["fault-log-captured-payload-mismatch"]["expected"])

    def test_raw_counter_positive_and_adversarial_packets_have_expected_bytes(self) -> None:
        cases = {case["id"]: case for case in adversarial.adversarial_vectors()}
        positive = cases["raw-counter-exact-positive"]["packets"][0]
        self.assertEqual(int.from_bytes(positive[16:20], "little"), 42)
        self.assertEqual(len(cases["raw-counter-short-packet"]["packets"][0]), 18)
        self.assertEqual([int.from_bytes(packet[16:20], "little")
                          for packet in cases["raw-counter-order-error"]["packets"]], [43, 42])

    def test_dispatch_result_judge_uses_boolean_result_not_process_status(self) -> None:
        output = "ADVERSARIAL_RESULT|bad-offset|CheckTlmCounters|False\n"
        self.assertTrue(adversarial.judge_adversarial_outcome(output, "bad-offset", "CheckTlmCounters", False))
        self.assertFalse(adversarial.judge_adversarial_outcome(output, "bad-offset", "CheckTlmCounters", True))
        self.assertFalse(adversarial.judge_adversarial_outcome("", "bad-offset", "CheckTlmCounters", False))


if __name__ == "__main__":
    unittest.main()
