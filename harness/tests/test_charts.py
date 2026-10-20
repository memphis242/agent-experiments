import math
import unittest
import xml.etree.ElementTree as ET

import _support  # noqa: F401
import aggregate
import charts


def run(arm, cost_usd, minutes, passed=True, task="task1", status="completed", tokens=None, ctx=None, sub_ctx=None):
    cost = None if cost_usd is None else {"scenarios": {"billed": {"total_usd": cost_usd}}}
    if cost is not None and tokens is not None:
        cost["tokens"] = tokens
    return {"arm": arm, "task": task, "status": status, "passed": passed,
            "rounds": [{"wall_time_s": minutes * 60.0}], "cost": cost,
            "behavior": {"max_context_main": ctx, "max_context_subagent": sub_ctx}}


def rows_for(runs, metric="cost", drop=()):
    return charts.build_rows(runs, "task1", metric, "billed", drop)


class AxisTest(unittest.TestCase):
    def test_ticks_start_at_zero_and_pass_the_maximum(self):
        for top in (0.7, 4.73, 10.94, 44.8, 123.0):
            ticks = charts.axis_ticks(top)
            self.assertEqual(ticks[0], 0)
            self.assertGreaterEqual(ticks[-1], top)
            self.assertLessEqual(len(ticks), charts.TARGET_TICKS + 2)

    def test_step_is_one_two_or_five_times_a_power_of_ten(self):
        for span in (0.3, 1.0, 4.9, 11.4, 46.6, 700.0):
            step = charts.nice_step(span)
            mantissa = step / 10.0 ** round(math.log10(step))
            self.assertTrue(any(abs(mantissa - m) < 1e-9 for m in (0.1, 0.2, 0.5, 1, 2, 5, 10)), step)

    def test_empty_maximum_still_gives_an_axis(self):
        self.assertGreater(len(charts.axis_ticks(0.0)), 1)


class FamilyTest(unittest.TestCase):
    def test_family_mapping(self):
        cases = {
            "solo Opus 5.5": "Solo Opus 5.5",
            "Opus 5.5 subagents, eager": "Opus 5.5",
            "Sonnet 5.5 subagents, eager": "Sonnet 5.5",
            "Sonnet 5 subagents, conservative": "Sonnet 5",
            "Sonnet 4.6 subagents, eager": "Sonnet 4.6",
            "Haiku 5.5 subagents, eager": "Haiku 5.5",
            "Opus 5.5 high main, Codex GPT-6.1 Sol high subagents, eager": "Codex",
            "solo Codex GPT-6.1 Sol, high effort": "Solo Codex",
            "A12": "Other",
        }
        for description, family in cases.items():
            self.assertEqual(charts.family_style(description)[0], family, description)

    def test_conservative_is_lighter_than_eager_of_the_same_family(self):
        eager = charts.family_style("Sonnet 5 subagents, eager")[1]
        tint = charts.family_style("Sonnet 5 subagents, conservative")[1]
        self.assertNotEqual(eager, tint)
        self.assertGreater(sum(charts._rgb(tint)), sum(charts._rgb(eager)))
        self.assertEqual(charts.family_style("solo Opus 5.5")[1], charts.FAMILIES[-1][2])


class LabelTest(unittest.TestCase):
    def test_effort_labels(self):
        cases = {
            "A1": "solo Opus 5.5, high",
            "A6": "Sonnet 5 subagents, eager, high",
            "A10": "solo Sonnet 5.5, xhigh",
            "A13": "solo Opus 5.5, medium",
            "A14": "Opus 5.5 low main, Sonnet 5.5 high + Haiku 5.5 high, tiered",
            "A26": "solo Codex GPT-6.1 Sol, high",
        }
        for arm, label in cases.items():
            self.assertEqual(charts.chart_label(arm, aggregate.ARM_DESCRIPTIONS[arm]), label, arm)

    def test_solo_codex_has_its_own_colour(self):
        solo = {charts.family_style(aggregate.ARM_DESCRIPTIONS[arm]) for arm in ("A26", "A27", "A28", "A29")}
        self.assertEqual(len(solo), 1)
        (_, color), = solo
        self.assertEqual([fam for _, fam, c in charts.FAMILIES if c == color], ["Solo Codex"])

    def test_unknown_arm_keeps_description(self):
        self.assertEqual(charts.chart_label("A99", "something"), "something")


