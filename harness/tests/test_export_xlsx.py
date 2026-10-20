import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import _support  # noqa: F401
import aggregate
import export_xlsx

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def run_json(run_id, arm, rep, *, task="task1", status="completed", passed=True, usd=2.0, error=None,
             rounds_used=1, codex_usd=0.0):
    by_model = {"claude-opus-5-5": usd - codex_usd}
    if codex_usd:
        by_model["gpt-6.1-sol"] = codex_usd
    return {"run_id": run_id, "arm": arm, "task": task, "rep": rep, "status": status, "passed": passed,
            "rounds_used": rounds_used, "cap_hit": False, "error": error,
            "started_at": "2026-10-18T00:00:00Z", "finished_at": "2026-10-18T00:10:00Z",
            "totals": {"wall_time_s": 600.0, "turns": 12},
            "behavior": {"subagent_spawns": 1, "compaction_events": 0, "max_context_main": 50000,
                         "max_context_subagent": None},
            "cost": {"scenarios": {"billed": {"total_usd": usd, "by_model": by_model}},
                     "tokens": {"claude-opus-5-5|main": {"input": 10, "output": 20, "cache_read": 30,
                                                         "cache_write_5m": 1, "cache_write_1h": 2}}}}


def write_run(root, data):
    d = root / data["run_id"]
    d.mkdir()
    (d / "run.json").write_text(json.dumps(data), encoding="utf-8")


class ExportXlsxTest(unittest.TestCase):
    def build(self, runs, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for r in runs:
                write_run(root, r)
            loaded = aggregate.load_runs(root)
        return export_xlsx.build_workbook(loaded, scenario="billed", runs_dir="RUNS", **kwargs)

    def sheet_cells(self, zf, sheet_no):
        root = ET.fromstring(zf.read(f"xl/worksheets/sheet{sheet_no}.xml"))
        strings = [si.find("m:t", NS).text for si in ET.fromstring(zf.read("xl/sharedStrings.xml"))]
        rows = []
        for row in root.iter(f"{{{NS['m']}}}row"):
            cells = {}
            for c in row:
                v = c.find("m:v", NS)
                raw = v.text if v is not None else None
                cells[c.get("r")] = (c.get("t"), strings[int(raw)] if c.get("t") == "s" else raw)
            rows.append(cells)
        return rows

    def test_workbook_structure_and_typed_cells(self):
        runs = [run_json("aaaa0001", "A2", 1, usd=3.0, codex_usd=1.0, rounds_used=2),
                run_json("aaaa0002", "A10", 1, usd=1.0),
                run_json("aaaa0003", "A1", 1, usd=2.0),
                run_json("aaaa0004", "A1", 2, usd=2.0, passed=False, status="cli_error", error="bad <x> & \x1b"),
                run_json("aaaa0005", "A1", 3, usd=9.0)]
        data = self.build(runs, superseded={"aaaa0005": "rerun"}, drop_status=["cli_error"])
        zf = zipfile.ZipFile(io.BytesIO(data))
        for name in zf.namelist():
            ET.fromstring(zf.read(name))      # every part is well-formed XML
        sheets = ET.fromstring(zf.read("xl/workbook.xml")).find("m:sheets", NS)
        self.assertEqual([s.get("name") for s in sheets], ["README", "T1 summary", "T1 runs"])
        for no, last_row in ((2, 4), (3, 6)):   # header plus 3 arms, header plus 5 attempts
            table = ET.fromstring(zf.read(f"xl/tables/table{no - 1}.xml"))
            self.assertTrue(table.get("ref").endswith(str(last_row)), table.get("ref"))
            self.assertEqual(table.get("ref"), table.find("m:autoFilter", NS).get("ref"))
            self.assertEqual(int(table.find("m:tableColumns", NS).get("count")),
                             len(table.find("m:tableColumns", NS)))
        summary = self.sheet_cells(zf, 2)
        self.assertEqual([r[f"A{n}"][1] for n, r in enumerate(summary[1:], start=2)], ["A1", "A2", "A10"])
        head = [c[1] for c in summary[0].values()]
        a1, a2 = summary[1], summary[2]
        passes, attempts = head.index("Passes"), head.index("Attempts")
        col = lambda i: export_xlsx.column_letters(i)
        self.assertEqual(float(a1[f"{col(passes)}2"][1]), 1.0)
        self.assertEqual(float(a1[f"{col(attempts)}2"][1]), 1.0)       # superseded and dropped runs are out
        ratio = col(head.index("Spend per pass vs A1"))
        self.assertEqual(float(a1[f"{ratio}2"][1]), 1.0)
        self.assertEqual(float(a2[f"{ratio}3"][1]), 1.5)
        self.assertEqual(float(a2[f"{col(head.index('Codex cost per run USD median'))}3"][1]), 1.0)
        self.assertEqual(float(a2[f"{col(head.index('Runs over one round'))}3"][1]), 1.0)
        runs_rows = self.sheet_cells(zf, 3)
        header = [c[1] for c in runs_rows[0].values()]
        by_id = {r["A" + str(n)][1]: (n, r) for n, r in enumerate(runs_rows[1:], start=2)}
        self.assertEqual(len(by_id), 5)
        n, row = by_id["aaaa0004"]
        at = lambda name: row[f"{col(header.index(name))}{n}"]
        self.assertEqual(at("in_results"), ("b", "0"))
        self.assertEqual(at("passed"), ("b", "0"))
        self.assertEqual(at("excluded_reason"), ("s", "dropped status: cli_error"))
        self.assertEqual(at("error"), ("s", "bad <x> & \\x1b"))
        self.assertEqual(at("started_utc")[0], None)                   # numeric serial, no type tag
        self.assertAlmostEqual(float(at("started_utc")[1]), 46313.0, places=3)
        n, row = by_id["aaaa0005"]
        self.assertTrue(row[f"{col(header.index('excluded_reason'))}{n}"][1].startswith("superseded: rerun"))
        n, row = by_id["aaaa0001"]
        self.assertEqual(float(row[f"{col(header.index('cache_write_tokens'))}{n}"][1]), 3.0)
        self.assertEqual(float(row[f"{col(header.index('wall_minutes'))}{n}"][1]), 10.0)

    def test_solo_codex_run_is_all_codex_usd(self):
        run = run_json("c1", "A26", 1, usd=3.0)
        run["cost"]["scenarios"]["billed"]["by_model"] = {"gpt-6.1-sol": 3.0}
        self.assertEqual(export_xlsx.split_cost(run, "billed"), (0, 3.0))
        self.assertEqual(aggregate.ARM_DESCRIPTIONS["A26"], "solo Codex GPT-6.1 Sol, high effort")

    def test_output_is_deterministic(self):
        runs = [run_json("bbbb0001", "A1", 1), run_json("bbbb0002", "A3", 1, task="task2")]
        self.assertEqual(self.build(runs), self.build(runs))

    def test_unknown_task_is_an_error(self):
        with self.assertRaises(ValueError):
            self.build([run_json("cccc0001", "A1", 1)], tasks=["task4"])

    def test_names_and_columns(self):
        self.assertEqual(export_xlsx.column_letters(0), "A")
        self.assertEqual(export_xlsx.column_letters(26), "AA")
        self.assertTrue(export_xlsx.valid_table_name("Summary_T1"))
        self.assertFalse(export_xlsx.valid_table_name("T1"))
        self.assertEqual(export_xlsx.clean_text("a\x00b\tc"), "a\\x00b\tc")


if __name__ == "__main__":
    unittest.main()
