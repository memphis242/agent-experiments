"""Pass and fail paths of every check's decision logic, on small fixtures (fake trees, telemetry, frames)."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
sys.path.insert(0, str(PACK))

import ctf_checks as cc  # noqa: E402
import frames as fr  # noqa: E402
import harness_checks as hc  # noqa: E402
import live_cmdi as l3  # noqa: E402
import live_gw as l5  # noqa: E402
import live_nodes as ln  # noqa: E402
import live_sfi as l6  # noqa: E402
import live_stor as l12  # noqa: E402
import live_tlmo as l4  # noqa: E402
import node as nd  # noqa: E402
import spec  # noqa: E402
import static_checks as st  # noqa: E402
import tables as tb  # noqa: E402

TEMPLATE = Path("/workspace/agent-experiments-fixture/mission-template")


def tlm(mid: int, payload: bytes, secs: int = 1000) -> bytes:
    return struct.pack(">HHH", mid, 0xC000, 16 + len(payload) - 7) + struct.pack(">I", secs) + bytes(6) + payload


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t5-selftest-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# ============================================================================ static

class Layout(Tmp):
    def make_tree(self) -> Path:
        t = self.tmp / "tree"
        for app in st.APPS:
            d = t / "apps" / app
            for sub in ("fsw/src", "fsw/inc", "fsw/tables", "config", "unit-test"):
                (d / sub).mkdir(parents=True)
            (d / "DESIGN.md").write_text("x")
            for f in (f"fsw/inc/{app}_topicids.h", f"fsw/inc/{app}_eventids.h", f"fsw/inc/{app}_fcncodes.h",
                      f"config/default_{app}_msgids.h", f"config/default_{app}_fcncode_values.h",
                      f"config/default_{app}_tbldefs.h", f"fsw/tables/{app}_tbl.c"):
                (d / f).write_text("")
            (d / "fsw" / "src" / f"{app}_app.c").write_text(f"void {app.upper()}_AppMain(void) {{}}\n")
        (t / "apps/sfi/fsw/src/sfi_cluster.c").write_text("")
        (t / "apps/sfi/fsw/src/sfi_cluster.h").write_text("void SFI_ComputeClusters(void);")
        lib = t / "libs" / "cmdval_lib"
        for sub in ("fsw/src", "fsw/public_inc", "unit-test", "ut-stubs"):
            (lib / sub).mkdir(parents=True)
        (lib / "DESIGN.md").write_text("x")
        (lib / "fsw/public_inc/cmdval_lib.h").write_text("")
        (lib / "fsw/src/cmdval_lib.c").write_text("int CMDVAL_LibInit(void) { return 0; }")
        return t

    def test_pass_and_fail(self) -> None:
        t = self.make_tree()
        self.assertEqual(st.judge_layout(t), [])
        (t / "apps/gw/fsw/inc/gw_eventids.h").unlink()
        (t / "apps/tlmo/fsw/src/tlmo_eds_dispatch.c").write_text("")
        items = st.judge_layout(t)
        self.assertTrue(any("gw_eventids.h" in i for i in items) and any("EDS" in i for i in items), items)


TARGETS_OK = """list(APPEND MISSION_GLOBAL_APPLIST sample_app sample_lib cmdval_lib stor cmdi tlmo gw sfi)
SET(cpu1_APPLIST sch_lab)
SET(cpu2_APPLIST sch_lab)
"""
STARTUP_OK = """CFE_LIB, cfe_assert,  CFE_Assert_LibInit, ASSERT_LIB,    0,   0,     0x0, 0;
CFE_LIB, sample_lib,  SAMPLE_LIB_Init,    SAMPLE_LIB,    0,   0,     0x0, 0;
CFE_LIB, cmdval_lib,  CMDVAL_LibInit,     CMDVAL_LIB,    0,   0,     0x0, 0;
CFE_APP, sch_lab,     SCH_LAB_AppMain,    SCH_LAB,      35,   32768, 0x0, 0;
CFE_APP, cmdi,        CMDI_AppMain,       CMDI,         40,   32768, 0x0, 0;
CFE_APP, tlmo,        TLMO_AppMain,       TLMO,         45,   32768, 0x0, 0;
CFE_APP, sample_app,  SAMPLE_APP_Main,    SAMPLE_APP,   50,   32768, 0x0, 0;
""" + "".join(f"CFE_APP, {a},  {a.upper()}_AppMain, {a.upper()}, 70,  131072, 0x0, 0;\n"
              for a in "lc cf ds fm hk hs sc md cs stor gw sfi sbn".split())


class Integration(Tmp):
    def test_targets(self) -> None:
        t = self.tmp
        (t / "sample_defs").mkdir()
        (t / "sample_defs/targets.cmake").write_text(TARGETS_OK)
        (t / "sample_defs/default_osconfig.cmake").write_text("set(OSAL_CONFIG_MAX_FILE_NAME 32)\n")
        self.assertEqual(st.judge_targets(t), [])
        (t / "sample_defs/targets.cmake").write_text(TARGETS_OK.replace("SET(cpu2_APPLIST sch_lab)", "SET(cpu2_APPLIST ci_lab to_lab sch_lab)"))
        self.assertTrue(any("cpu2_APPLIST" in i for i in st.judge_targets(t)))

    def test_startup_script(self) -> None:
        self.assertEqual(st.judge_startup_script(STARTUP_OK), [])
        bad = STARTUP_OK.replace("CFE_APP, tlmo,        TLMO_AppMain,       TLMO,         45",
                                 "CFE_APP, to_lab,      TO_LAB_AppMain,     TO_LAB,       45")
        items = st.judge_startup_script(bad)
        self.assertTrue(any("to_lab" in i for i in items) and any("tlmo" in i for i in items), items)
        if TEMPLATE.is_dir():
            self.assertTrue(st.judge_startup_script("CFE_APP, ci_lab, CI_LAB_AppMain, CI_LAB, 40, 32768, 0x0, 0;"))

    def test_install_custom(self) -> None:
        ok = ("set(APP_DEFINITION_TABLE_LIST)\nset(APP_INCLUDE_TABLE_LIST\n"
              + "".join(f"    $<TARGET_PROPERTY:{a},INCLUDE_DIRECTORIES>\n" for a in ("sample_app", "hk") + st.APPS) + ")\n"
              + "".join(f"target_include_directories({t} INTERFACE ${{APP_INCLUDE_TABLE_LIST}})\n"
                        f"target_compile_definitions({t} INTERFACE ${{APP_DEFINITION_TABLE_LIST}})\n"
                        for t in ("tlmo.table", "stor.table", "sfi.table", "sch_lab.table")))
        self.assertEqual(st.judge_install_custom(ok, "cpu1"), [])
        if TEMPLATE.is_dir():
            bad = (TEMPLATE / "sample_defs/cpu1/install_custom.cmake").read_text()
            self.assertTrue(len(st.judge_install_custom(bad, "cpu1")) >= 5)

    def test_schedule(self) -> None:
        entries = "".join(f"{{CFE_SB_MSGID_WRAP_VALUE({n}), {p}, 0}},\n" for n, p in st.SCHED_NEW)
        self.assertEqual(st.judge_schedule("#include \"stor_msgids.h\"\n" + entries), [])
        items = st.judge_schedule(entries + "{CFE_SB_MSGID_WRAP_VALUE(CI_LAB_SEND_HK_MID), 50, 0},\n")
        self.assertTrue(any("CI_LAB_SEND_HK_MID" in i for i in items))

    def test_readme(self) -> None:
        ok = ("# x\n## Ground interface\nCMDI on UDP 1234 takes framed commands; TLMO sends on 2234.\n"
              "## Registering an app\nAdd streams to TLMO's PriorityTable.\n")
        self.assertEqual(st.judge_readme(ok), [])
        if TEMPLATE.is_dir():
            self.assertTrue(st.judge_readme((TEMPLATE / "README.md").read_text()))

    def test_change_scope(self) -> None:
        t = self.tmp / "t"
        (t / "apps/ci_lab").mkdir(parents=True)
        (t / "apps/ci_lab/a.c").write_text("x")
        (t / "README.md").write_text("r")
        (t / "cfe").mkdir()
        (t / "cfe/core.c").write_text("c")
        manifest = {rel: ["100644", st.git_blob_sha(t / rel)] for rel in ("apps/ci_lab/a.c", "README.md", "cfe/core.c")}
        (t / "README.md").write_text("changed is allowed")
        self.assertEqual(st.judge_change_scope(t, manifest), [])
        (t / "cfe/core.c").write_text("changed is not")
        (t / "apps/ci_lab/new.c").write_text("added")
        items = st.judge_change_scope(t, manifest)
        self.assertTrue(any("cfe/core.c" in i for i in items) and any("lab apps" in i for i in items), items)

    def test_template_unchanged(self) -> None:
        if not TEMPLATE.is_dir():
            self.skipTest("template not available")
        self.assertEqual(st.judge_change_scope(TEMPLATE, st.load_manifest()), [])


class Documents(unittest.TestCase):
    DESIGN = "\n".join(["# STOR design", "Purpose: storage.", "Data flow: bus to files.", "Main data structures: tables.",
                        "Timing: the 1 Hz wakeup purges.", "Error handling: events.", "Decisions: chose X.", "a", "b", "c"])

    def test_design(self) -> None:
        self.assertEqual(st.judge_design(self.DESIGN, "apps/stor/DESIGN.md"), [])
        self.assertTrue(st.judge_design(self.DESIGN + "\n" * 80, "x"))
        self.assertTrue(any("timing" in i for i in st.judge_design(self.DESIGN.replace("Timing: the 1 Hz wakeup purges.", ""), "x")))

    REPORT = """# System report