class ChartTest(unittest.TestCase):
    def setUp(self):
        self.runs = [run("A2", 3.0, 6.0), run("A2", 4.0, 8.0, passed=False), run("A10", 2.0, 5.0),
                     run("A1", 1.5, 4.0), run("A1", 9.9, 1.0, status="cli_error"),
                     run("A1", None, 3.0), run("A1", 9.9, 1.0, task="task2")]

    def test_rows_are_ordered_by_arm_number_and_filtered(self):
        rows = rows_for(self.runs)
        self.assertEqual([r.arm for r in rows], ["A1", "A2", "A10"])
        self.assertEqual(len(rows[0].points), 1)          # missing cost, other status and other task excluded
        self.assertEqual(len(rows_for(self.runs, "time")[0].points), 2)  # time does not need a cost report
        self.assertEqual(rows_for(self.runs, "time")[1].points[0].value, 6.0)

    def test_drop_status_excludes_runs(self):
        runs = [run("A1", 1.0, 1.0), run("A1", 2.0, 2.0, status="running")]
        self.assertEqual(len(rows_for(runs, drop=["running"])[0].points), 1)

    def test_output_parses_and_is_deterministic(self):
        rows = rows_for(self.runs)
        first = charts.render_chart(rows, "T", "cost")
        self.assertEqual(first, charts.render_chart(rows_for(self.runs), "T", "cost"))
        self.assertNotIn("—", first)
        root = ET.fromstring(first)
        self.assertTrue(root.tag.endswith("svg"))

    def test_failed_run_draws_hollow(self):
        ns = "{http://www.w3.org/2000/svg}"
        runs = [run("A2", 3.0, 6.0, passed=False), run("A2", 4.0, 8.0)]
        root = ET.fromstring(charts.render_chart(rows_for(runs), "T", "cost"))
        dots = [c for c in root.iter(ns + "circle") if float(c.get("r")) == charts.DOT_RADIUS]
        hollow = [c for c in dots if c.get("fill") == charts.CARD_FILL]
        self.assertEqual((len(dots), len(hollow)), (2, 1))
        self.assertNotEqual(hollow[0].get("stroke"), charts.CARD_FILL)

    def test_single_run_arm_has_a_visible_bar_and_one_value_label(self):
        ns = "{http://www.w3.org/2000/svg}"
        svg = charts.render_chart(rows_for([run("A1", 1.5, 4.0)]), "T", "cost")
        root = ET.fromstring(svg)
        bars = [r for r in root.iter(ns + "rect") if r.get("fill-opacity") == str(charts.BAR_OPACITY)]
        self.assertEqual(len(bars), 1)
        self.assertGreater(float(bars[0].get("width")), 0)
        texts = [t.text for t in root.iter(ns + "text")]
        self.assertIn("1.50", texts)
        self.assertIn("n=1", texts)


