"""Logic-only regression tests for expanded GW, CTF and mission acceptance gates."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import ctf_checks as ctf
import live_gw as gw
import spec
import static_checks as static
import tables


def script(instructions: list[dict]) -> str:
    return json.dumps({"tests": [{"instructions": instructions}]})


def instruction(op: str, **data) -> dict:
    return {"instruction": op, "data": data}


def command(app: str, cc: str, **payload) -> dict:
    return instruction("SendCmdiCommand", target="cpu1", mid=f"{app}_CMD_MID", cc=f"{app}_{cc}_CC", args={"Payload": payload})


def observe(*fields: str) -> dict:
    return instruction("CheckTlmValue", target="cpu1", mid="MODE_HK_TLM_MID",
                       args=[{"variable": f"Payload.{field}", "compare": "==", "value": 0} for field in fields])


class ExpandedGateway(unittest.TestCase):
    def test_native_connection_and_table_layout(self) -> None:
        definition = tables.ConnDef(1, 1, 3101, "127.0.0.1", 3201, "", (0x187A, 0x187B),
                                    keepalive_enabled=1, peer_node=2)
        raw = definition.pack()
        self.assertEqual(len(raw), 156)
        self.assertEqual(raw[88:92], b"\x01\x02\0\0")
        self.assertEqual(struct.unpack_from("<II", raw, 92), (0x187A, 0x187B))
        body = tables.gw_table({0: definition, 1: definition}, ((0x187A, 0, 1),))
        self.assertEqual(len(body), 1376)
        self.assertEqual(struct.unpack_from("<IBBH", body, 1248), (0x187A, 0, 1, 0))

    def test_connection_snapshot_decodes_every_counter_and_route_tail(self) -> None:
        payload = b"".join(struct.pack("<BBBB10I", 1, 1, 1, 2, *range(10)) for _ in range(8))
        payload += bytes(range(16)) + bytes([1] * 16)
        packet = bytes(16) + payload
        node = SimpleNamespace(id=1, ensure_command_path=lambda: None, cmd=lambda *a, **kw: None,
                               request=lambda *a, **kw: [SimpleNamespace(data=packet)])
        connections, selected, preferred = gw.conn_snapshot(node)
        self.assertEqual(connections[7]["KeepAliveTimeouts"], 9)
        self.assertEqual(connections[0]["TxFrames"], 4)
        self.assertEqual(connections[0]["LastKeepAliveSeconds"], 8)
        self.assertEqual(selected, tuple(range(16)))
        self.assertEqual(preferred, (1,) * 16)

    def test_truncated_expanded_connection_snapshot_fails(self) -> None:
        node = SimpleNamespace(id=1, ensure_command_path=lambda: None, cmd=lambda *a, **kw: None,
                               request=lambda *a, **kw: [SimpleNamespace(data=bytes(176))])
        with self.assertRaisesRegex(Exception, "expanded S5.4"):
            gw.conn_snapshot(node)

    def test_hidden_case_registration_matches_feedback_manifest(self) -> None:
        root = PACK / "hidden_tests/gw"
        cases = json.loads((root / "cases.json").read_text())["s5-hidden-gw"]["cases"]
        source = (root / "coveragetest_gw_hidden.c").read_text()
        for case in cases:
            self.assertIn(json.dumps(case), source)
        for case in ("Keep-alive exact deadline and recovery", "Bounded inbound health and mode cache",
                     "Mandatory switch HMAC with mask disabled", "Route selection failback and preference"):
            self.assertIn(case, cases)


class ExpandedCtf(unittest.TestCase):
    def test_pinned_run_order_and_required_instructions(self) -> None:
        self.assertEqual(len(ctf.SYSTEM), 15)
        self.assertEqual(len(ctf.SMOKE + ctf.SYSTEM), 17)
        self.assertEqual(ctf.SYSTEM[-3:], ("st_safe_mode_node_loss.json", "st_key_switch.json", "st_tlmo_profiles.json"))
        for name in ("ReplayCmdiFrame", "PrepareCmdiKeyTable", "ActivateCmdiKeyTable", "CheckTlmCounters",
                     "OpenGwRelay", "SetGwRelayEnabled", "StartStorGroundCapture", "CheckStorGroundTransfer"):
            self.assertIn(name, ctf.INSTRUCTIONS)
        self.assertEqual(ctf.judge_run_system_tests("\n".join("./run_ctf.sh tests/" + name for name in ctf.SMOKE + ctf.SYSTEM)), [])
        self.assertTrue(ctf.judge_run_system_tests("run_ctf.sh " + " ".join(ctf.SYSTEM)))

    def test_description_cannot_substitute_for_executable_instructions(self) -> None:
        body = {"description": "StopApp GW RestartsSent Attempts BudgetPacketsPerCycle PurgeThresholdPercent Mode",
                "tests": [{"instructions": [command("MODE", "SET_ENABLE", Enabled=0), observe("Enabled")]}]}
        self.assertTrue(any("must stop GW" in error for error in ctf.judge_script_content(json.dumps(body), ctf.SYSTEM[8])))

    def test_automatic_restart_content_and_manual_start_rejection(self) -> None:
        instructions = [command("MODE", "SET_ENABLE", Enabled=0), observe("Enabled"),
                        instruction("StopApp", target="cpu1", app="GW"),
                        observe("RestartsSent", "Attempts", "BudgetPacketsPerCycle", "PurgeThresholdPercent", "Mode")]
        self.assertEqual(ctf.judge_script_content(script(instructions), ctf.SYSTEM[8]), [])
        instructions += [instruction("StartApp", target="cpu1", app="GW")]
        self.assertTrue(any("manual GW START" in error for error in ctf.judge_script_content(script(instructions), ctf.SYSTEM[8])))

    def test_exact_tlmo_counter_sequences_and_mode_profile_path(self) -> None:
        instructions = [command("MODE", "SET_ENABLE", Enabled=0), command("MODE", "SET_MODE", Mode=1),
                        command("MODE", "APPLY_PROFILE"), observe("Enabled", "StreamCount", "PacketsDecimated", "BudgetPacketsPerCycle",
                        "Decimation", "Decimated", "Queued", "SendErrors"), instruction("StopTlmRecording", target="cpu1", name="profiles")]
        for values in ([1,3,5,7,9,11], [1,4,7,10], [1,3,5,7,9,11,13,15,17], [1,4,7,10,13,16]):
            instructions += [instruction("CheckTlmCounters", target="cpu1", name="profiles", mid=0x18EA, counter_offset=16, values=values)]
        self.assertEqual(ctf.judge_script_content(script(instructions), ctf.SYSTEM[14]), [])
        instructions[-1]["data"]["values"] += [16]
        self.assertTrue(ctf.judge_script_content(script(instructions), ctf.SYSTEM[14]))
        instructions += [command("TLMO", "SELECT_PROFILE", Profile=1)]
        self.assertTrue(any("driven by MODE" in error for error in ctf.judge_script_content(script(instructions), ctf.SYSTEM[14])))

    def test_global_definitions_and_native_table_commands_are_required(self) -> None:
        self.assertTrue({"SFI_HEALTH_MID", "MODE_PEER_MID", "GW_KEEPALIVE_MID", "SFI_FAULT_MID", "MODE_CMD_MID"}.issubset(ctf.required_mid_names()))
        for typ in ("CFE_TBL_LoadCmd_t", "CFE_TBL_ValidateCmd_t", "CFE_TBL_ActivateCmd_t", "TLMO_StreamTlm_t", "MODE_StatusTlm_t"):
            self.assertIn(typ, ctf.REQUIRED_CCDD_TYPES)


class ExpandedStatic(unittest.TestCase):
    def test_full_mid_audit_distinguishes_command_and_telemetry(self) -> None:
        expected = {name: spec.app_mid(app, field) for app, rows in spec.APP_TOPICS.items() for field in rows
                    for name in (f"{app}_{field}_MID",)}
        expected.update(STOR_XFER_SEG_MID=0x1878, STOR_XFER_ACK_MID=0x1879,
                        SFI_HEALTH_MID=0x187A, MODE_PEER_MID=0x187B, GW_KEEPALIVE_MID=0x187C, CF_CH0_RX_MID=0x18C8)
        probe = "\n".join(f"MID {name} {value:#06x}" for name, value in expected.items())
        self.assertEqual(static.judge_mid_inventory(probe), [])
        self.assertTrue(static.judge_mid_inventory(probe + "\nMID BUNDLED_CONFLICT_MID 0x08C8"))
        self.assertTrue(static.judge_mid_inventory(probe + "\nMID RESERVED_MID 0x18EF"))

    def test_scheduler_requires_all_32_initializers(self) -> None:
        entries = [f"{{CFE_SB_MSGID_WRAP_VALUE({name}), {period}, 0}}," for name, period in static.SCHED_NEW]
        entries += [f"{{CFE_SB_MSGID_WRAP_VALUE(BASE_{index}_MID), 10, 0}}," for index in range(18)]
        self.assertTrue(static.judge_schedule("\n".join(entries)))
        entries += ["{CFE_SB_MSGID_WRAP_VALUE(SBN_WAKEUP_MID), 10, 0},", "{CFE_SB_MSGID_WRAP_VALUE(SBN_SEND_HK_MID), 20, 0},"]
        self.assertEqual(static.judge_schedule("\n".join(entries)), [])
        self.assertTrue(static.judge_schedule("\n".join(entries) + "\n{CFE_SB_MSGID_WRAP_VALUE(EXTRA_TIMER_MID), 10, 0},"))

    def test_mode_default_spares_and_native_node_mid(self) -> None:
        from dataclasses import replace
        from tables_sfi_mode import ModeTable, default_responses
        for node in (1, 2):
            body = ModeTable(responses=tuple(replace(row, record_mid=spec.named_tlm_mid("CFE_EVS_HK_TLM_MID", node))
                                            for row in default_responses())).pack()
            self.assertEqual(static.judge_mode_default(body, node), [])
            broken = bytearray(body)
            broken[7] = 1
            self.assertTrue(static.judge_mode_default(bytes(broken), node))


if __name__ == "__main__":
    unittest.main()
