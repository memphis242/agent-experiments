from __future__ import annotations

import configparser
import logging
from pathlib import Path
import queue
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import ctf_native_probe as probe
import frames
import spec
import tables


def native_default() -> bytes:
    header = bytearray(spec.TBL_FILE_HDR)
    struct.pack_into(">III", header, 0, 0x63464531, 8, spec.CFE_FS_HDR)
    struct.pack_into(">I", header, spec.CFE_FS_HDR + 8, spec.CMDI_TABLE_SIZE)
    header[spec.CFE_FS_HDR + 12:spec.CFE_FS_HDR + 52] = b"CMDI.ValTable".ljust(40, b"\0")
    return bytes(header) + tables.CmdiTable().pack()


class PublicDispatcher:
    """Fake public dispatch with a transport boundary and parsed native telemetry."""
    def __init__(self, root: Path, config, mutation: str = "") -> None:
        self.root = root
        self.config = config
        self.mutation = mutation
        self.serial = 0
        self.incoming = queue.Queue()
        self.calls: list[str] = []

    def receive(self, packet: bytes) -> None:
        self.incoming.put(packet)

    def send(self, packet: bytes, key: bytes) -> None:
        self.serial += 1
        if self.mutation == "serial-reuse":
            self.serial = 1
        if self.mutation == "wrong-key":
            key = bytes(32)
        frame = frames.build_frame(packet, time_seconds=1000, source_id=probe.SOURCE_ID,
                                   key=key, sequence=self.serial)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            if self.mutation == "connected-send":
                tx.connect(("127.0.0.1", 1234))
                tx.send(frame)
            elif self.mutation == "sendto-flags":
                tx.sendto(frame, 0, ("127.0.0.1", 1234))
            else:
                tx.sendto(frame, ("127.0.0.1", 1234))

    def __call__(self, command: dict) -> bool:
        instruction = command["instruction"]
        data = command["data"]
        self.calls.append(instruction)
        if instruction == "PrepareCmdiKeyTable":
            default = (self.root / "cf/cmdi_tbl.tbl").read_bytes()
            image = default[:spec.TBL_FILE_HDR] + tables.CmdiTable(
                key=probe.ACTIVE_KEY, next_key=probe.NEXT_KEY, next_valid=1).pack()
            (self.root / data["path"]).write_bytes(image)
            return True
        if instruction == "RemoveNodeFile":
            (self.root / data["path"]).unlink()
            return True
        if instruction == "SendCmdiRawPacket":
            key = bytes.fromhex(self.config.get("cpu1", "cmdi_key_hex"))
            self.send(frames.build_command(data["mid"], data["cc"]), key)
            return True
        if instruction != "ActivateCmdiKeyTable":
            raise AssertionError(instruction)
        while not self.incoming.empty():
            self.incoming.get_nowait()
        deadline = time.monotonic() + data["timeout"]
        payloads = [spec.tbl_load_payload("/" + probe.PREPARED_PATH),
                    spec.tbl_validate_payload(probe.TABLE_NAME), spec.tbl_activate_payload(probe.TABLE_NAME)]
        for index, (stage, cc, eid) in enumerate(probe.STAGES):
            if self.mutation == "per-stage-timeout":
                deadline = time.monotonic() + data["timeout"]
            payload = payloads[index]
            if self.mutation == "wrong-payload":
                payload = bytes(len(payload))
            self.send(frames.build_command(spec.tbl_cmd_mid(), cc, payload), probe.ACTIVE_KEY)
            if self.mutation == "early-stages":
                continue
            while time.monotonic() < deadline:
                try:
                    event = self.incoming.get(timeout=.005)
                except queue.Empty:
                    continue
                if self.mutation == "native-hk":
                    if int.from_bytes(event[:2], "big") != spec.named_tlm_mid("CFE_TBL_HK_TLM_MID"):
                        continue
                    body = event[16:]
                    failed = body[1] > 0 or (stage == "VALIDATE" and struct.unpack_from("<i", body, 12)[0] != 0)
                    if failed:
                        logging.error("%s native telemetry rejected the operation", stage)
                        return False
                    ready = {"LOAD": body[116:180].split(b"\0")[0] == ("/" + probe.PREPARED_PATH).encode(),
                             "VALIDATE": struct.unpack_from("<H", body, 6)[0] > 0,
                             "ACTIVATE": body[76:116].split(b"\0")[0] == probe.TABLE_NAME.encode()}[stage]
                    if ready:
                        break
                    continue
                if int.from_bytes(event[:2], "big") != spec.evs_long_mid():
                    continue
                event_id, severity = struct.unpack_from("<HH", event, 36)
                if severity >= 3:
                    if self.mutation == "accept-rejection":
                        return True
                    logging.error("native table operation failed: %s", event[48:].split(b"\0")[0].decode())
                    return False
                if event_id == eid:
                    break
            else:
                logging.error("native table operation timed out: %s", stage)
                return False
        if self.mutation == "early-stages":
            return True
        cmdi = False
        gw = 0
        while time.monotonic() < deadline:
            try:
                packet = self.incoming.get(timeout=.005)
            except queue.Empty:
                continue
            mid = int.from_bytes(packet[:2], "big")
            if mid == spec.app_mid("CMDI", "HK_TLM"):
                cmdi = spec.HK_LAYOUTS["CMDI"].unpack(packet[16:])["NextValid"] == 1
            if mid == spec.app_mid("GW", "HK_TLM"):
                gw += spec.HK_LAYOUTS["GW"].unpack(packet[16:])["ValTableShared"] == 1
            if cmdi and gw >= 2:
                if self.mutation == "change-config":
                    self.config.set("cpu1", "cmdi_key_hex", probe.ACTIVE_KEY.hex())
                if self.mutation == "change-default":
                    (self.root / "cf/cmdi_tbl.tbl").write_bytes(b"changed")
                return True
        logging.error("CMDI/GW barrier timed out")
        return False


class NativeProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-probe-", dir="/tmp")
        self.root = Path(self.temporary.name)
        (self.root / "cf").mkdir()
        (self.root / "cf/cmdi_tbl.tbl").write_bytes(native_default())
        self.config = configparser.ConfigParser()
        self.config.read_dict({"cfs": {}, "cpu1": {"cmdi_key_hex": frames.DEFAULT_KEY.hex(),
                                                  "cmd_udp_port": "1234"}})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_probe(self, mutation: str = "") -> dict:
        dispatch = PublicDispatcher(self.root, self.config, mutation)
        prepared = probe.prepare_native_probe(dispatch, self.config, self.root)
        transport_type = probe.NativeTransport
        def transport_factory(case, address):
            return transport_type(case, address, emit=dispatch.receive)
        with patch.object(probe, "NativeTransport", transport_factory):
            return probe.run_native_probe(dispatch, self.config, self.root, prepared=prepared)

    def test_native_image_checks_entire_body_and_header(self) -> None:
        default = native_default()
        image = default[:spec.TBL_FILE_HDR] + tables.CmdiTable(
            key=probe.ACTIVE_KEY, next_key=probe.NEXT_KEY, next_valid=1).pack()
        probe.validate_prepared_image(image, default)
        for offset in (0, spec.CFE_FS_HDR + 8, spec.TBL_FILE_HDR + 35,
                       spec.TBL_FILE_HDR + 55, len(image) - 1):
            with self.subTest(offset=offset):
                broken = bytearray(image)
                broken[offset] ^= 1
                with self.assertRaises(probe.NativeProbeFailure):
                    probe.validate_prepared_image(bytes(broken), default)

    def test_native_image_allows_creation_metadata(self) -> None:
        default = native_default()
        image = bytearray(default[:spec.TBL_FILE_HDR] + tables.CmdiTable(
            key=probe.ACTIVE_KEY, next_key=probe.NEXT_KEY, next_valid=1).pack())
        struct.pack_into(">IIII", image, 12, 0x42, 1, 7, 123456)
        image[32:64] = b"fresh native table image".ljust(32, b"\0")
        probe.validate_prepared_image(bytes(image), default)

    def test_event_is_complete_native_packet_with_zero_padding(self) -> None:
        packet = probe._event(12, 2, "CMDI.ValTable native completion")
        self.assertEqual(len(packet), 172)
        self.assertEqual(frames.packet_len(packet), len(packet))
        self.assertEqual(packet[-2:], b"\0\0")
        self.assertEqual(struct.unpack_from("<HH", packet, 36), (12, 2))

    def test_public_dispatch_good_behavior_passes_all_five_cases(self) -> None:
        report = self.run_probe()
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["cases"]), 5)
        self.assertFalse((self.root / probe.PREPARED_PATH).exists())
        for case in report["cases"]:
            self.assertTrue(any(action.get("configured_key_frame") for action in case["actions"]))

    def test_native_housekeeping_can_drive_completion_without_events(self) -> None:
        report = self.run_probe("native-hk")
        self.assertTrue(report["ok"], report)

    def test_connected_udp_send_and_sendto_flags_are_observed(self) -> None:
        for mutation in ("connected-send", "sendto-flags"):
            with self.subTest(transport=mutation):
                report = self.run_probe(mutation)
                self.assertTrue(report["ok"], report)

    def test_tbl_housekeeping_uses_native_completion_field_offsets(self) -> None:
        transport = probe.NativeTransport("success", ("127.0.0.1", 1234), emit=lambda packet: None)
        load = transport._table_housekeeping("LOAD", False)
        self.assertEqual(len(load), 300)
        self.assertEqual(load[16 + 116:16 + 180].split(b"\0")[0], ("/" + probe.PREPARED_PATH).encode())
        validation = transport._table_housekeeping("VALIDATE", True)
        self.assertEqual(struct.unpack_from("<H", validation, 16 + 6)[0], 1)
        self.assertEqual(struct.unpack_from("<i", validation, 16 + 12)[0], -1)
        self.assertEqual(validation[16 + 58], 1)
        activation = transport._table_housekeeping("ACTIVATE", False)
        self.assertEqual(activation[16 + 76:16 + 116].split(b"\0")[0], probe.TABLE_NAME.encode())
        self.assertEqual(struct.unpack_from("<II", activation, 16 + 68), (1000, 0))

    def test_faults_are_detected_and_cleanup_still_runs(self) -> None:
        for mutation in ("early-stages", "accept-rejection", "wrong-payload", "wrong-key", "serial-reuse",
                         "per-stage-timeout", "change-config", "change-default"):
            with self.subTest(mutation=mutation):
                self.config.set("cpu1", "cmdi_key_hex", frames.DEFAULT_KEY.hex())
                (self.root / "cf/cmdi_tbl.tbl").write_bytes(native_default())
                report = self.run_probe(mutation)
                self.assertFalse(report["ok"], report)
                self.assertTrue(report["errors"])
                self.assertFalse((self.root / probe.PREPARED_PATH).exists())

    def test_dispatch_exception_cannot_count_as_expected_rejection(self) -> None:
        dispatch = PublicDispatcher(self.root, self.config)
        prepared = probe.prepare_native_probe(dispatch, self.config, self.root)
        def raises(command):
            if command["instruction"] == "ActivateCmdiKeyTable":
                raise RuntimeError("dispatch exploded")
            return dispatch(command)
        report = probe.run_native_probe(raises, self.config, self.root, prepared=prepared)
        self.assertFalse(report["ok"])
        self.assertIn("dispatch exploded", " ".join(report["errors"]))
        self.assertFalse((self.root / probe.PREPARED_PATH).exists())


if __name__ == "__main__":
    unittest.main()