class AnnotationTest(unittest.TestCase):
    def test_humanize_two_significant_figures(self):
        cases = {None: "-", 0: "0", 820: "820", 2100: "2.1K", 59000: "59K", 185000: "185K",
                 999600: "1.0M", 7.04e6: "7.0M", 2.5e9: "2.5G"}
        for value, text in cases.items():
            self.assertEqual(charts.humanize(value), text, value)

    def test_maxima_sum_threads_then_take_the_max_over_runs(self):
        def toks(i, o, c):
            return {"m|main": {"input": i, "output": o, "cache_read": c},
                    "s|subagent": {"input": 1, "output": 2, "cache_read": 3}}
        runs = [run("A1", 1.0, 1.0, tokens=toks(100, 50, 1000), ctx=9000, sub_ctx=300),
                run("A1", 2.0, 2.0, tokens=toks(10, 500, 10), ctx=7000)]
        m = rows_for(runs)[0].maxima
        self.assertEqual((m.input, m.output, m.cache_read, m.ctx), (101, 502, 1003, 9000))
        self.assertEqual(m.sub_ctx, 300)
        self.assertIsNone(rows_for([run("A1", 1.0, 1.0, ctx=5)])[0].maxima.sub_ctx)

    def test_missing_token_data_gives_none_and_label_stays_clear_of_the_column(self):
        m = rows_for([run("A1", 1.0, 1.0)])[0].maxima
        self.assertEqual((m.input, m.ctx), (None, None))
        svg = charts.render_chart(rows_for([run("A1", 1.0, 1.0), run("A1", 99999.0, 1.0)]), "T", "cost")
        ET.fromstring(svg)

    def test_only_the_cost_chart_carries_the_annotation_column(self):
        rows = rows_for([run("A1", 1.0, 1.0, tokens={"m|main": {"input": 1}}, ctx=5)])
        cost_svg, time_svg = charts.render_chart(rows, "T", "cost"), charts.render_chart(rows, "T", "time")
        self.assertIn(charts.ANNOT_HEADER, cost_svg)
        self.assertNotIn(charts.ANNOT_HEADER, time_svg)
        self.assertEqual(ET.fromstring(cost_svg).get("width"), str(charts.WIDTH_ANNOTATED))
        self.assertEqual(ET.fromstring(time_svg).get("width"), str(charts.WIDTH))


if __name__ == "__main__":
    unittest.main()


class FailedArmTest(unittest.TestCase):
    def test_failed_arm_draws_a_full_red_bar_and_footnote_and_leaves_the_axis_alone(self):
        ns = "{http://www.w3.org/2000/svg}"
        runs = [run("A1", 1.5, 4.0), run("A18", 9.0, 30.0, passed=False), run("A18", 2.0, 5.0)]
        rows = charts.build_rows(runs, "task1", "cost", "billed", failed=frozenset({"A18"}))
        root = ET.fromstring(charts.render_chart(rows, "T", "cost", "the reason"))
        red = [r for r in root.iter(ns + "rect") if r.get("fill") == charts.FAILED_COLOR]
        self.assertEqual(len(red), 1)
        texts = [t.text for t in root.iter(ns + "text")]
        self.assertIn(charts.FAILED_LABEL, texts)
        self.assertIn("* the reason", texts)
        self.assertNotIn("$10", texts)                       # the failed arm's 9.00 does not set the axis
        dots = [c for c in root.iter(ns + "circle") if float(c.get("r")) == charts.DOT_RADIUS]
        self.assertEqual(len(dots), 1)                       # only A1's run is drawn as a dot

    def test_gate_failures(self):
        def gated(arm, passed, used):
            r = run(arm, 1.0, 1.0, passed=passed)
            r.update(rounds_used=used, max_rounds=5)
            return r
        runs = ([gated("A1", True, 1)] * 3 + [gated("A2", True, 5)] + [gated("A2", True, 1)] * 2
                + [gated("A3", True, 5)] * 2 + [gated("A3", True, 1)] + [gated("A4", False, 5)])
        self.assertEqual(charts.gate_failures(runs, "task1"), {"A3", "A4"})

    def test_notes_need_a_footnote_for_failed_arms(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "notes.json"
            path.write_text(json.dumps({"task1": {"failed": ["A18"]}}))
            with self.assertRaises(ValueError):
                charts.load_notes(path, "task1")
            path.write_text(json.dumps({"task1": {"failed": ["A18"], "footnote": "why"}}))
            self.assertEqual(charts.load_notes(path, "task1"), charts.TaskNotes(frozenset({"A18"}), "why"))
            self.assertEqual(charts.load_notes(path, "task2"), charts.TaskNotes())


class LegendTest(unittest.TestCase):
    def test_legend_wraps_instead_of_overflowing(self):
        at = charts.legend_layout(["aaaa"] * 5, 0.0, 3 * (charts.legend_item_width("aaaa") + charts.LEGEND_ITEM_GAP))
        self.assertEqual([row for _, row in at], [0, 0, 0, 1, 1])
        self.assertEqual(at[3][0], 0.0)