## Design summary
Two nodes; each RAM disk lives in its own mount namespace; clocks start within a second.
## Interfaces
x
## Unit tests
stor 30, cmdi 20, tlmo 25, gw 40, sfi 22, cmdval_lib 15
## Coverage
stor.c 100%, stor_files.c 99.1%, cmdi.c 100%, tlmo.c 100%, gw.c 98%, sfi.c 100%, cmdval_lib.c 100%
## System tests
ctf/run_system_tests.sh: all pass; plugins cmdi_plugin ...
## Limitations
none
"""

    def test_report(self) -> None:
        self.assertEqual(st.judge_report(self.REPORT), [])
        self.assertTrue(st.judge_report(self.REPORT.replace("## Limitations", "## Notes")))
        self.assertTrue(st.judge_report(self.REPORT.replace("ctf/run_system_tests.sh", "the CTF runner")))

    def test_coverage(self) -> None:
        srcs = {"stor": ["stor.c", "stor_files.c"]}
        self.assertEqual(st.judge_coverage(srcs, {"stor": {"stor.c": (100, 100), "stor_files.c": (95, 100)}}), [])
        items = st.judge_coverage(srcs, {"stor": {"stor.c": (94, 100)}})
        self.assertEqual(len(items), 2)


class DefaultTables(unittest.TestCase):
    def test_stor(self) -> None:
        for node in spec.NODES:
            t = tb.StorTable(entries=((spec.named_tlm_mid("CFE_EVS_HK_TLM_MID", node), 10, 1),
                                      (spec.named_tlm_mid("CFE_ES_HK_TLM_MID", node), 20, 1),
                                      (spec.named_tlm_mid("SAMPLE_APP_HK_TLM_MID", node), 30, 2),
                                      (spec.named_tlm_mid("CFE_TIME_HK_TLM_MID", node), 40, 1)))
            self.assertEqual(st.judge_stor_default(tb.StorTable.unpack(t.pack()), node), [])
            self.assertTrue(st.judge_stor_default(tb.StorTable.unpack(t.pack()), 3 - node))

    def test_cmdi(self) -> None:
        self.assertEqual(st.judge_cmdi_default(tb.CmdiTable.unpack(tb.CmdiTable().pack())), [])
        self.assertTrue(st.judge_cmdi_default(tb.CmdiTable(mask=0x0D)))

    def test_tlmo(self) -> None:
        for node in spec.NODES:
            streams = tuple(spec.tlmo_default_streams(node).items())
            t = tb.TlmoTable.unpack(tb.TlmoTable(16, 64, streams).pack())
            self.assertEqual(st.judge_tlmo_default(t, node), [])
            wrong = tb.TlmoTable(16, 64, tuple((m, (c + 1) % 4) if i == 3 else (m, c) for i, (m, c) in enumerate(streams)))
            self.assertTrue(st.judge_tlmo_default(wrong, node))

    def test_sfi(self) -> None:
        for node in spec.NODES:
            rows = tuple((spec.verif_mid(m, node) if isinstance(m, int) else spec.named_tlm_mid(m, node), o, w, spec.SFI_RULE[r], lim)
                         for m, o, w, r, lim in spec.SFI_DEFAULT_WATCH_ROWS)
            t = tb.SfiTable.unpack(tb.SfiTable(watches=rows).pack())
            self.assertEqual(st.judge_sfi_default(t, node), [])
            self.assertTrue(st.judge_sfi_default(tb.SfiTable(watches=rows[:-1]), node))

    def test_image_header(self) -> None:
        hdr = bytearray(116)
        struct.pack_into(">III", hdr, 64, 0, 0, 56)
        hdr[64 + 12:64 + 12 + 13] = b"CMDI.ValTable"
        img = bytes(hdr) + tb.CmdiTable().pack()
        h, body = tb.split_image(img, "CMDI")
        self.assertEqual(len(body), 56)
        with self.assertRaises(tb.TableImageError):
            tb.split_image(img + b"\0", "CMDI")
        with self.assertRaises(tb.TableImageError):
            tb.split_image(img, "GW")


# ============================================================================ S3.5 harness

class HarnessFunctions(Tmp):
    def run_probe(self, tools: Path) -> tuple[dict, dict]:
        plan = hc.make_plan()
        pf = self.tmp / "plan.json"
        pf.write_text(json.dumps(plan))
        proc = subprocess.run([sys.executable, "-I", "-B", str(PACK / "harness_probe.py"), str(tools), str(pf)],
                              capture_output=True, text=True, timeout=60)
        return plan, json.loads(proc.stdout.strip().splitlines()[-1])

    def test_reference_passes(self) -> None:
        plan, obs = self.run_probe(HERE / "fixtures" / "ref_tools")
        self.assertEqual(hc.judge_functions(plan, obs), [])

    def test_fixture_original_fails(self) -> None:
        orig = PACK.parents[2] / "fixture" / "mission" / "tools"
        plan, obs = self.run_probe(orig)
        items = hc.judge_functions(plan, obs)
        self.assertTrue(items and "lacks" in items[0], items)

    def test_mutant_fails(self) -> None:
        mut = self.tmp / "mut"
        mut.mkdir()
        src = (HERE / "fixtures" / "ref_tools" / "udp_harness.py").read_text()
        (mut / "udp_harness.py").write_text(src.replace('">HBBHHII", 0x4356, 1, 0', '">HBBHHII", 0x4356, 1, 1'))
        plan, obs = self.run_probe(mut)
        self.assertTrue(any("byte-identical" in i for i in hc.judge_functions(plan, obs)))

    def test_cli_judge(self) -> None:
        good = {"send_framed_node2": {"rc": 0, "out": "sent frame of 58 bytes, sequence 7", "err": "",
                                      "datagram": fr.build_frame(fr.build_command(0x1882, 0), time_seconds=1234, sequence=7).hex()},
                "send_framed_options": {"rc": 0, "out": "", "err": "", "datagram": fr.build_frame(
                    fr.build_command(0x18D4, 3, bytes.fromhex("10000000")), time_seconds=5, key=hc.KEY2, source_id=2,
                    sequence=3).hex()},
                "auto_seconds": 1000456,
                "send_framed_auto_time": {"rc": 0, "out": "", "err": "", "datagram": fr.build_frame(
                    fr.build_command(0x1882, 0), time_seconds=1000456, sequence=1).hex()},
                "recv_node2": {"rc": 0, "out": "mid=0x0983 len=18\n", "err": "", "datagram": None},
                "bad_node": {"rc": 1, "out": "", "err": "udp_harness: error: node 3\n", "datagram": None}}
        self.assertEqual(hc.judge_cli(good), [])
        bad = dict(good, bad_node={"rc": 1, "out": "", "err": "Traceback\n  x\nHarnessError\n", "datagram": None})
        self.assertTrue(hc.judge_cli(bad))


# ============================================================================ telemetry parsing and live judges

class Parsing(unittest.TestCase):
    def test_hk_layouts(self) -> None:
        for app, layout in spec.HK_LAYOUTS.items():
            values = list(range(1, len(layout.fields) + 1))
            payload = struct.pack(layout.fmt, *values)
            hk = nd.parse_hk(app, tlm(spec.app_mid(app, "HK_TLM"), payload))
            names = [f for f in layout.fields if f is not None]
            self.assertEqual(hk[names[0]], 1)
            self.assertEqual(hk["CommandErrorCounter"], 2)
            with self.assertRaises(nd.Fail):
                nd.parse_hk(app, tlm(0x0800, payload[:-1]))
        self.assertEqual(nd.parse_hk("STOR", tlm(0x08C4, struct.pack(spec.HK_LAYOUTS["STOR"].fmt, *range(28))))["RxErrors"], 27)

    def test_long_event(self) -> None:
        pkt = bytearray(172)
        pkt[16:20] = b"CMDI"
        struct.pack_into("<HH", pkt, 36, 12, 3)
        pkt[48:48 + 21] = b"Frame rejected: HMAC 2"
        ev = nd.parse_long_event(bytes(pkt))
        self.assertEqual((ev.app, ev.eid, ev.etype, ev.text), ("CMDI", 12, 3, "Frame rejected: HMAC 2"))

    def test_console_and_startup(self) -> None:
        log = "EVS Port1 1980-012-14:03:20.05131 66/1/STOR 1: STOR initialized\nEVS Port1 x 66/1/CMDI 12: Reject FORMAT\n"
        self.assertEqual(nd.console_events(log), [("STOR", 1, "STOR initialized"), ("CMDI", 12, "Reject FORMAT")])
        moved = nd.move_startup_line(STARTUP_OK, "gw", before="cmdi")
        objs = [e[1] for e in nd.startup_lines(moved)]
        self.assertLess(objs.index("gw"), objs.index("cmdi"))
        self.assertEqual(sorted(objs), sorted(e[1] for e in nd.startup_lines(STARTUP_OK)))
        evs = [(a, 1, "") for a in ln.NEW_APPS]
        self.assertEqual(ln.judge_startup_events(evs), [])
        self.assertTrue(ln.judge_startup_events(evs[:-1] + [("TO_LAB", 1, "")]))


class Ev:
    def __init__(self, text: str, etype: int = 3) -> None:
        self.text = text
        self.etype = etype


class CmdiJudges(unittest.TestCase):
    def test_reasons(self) -> None:
        d = {"FormatErrors": 2, "CrcErrors": 1, "HmacErrors": 0, "SourceErrors": 0, "AgeErrors": 0}
        self.assertEqual(l3.judge_reason_deltas(d, {"FORMAT": 2, "CRC": 1}), [])
        self.assertTrue(l3.judge_reason_deltas(d, {"FORMAT": 2}))

    def test_events(self) -> None:
        evs = [Ev("rejected FORMAT"), Ev("rejected HMAC from source 2"), Ev("rejected SOURCE 7")]
        self.assertEqual(l3.judge_reject_events(evs, [("FORMAT", None), ("HMAC", 2), ("SOURCE", 7)]), [])
        self.assertTrue(l3.judge_reject_events(evs[:2], [("FORMAT", None), ("HMAC", 2), ("SOURCE", 7)]))
        self.assertTrue(l3.judge_reject_events([Ev("rejected FORMAT", 2)], [("FORMAT", None)]))


class TlmoJudges(unittest.TestCase):
    def test_eviction_model(self) -> None:
        order, drops = l4.expected_queue(l4.DROP_BURSTS, 8, l4.CLASS_OF)
        self.assertEqual(drops, [0, 1, 6, 6])
        self.assertEqual(order, [(l4.E8, 400), (l4.E8, 401), (l4.E8, 402), (l4.E8, 403),
                                 (l4.E9, 301), (l4.E9, 302), (l4.E9, 303), (l4.E9, 304)])
        order, drops = l4.expected_queue([[(l4.EB, 1), (l4.EB, 2), (l4.EB, 3)]], 2, l4.CLASS_OF)
        self.assertEqual((order, drops), ([(l4.EB, 1), (l4.EB, 2)], [0, 0, 0, 1]))

    def test_class_map(self) -> None:
        payload = struct.pack("<HH", 16, 2) + struct.pack("<IB3x", 0x0808, 0) + struct.pack("<IB3x", 0x18EB, 3) + bytes(62 * 8)
        cm = l4.parse_class_map(tlm(0x08D5, payload))
        self.assertEqual((cm.budget, cm.count, cm.streams[:2]), (16, 2, ((0x0808, 0), (0x18EB, 3))))
        with self.assertRaises(nd.Fail):
            l4.parse_class_map(tlm(0x08D5, payload[:100]))

    def test_default_telemetry(self) -> None:
        mids = {spec.named_tlm_mid(n, 2) for n in l4.DEFAULT_TLM_NAMES}
        self.assertEqual(l4.judge_default_telemetry(mids, 2), [])
        self.assertTrue(l4.judge_default_telemetry(mids - {spec.named_tlm_mid("GW_HK_TLM_MID", 2)}, 2))


class StorJudges(Tmp):
    def test_stats_and_list(self) -> None:
        entries = struct.pack(spec.STOR_STATS_ENTRY_FMT, 0x18E0, 10, 1, 0, 5, 4, 1, 0) + bytes(24 * 31)
        st_ = l12.parse_stats(tlm(0x08C6, entries))
        self.assertEqual(st_[0x18E0].stored, 4)
        lst = struct.pack("<HHHH", 5, 1, 1, 0) + struct.pack(spec.STOR_LIST_ENTRY_FMT, b"m/f4.tbl", 116, 0) + bytes(72 * 3)
        lp = l12.parse_list(tlm(0x08C5, lst))
        self.assertEqual((lp.total, lp.index, lp.entries), (5, 1, (("m/f4.tbl", 116),)))

    def test_packet_files(self) -> None:
        hdr = bytearray(64)
        struct.pack_into(">II", hdr, 0, 0x63464531, 0x5354)
        hdr[32:48] = b"STOR packet file"
        pkts = [l12.stor_packet(0x18E0, 300, i) for i in range(3)]
        data = bytes(hdr) + b"".join(pkts)
        self.assertEqual(l12.check_fs_header(data), [])
        self.assertEqual(l12.read_packets(data), pkts)
        with self.assertRaises(nd.Fail):
            l12.read_packets(data[:-5])
        (self.tmp / "tlm_18E0_00000002.bin").write_bytes(data)
        (self.tmp / "tlm_18E1_00000000.arc").write_bytes(b"x")
        (self.tmp / "other.bin").write_bytes(b"x")
        files = l12.list_packet_files(self.tmp)
        self.assertEqual([(f.mid, f.seq, f.archived) for f in files], [(0x18E1, 0, True), (0x18E0, 2, False)])
        bad = bytearray(data)
        bad[32] = ord("X")
        self.assertTrue(l12.check_fs_header(bytes(bad)))

    def test_purge_prefix(self) -> None:
        A, B = 0x18E0, 0x18E1
        universe = [(A, 0, False), (A, 1, False), (B, 0, False), (B, 1, True)]
        prio = {A: 5, B: 200}
        self.assertEqual(l12.prefix_violations(universe, {(A, 1, False), (B, 0, False)}, prio), [])
        self.assertTrue(l12.prefix_violations(universe, {(B, 1, True), (A, 1, False), (B, 0, False)}, prio))

    def segments(self, content: bytes, **kw) -> list[l12.Segment]:
        out = []
        count = max(1, -(-len(content) // 512))
        for i in range(count):
            chunk = content[i * 512:(i + 1) * 512]
            payload = struct.pack(spec.XFER_SEG_FMT, kw.get("tid", 1), 1, 0, kw.get("area", 0), 0, i, count, len(content),
                                  zlib.crc32(content), i * 512, len(chunk), 0, b"x/data.bin", chunk.ljust(512, b"\0"))
            out.append(l12.parse_segment(tlm(0x08C7, payload), 16))
        return out

    def test_segments(self) -> None:
        content = bytes(range(256)) * 5 + b"tail"
        segs = self.segments(content)
        self.assertEqual(l12.judge_segments(segs, content, transfer_id=1, src=1, dst=0, area=0, dest_path="x/data.bin"), [])
        self.assertTrue(l12.judge_segments(segs[:-1], content, transfer_id=1, src=1, dst=0, area=0, dest_path="x/data.bin"))
        self.assertTrue(l12.judge_segments(segs, content + b"!", transfer_id=1, src=1, dst=0, area=0, dest_path="x/data.bin"))
        empty = self.segments(b"")
        self.assertEqual(l12.judge_segments(empty, b"", transfer_id=1, src=1, dst=0, area=0, dest_path="x/data.bin"), [])
        with self.assertRaises(nd.Fail):
            l12.parse_segment(tlm(0x08C7, bytes(100)), 16)


class GwJudges(unittest.TestCase):
    def test_egress(self) -> None:
        pkts = [fr.build_verification(0x18E4, 40, counter=i) for i in range(3)]
        units = [fr.build_frame(p, time_seconds=1000, source_id=1, sequence=i + 1) for i, p in enumerate(pkts)]
        self.assertEqual(l5.judge_egress(units, pkts, key=fr.DEFAULT_KEY, node=1, onboard=1001), [])
        self.assertTrue(l5.judge_egress(units, pkts, key=l5.KEY2, node=1, onboard=1001))
        self.assertTrue(l5.judge_egress(units, pkts, key=fr.DEFAULT_KEY, node=2, onboard=1001))
        self.assertTrue(l5.judge_egress(units[::-1], pkts, key=fr.DEFAULT_KEY, node=1, onboard=1001))
        stream = b"".join(fr.stream_wrap(u) for u in units)
        self.assertEqual(l5.judge_stream_egress(stream, pkts, key=fr.DEFAULT_KEY, node=1, onboard=1001), [])
        self.assertTrue(l5.judge_stream_egress(stream[4:], pkts, key=fr.DEFAULT_KEY, node=1, onboard=1001))


class SfiParsing(unittest.TestCase):
    def test_status(self) -> None:
        apps = b"".join(struct.pack(spec.SFI_APP_STATUS_FMT, n.encode(), s, 0, t, ls, 0)
                        for n, s, t, ls in (("STOR", 1, 0, 0), ("GW", 2, 1, 1000123))) + bytes(32 * 14)
        hist = struct.pack(spec.SFI_MISS_RECORD_FMT, 1, 1000123, 5) + bytes(12 * 15)
        watches = struct.pack(spec.SFI_WATCH_STATUS_FMT, 3, 5) + bytes(8 * 15)
        payload = struct.pack("<BBHHH", 1, 1, 1, 1, 1) + apps + hist + watches
        s = l6.parse_status(tlm(0x08DD, payload))
        self.assertEqual((s.apps_missing, s.app("GW").state, s.app("GW").last_secs, s.history[0][:2], s.watches[0]),
                         (1, 2, 1000123, (1, 1000123), (3, 5)))
        with self.assertRaises(nd.Fail):
            l6.parse_status(tlm(0x08DD, payload[:-1]))
        hb = l6.heartbeat("SFI_T1", 3)
        self.assertEqual((fr.packet_mid(hb), struct.unpack_from("<I", hb, 28)[0], hb[8:14]), (0x18DF, 3, b"SFI_T1"))


# ============================================================================ CTF

class CtfJudges(Tmp):
    def make_ctf(self) -> Path:
        ctf = self.tmp / "ctf"
        (ctf / "plugins").mkdir(parents=True)
        (ctf / "plugins/__init__.py").write_text("")
        for d, cls in cc.PLUGINS.items():
            (ctf / "plugins" / d).mkdir()
            (ctf / "plugins" / d / "plugin.py").write_text(f"class {cls}:\n    pass\n")
        (ctf / "configs").mkdir()
        (ctf / "configs/mission.ini").write_text(
            "[core]\nadditional_plugins_path = ${CTF_PLUGINS_DIR}\n[mission]\nx = 1\n"
            "[cpu1]\ncfs_run_dir = ${CFS_RUN_DIR}\ncfs_exe = core-cpu1\ncmd_udp_port = 1234\ntlm_udp_port = 2234\n"
            "CCSDS_target = set1\ncmdi_key_hex = 00\ncmdi_source_id = 1\n"
            "[cpu2]\ncfs_run_dir = ${CFS_RUN_DIR_CPU2}\ncfs_exe = core-cpu2\ncmd_udp_port = 1235\ntlm_udp_port = 2235\n"
            "CCSDS_target = set2\ncmdi_key_hex = 00\ncmdi_source_id = 1\n")
        (ctf / "run_ctf.sh").write_text("export CTF_PLUGINS_DIR=x\nexport CFS_RUN_DIR_CPU2=y\n# 1234 1235\n")
        (ctf / "README.md").write_text(" ".join(cc.PLUGINS))
        return ctf

    def test_plugin_layout(self) -> None:
        ctf = self.make_ctf()
        self.assertEqual(cc.judge_plugin_layout(ctf), [])
        (ctf / "plugins/link_plugin/plugin.py").write_text("class Link:\n    pass\n")
        (ctf / "shims/patches").mkdir(parents=True)
        (ctf / "shims/patches/listener.patch").write_text("")
        items = cc.judge_plugin_layout(ctf)
        self.assertTrue(any("LinkPlugin" in i for i in items) and any("listener.patch" in i for i in items), items)

    def make_ccdd(self) -> tuple[Path, str]:
        d = self.tmp / "json"
        d.mkdir()
        names = cc.required_mid_names() + ["CFE_ES_CMD_MID"]
        set1 = []
        for n in names:
            if n == "CFE_ES_CMD_MID":
                v = 0x1806
            elif n in ("STOR_XFER_SEG_MID", "STOR_XFER_ACK_MID"):
                v = spec.XFER_SEG_MID if "SEG" in n else spec.XFER_ACK_MID
            else:
                app, rest = n.split("_", 1)
                v = spec.app_mid(app, rest.removesuffix("_MID"))
            set1.append({"mid_name": n, "mid_value": hex(v)})
        set2 = [{"mid_name": m["mid_name"], "mid_value": hex(spec.localize(int(m["mid_value"], 16), 2))} for m in set1]
        (d / "mission_mids.json").write_text(json.dumps([{"target": "set1", "mids": set1}, {"target": "set2", "mids": set2}]))
        types = iter(cc.REQUIRED_CCDD_TYPES)
        probe = [f"MID {m['mid_name']} {m['mid_value']}" for m in set1]
        for n in names:
            if n.endswith(("_CMD_MID",)) and n.split("_")[0] in spec.NEW_APPS:
                app = n.split("_")[0]
                doc = {"cmd_mid_name": n, "cmd_codes": [{"cc_name": f"{app}_{k}_CC", "cc_value": str(v), "cc_data_type": f"{app}_{k}Cmd_t",
                                                         "cc_parameters": []} for k, v in spec.CC[app].items()]}
            elif n == "CFE_ES_CMD_MID":
                doc = {"cmd_mid_name": n, "cmd_codes": [
                    {"cc_name": "CFE_ES_START_APP_CC", "cc_value": "4", "cc_data_type": "CFE_ES_StartAppCmd_t", "cc_parameters": []},
                    {"cc_name": "CFE_ES_STOP_APP_CC", "cc_value": "5", "cc_data_type": "CFE_ES_StopAppCmd_t", "cc_parameters": []}]}
            else:
                doc = {"tlm_mid_name": n, "tlm_data_type": f"{n}_t", "tlm_parameters": []}
            (d / f"{n.lower()}.json").write_text(json.dumps(doc))
        # every required type is used by some definition and probed
        extra = {"tlm_mid_name": "STOR_HK_TLM_MID", "tlm_data_type": "STOR_HkTlm_t", "tlm_parameters": []}
        (d / "stor_hk_tlm_mid.json").write_text(json.dumps(extra))
        for t in cc.REQUIRED_CCDD_TYPES:
            probe.append(f"SIZE {t} 16")
            (d / f"type_{t}.json").write_text(json.dumps({"tlm_mid_name": f"TYPE_{t}", "tlm_data_type": t, "tlm_parameters": []}))
        del types
        return d, "\n".join(probe)

    def test_ccdd(self) -> None:
        d, probe = self.make_ccdd()
        items = cc.judge_ccdd(d, probe, 0, "17 message definitions size-checked, 60 MIDs compared, 0 mismatches")
        self.assertEqual(items, [])
        self.assertTrue(cc.judge_ccdd(d, probe, 1, "MISMATCH: x\n1 mismatches"))
        docs = json.loads((d / "mission_mids.json").read_text())
        docs[1]["mids"] = docs[1]["mids"][:-3]
        (d / "mission_mids.json").write_text(json.dumps(docs))
        self.assertTrue(any("set2" in i for i in cc.judge_ccdd(d, probe, 0, "0 mismatches")))

    def test_scripts(self) -> None:
        ok = {"tests": [{"instructions": [{"instruction": "RegisterCfs"}, {"instruction": "SendCmdiCommand"},
                                          {"instruction": "ShutdownCfs"}]}]}
        self.assertEqual(cc.judge_script_static(json.dumps(ok), "st_gw_udp.json"), [])
        bad = json.dumps(ok).replace("SendCmdiCommand", "SendCfsCommand")
        self.assertTrue(cc.judge_script_static(bad, "st_gw_udp.json"))
        self.assertTrue(cc.judge_script_static(json.dumps(ok).replace("Cmdi", "Cmdi TO_LAB"), "smoke_sample_app.json"))
        text = "\n".join(f"./run_ctf.sh tests/{n}" for n in cc.SMOKE + cc.SYSTEM)
        self.assertEqual(cc.judge_run_system_tests(text), [])
        swapped = text.replace("st_gw_udp.json", "TMP").replace("st_gw_tcp.json", "st_gw_udp.json").replace("TMP", "st_gw_tcp.json")
        self.assertTrue(cc.judge_run_system_tests(swapped))


if __name__ == "__main__":
    unittest.main()
