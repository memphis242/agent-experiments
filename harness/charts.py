#!/usr/bin/env python3
"""Render per-arm spread charts (billed cost and wall time per run) as standalone SVG files.

One chart per metric, one row per arm: a range bar from the arm's minimum to maximum, one dot per
run (hollow when the run did not pass), and a tick at the median. Standard library only; the
output is deterministic for a given input.
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

import aggregate
import cost

# ---- Design parameters ------------------------------------------------------------------------
FONT_STACK = '-apple-system, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif'
ROW_HEIGHT = 44
CARD_MARGIN = 8                 # space between the SVG edge and the card
CARD_RADIUS = 14
CARD_FILL = "#0d1117"
CARD_STROKE = "#30363d"
PAD_X = 28                      # card edge to content
GUTTER_WIDTH = 340              # arm id / description column
ARM_ID_WIDTH = 42               # arm id to description text
PLOT_WIDTH = 518                # x extent of the plot area (value 0 to the axis limit)
PLOT_RIGHT_PAD = 120            # room right of the axis end for the range text
ANNOT_GAP = 14                  # gap between the range-text zone and the annotation column
ANNOT_FIELD_WIDTHS = (62, 66, 88, 66, 84)   # in / out / cached / ctx / sub ctx cell widths
ANNOT_FONT_SIZE = 11
ANNOT_FONT_STACK = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'
ANNOT_HEADER = "max per arm: input / output / cache read / main ctx / subagent ctx"
RANGE_CHAR_WIDTH = 6.6          # approximate glyph width of the range text
WIDTH = 2 * CARD_MARGIN + 2 * PAD_X + GUTTER_WIDTH + PLOT_WIDTH + PLOT_RIGHT_PAD
WIDTH_ANNOTATED = WIDTH + ANNOT_GAP + sum(ANNOT_FIELD_WIDTHS)   # cost chart: room for the annotation column
HEADER_TOP = 40                 # card top to the title baseline
TITLE_SIZE = 20
SUBTITLE_SIZE = 13
SUBTITLE_GAP = 22
LEGEND_GAP = 26                 # subtitle baseline to legend row centre
LEGEND_TO_ROWS = 26             # legend row centre to the top of the first row
LEGEND_ITEM_GAP = 22
LEGEND_FONT_SIZE = 12
LEGEND_SWATCH_RADIUS = 5
LEGEND_CHAR_WIDTH = 6.4         # approximate glyph width used to space legend items
AXIS_HEIGHT = 44                # space under the last row for tick labels
CARD_BOTTOM_PAD = 16
ARM_ID_SIZE = 14
DESC_SIZE = 12
COUNT_SIZE = 11
RANGE_TEXT_SIZE = 12
TICK_LABEL_SIZE = 11
TEXT_COLOR = "#e6edf3"
MUTED_COLOR = "#9da7b3"
FAINT_COLOR = "#7d8590"
GRID_COLOR = "#ffffff"          # drawn at GRID_OPACITY
GRID_OPACITY = 0.09
AXIS_COLOR = "#484f58"
ROW_STRIPE_COLOR = "#161b22"
MEDIAN_COLOR = "#f0f6fc"
BAR_HEIGHT = 16
BAR_OPACITY = 0.34
DOT_RADIUS = 4.6
DOT_OPACITY = 0.92
HOLLOW_STROKE_WIDTH = 1.8
JITTER = 7.0                    # largest vertical dot offset from the row centre
JITTER_PATTERN = (0.0, -0.6, 0.6, -1.0, 1.0, -0.3, 0.3, -0.8, 0.8)  # fractions of JITTER
MEDIAN_HALF_HEIGHT = 11
MEDIAN_WIDTH = 2.6
TINT = 0.48                     # mix toward white for conservative arms
TARGET_TICKS = 6
AXIS_HEADROOM = 1.04            # the axis extends at least this far past the maximum value

# Subagent model family -> saturated colour. Order matters: first matching marker wins.
FAMILIES: tuple[tuple[str, str, str], ...] = (
    # (marker substring of the description, family label, saturated colour)
    ("tiered", "Tiered", "#fbbf24"),
    ("Opus 5.5 subagents", "Opus 5.5", "#a78bfa"),
    ("Sonnet 5.5 subagents", "Sonnet 5.5", "#60a5fa"),
    ("Sonnet 5 subagents", "Sonnet 5", "#2dd4bf"),
    ("Sonnet 4.6 subagents", "Sonnet 4.6", "#fb923c"),
    ("Haiku 5.5", "Haiku 5.5", "#4ade80"),
    ("solo Sonnet 5.5", "Solo Sonnet 5.5", "#f472b6"),
    ("solo Opus 5.5", "Solo Opus 5.5", "#9ca3af"),
)
FALLBACK_FAMILY = ("Other", "#9ca3af")
TINT_MARKERS = ("conservative", "roles")     # description words that select the lighter tint

# Metric id -> output file name
CHART_FILES = {"cost": "{task}-cost.svg", "time": "{task}-time.svg"}


@dataclass(frozen=True)
class Point:
    value: float
    passed: bool


@dataclass(frozen=True)
class Maxima:
    """Per-arm maxima over its runs; None where no run carried the figure."""
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    ctx: float | None = None
    sub_ctx: float | None = None


@dataclass(frozen=True)
class ArmRow:
    arm: str
    description: str
    family: str
    color: str          # fill colour for bar and dots
    points: tuple[Point, ...] = field(default_factory=tuple)
    maxima: "Maxima" = field(default_factory=lambda: Maxima())


# ---- Colour helpers ---------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def mix(hex_color: str, target: tuple[int, int, int], amount: float) -> str:
    assert 0.0 <= amount <= 1.0
    r, g, b = (round(c + (t - c) * amount) for c, t in zip(_rgb(hex_color), target))
    return f"#{r:02x}{g:02x}{b:02x}"


def family_style(description: str) -> tuple[str, str]:
    """(family label, fill colour) for an arm description."""
    label, base = FALLBACK_FAMILY
    for marker, fam, color in FAMILIES:
        if marker in description:
            label, base = fam, color
            break
    lowered = description.lower()
    fill = base
    if not label.startswith("Solo") and any(word in lowered for word in TINT_MARKERS):
        fill = mix(base, (255, 255, 255), TINT)
    return label, fill


# ---- Data -------------------------------------------------------------------------------------
def arm_number(arm: str) -> tuple[int, str]:
    found = re.search(r"\d+", arm)
    return (int(found.group(0)) if found else 10**9, arm)


def wall_minutes(run: dict[str, Any]) -> float:
    return sum(float(r.get("wall_time_s") or 0.0) for r in run.get("rounds") or []) / 60.0


def run_figures(run: dict[str, Any]) -> Maxima:
    """One run's token totals summed over every model and thread, and its main-agent context peak."""
    tokens = (run.get("cost") or {}).get("tokens") or {}
    totals = {k: sum(float(t.get(k) or 0) for t in tokens.values()) for k in ("input", "output", "cache_read")}
    behavior = run.get("behavior") or {}
    ctx, sub_ctx = behavior.get("max_context_main"), behavior.get("max_context_subagent")
    return Maxima(totals["input"] if tokens else None, totals["output"] if tokens else None,
                  totals["cache_read"] if tokens else None, float(ctx) if ctx is not None else None,
                  float(sub_ctx) if sub_ctx is not None else None)


def max_figures(per_run: Sequence[Maxima]) -> Maxima:
    def top(name: str) -> float | None:
        found = [getattr(m, name) for m in per_run if getattr(m, name) is not None]
        return max(found) if found else None
    return Maxima(top("input"), top("output"), top("cache_read"), top("ctx"), top("sub_ctx"))


def humanize(value: float | None) -> str:
    """Two significant figures below 10 of a unit, whole numbers above: 820, 2.1K, 59K, 7.0M."""
    if value is None:
        return "-"
    units = ((1.0, ""), (1e3, "K"), (1e6, "M"), (1e9, "G"))
    for i, (size, suffix) in enumerate(units):
        scaled = value / size
        text = f"{scaled:.1f}" if (i > 0 and round(scaled, 1) < 10) else f"{round(scaled):.0f}"
        if round(scaled) < 1000 or i == len(units) - 1:
            return text + suffix
    raise AssertionError("unreachable")


def build_rows(runs: Sequence[dict[str, Any]], task: str, metric: str, scenario: str,
               drop_status: Sequence[str] = ()) -> list[ArmRow]:
    """One ArmRow per arm with at least one completed run carrying the metric."""
    assert metric in CHART_FILES
    dropped = set(drop_status)
    by_arm: dict[str, list[Point]] = {}
    stats: dict[str, list[Maxima]] = {}
    for run in runs:
        if run.get("task") != task or run.get("status") != "completed" or run.get("status") in dropped:
            continue
        value = aggregate.scenario_cost(run, scenario) if metric == "cost" else wall_minutes(run)
        if value is None:
            continue
        stats.setdefault(run.get("arm", "?"), []).append(run_figures(run))
        by_arm.setdefault(run.get("arm", "?"), []).append(Point(float(value), bool(run.get("passed"))))
    rows: list[ArmRow] = []
    for arm in sorted(by_arm, key=arm_number):
        description = aggregate.ARM_DESCRIPTIONS.get(arm, arm)
        family, fill = family_style(description)
        points = tuple(sorted(by_arm[arm], key=lambda p: (p.value, not p.passed)))
        rows.append(ArmRow(arm, description, family, fill, points, max_figures(stats[arm])))
    return rows


# ---- Axis -------------------------------------------------------------------------------------
def nice_step(span: float, target: int = TARGET_TICKS) -> float:
    """Smallest 1, 2 or 5 x 10^k step that gives at most `target` intervals over `span`."""
    assert span > 0 and target > 0
    exponent = math.floor(math.log10(span / target))
    for exp in (exponent, exponent + 1):
        for mult in (1, 2, 5):
            step = mult * 10.0 ** exp
            if span / step <= target + 1e-9:
                return step
    raise AssertionError("unreachable: 10^(exponent+1) always satisfies the target")


def axis_ticks(max_value: float) -> list[float]:
    """Tick values from 0 to a limit at least AXIS_HEADROOM x max_value; the last is the axis limit."""
    top = max(max_value, 0.0) * AXIS_HEADROOM
    if top <= 0:
        top = 1.0
    step = nice_step(top)
    count = math.ceil(top / step - 1e-9)
    return [round(i * step, 10) for i in range(count + 1)]


def _tick_text(value: float, step: float, prefix: str, suffix: str) -> str:
    decimals = 0 if step >= 1 else max(0, -math.floor(math.log10(step)))
    return f"{prefix}{value:.{decimals}f}{suffix}"


# ---- SVG --------------------------------------------------------------------------------------
def _num(x: float) -> str:
    return f"{x:.1f}"


def _text(x: float, y: float, content: str, size: int, fill: str, weight: str = "400",
          anchor: str = "start") -> str:
    return (f'<text x="{_num(x)}" y="{_num(y)}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{escape(content)}</text>')


def legend_entries(rows: Sequence[ArmRow]) -> list[tuple[str, str]]:
    """(family label, saturated colour) for each family present, in FAMILIES order."""
    present = {row.family for row in rows}
    known = [(f, c) for _, f, c in FAMILIES if f in present]
    return known + ([FALLBACK_FAMILY] if FALLBACK_FAMILY[0] in present else [])


def render_chart(rows: Sequence[ArmRow], title: str, unit: str) -> str:
    """The SVG text of one chart. `unit` is 'cost' (dollar prefix) or 'time' (min suffix)."""
    assert unit in ("cost", "time")
    prefix, suffix = ("$", "") if unit == "cost" else ("", " min")
    max_value = max((p.value for r in rows for p in r.points), default=0.0)
    ticks = axis_ticks(max_value)
    limit = ticks[-1]
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    n_runs = sum(len(r.points) for r in rows)

    card_x, card_y = CARD_MARGIN, CARD_MARGIN
    annotate = unit == "cost"
    width = WIDTH_ANNOTATED if annotate else WIDTH
    card_w = width - 2 * CARD_MARGIN
    plot_left = card_x + PAD_X + GUTTER_WIDTH
    plot_right = plot_left + PLOT_WIDTH
    annot_x = plot_right + PLOT_RIGHT_PAD + ANNOT_GAP
    assert not annotate or annot_x + sum(ANNOT_FIELD_WIDTHS) <= card_x + card_w - PAD_X + 1e-6
    range_limit = annot_x - ANNOT_GAP if annotate else plot_right + PLOT_RIGHT_PAD
    scale = (plot_right - plot_left) / limit
    rows_top = card_y + HEADER_TOP + SUBTITLE_GAP + LEGEND_GAP + LEGEND_TO_ROWS
    rows_bottom = rows_top + ROW_HEIGHT * len(rows)
    card_h = rows_bottom - card_y + AXIS_HEIGHT + CARD_BOTTOM_PAD
    height = card_h + 2 * CARD_MARGIN

    def x_of(v: float) -> float:
        return plot_left + v * scale

    out: list[str] = []
    add = out.append
    add('<?xml version="1.0" encoding="UTF-8"?>')
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{_num(height)}" '
        f'viewBox="0 0 {width} {_num(height)}" role="img" aria-label="{escape(title)}" '
        f'font-family=\'{FONT_STACK.replace(chr(34), "&quot;")}\'>')
    add(f'<title>{escape(title)}</title>')
    add(f'<rect x="{card_x}" y="{card_y}" width="{card_w}" height="{_num(card_h)}" rx="{CARD_RADIUS}" '
        f'fill="{CARD_FILL}" stroke="{CARD_STROKE}" stroke-width="1"/>')

    left = card_x + PAD_X
    title_y = card_y + HEADER_TOP
    subtitle_y = title_y + SUBTITLE_GAP
    legend_y = subtitle_y + LEGEND_GAP
    add(_text(left, title_y, title, TITLE_SIZE, TEXT_COLOR, "700"))
    add(_text(left, subtitle_y,
              f"{n_runs} runs across {len(rows)} arms; bar = min to max, dot = one run, tick = median, "
              "light tint = conservative",
              SUBTITLE_SIZE, MUTED_COLOR))

    # Legend: colour families present, then the hollow marker.
    lx = float(left)
    for label, color in legend_entries(rows):
        add(f'<circle cx="{_num(lx + LEGEND_SWATCH_RADIUS)}" cy="{_num(legend_y)}" '
            f'r="{LEGEND_SWATCH_RADIUS}" fill="{color}"/>')
        add(_text(lx + 2 * LEGEND_SWATCH_RADIUS + 6, legend_y + 4, label, LEGEND_FONT_SIZE, MUTED_COLOR))
        lx += 2 * LEGEND_SWATCH_RADIUS + 6 + len(label) * LEGEND_CHAR_WIDTH + LEGEND_ITEM_GAP
    add(f'<circle cx="{_num(lx + LEGEND_SWATCH_RADIUS)}" cy="{_num(legend_y)}" r="{LEGEND_SWATCH_RADIUS - 0.9}" '
        f'fill="{CARD_FILL}" stroke="{FAINT_COLOR}" stroke-width="{HOLLOW_STROKE_WIDTH}"/>')
    add(_text(lx + 2 * LEGEND_SWATCH_RADIUS + 6, legend_y + 4, "did not pass", LEGEND_FONT_SIZE, MUTED_COLOR))
    legend_end = lx + 2 * LEGEND_SWATCH_RADIUS + 6 + len("did not pass") * LEGEND_CHAR_WIDTH
    assert legend_end <= (annot_x - ANNOT_GAP if annotate else card_x + card_w - PAD_X), "legend overflows its row"
    if annotate:
        add(_text(annot_x, legend_y + 4, ANNOT_HEADER, LEGEND_FONT_SIZE, MUTED_COLOR))

    # Row stripes, grid and axis.
    for i in range(len(rows)):
        if i % 2 == 0:
            add(f'<rect x="{left - 8}" y="{_num(rows_top + i * ROW_HEIGHT)}" width="{_num(card_w - 2 * PAD_X + 16)}" '
                f'height="{ROW_HEIGHT}" rx="6" fill="{ROW_STRIPE_COLOR}"/>')
    for t in ticks:
        gx = x_of(t)
        add(f'<line x1="{_num(gx)}" y1="{_num(rows_top)}" x2="{_num(gx)}" y2="{_num(rows_bottom)}" '
            f'stroke="{GRID_COLOR}" stroke-opacity="{GRID_OPACITY}" stroke-width="1"/>')
        add(_text(gx, rows_bottom + 20, _tick_text(t, step, prefix, suffix), TICK_LABEL_SIZE, MUTED_COLOR,
                  anchor="middle"))
    add(f'<line x1="{_num(plot_left)}" y1="{_num(rows_bottom)}" x2="{_num(plot_right)}" y2="{_num(rows_bottom)}" '
        f'stroke="{AXIS_COLOR}" stroke-width="1"/>')

    fmt = "{:.2f}" if unit == "cost" else "{:.1f}"
    for i, row in enumerate(rows):
        cy = rows_top + i * ROW_HEIGHT + ROW_HEIGHT / 2
        add(_text(left, cy - 3, row.arm, ARM_ID_SIZE, TEXT_COLOR, "700"))
        add(_text(left + ARM_ID_WIDTH, cy - 3, row.description, DESC_SIZE, MUTED_COLOR))
        add(_text(left + ARM_ID_WIDTH, cy + 11, f"n={len(row.points)}", COUNT_SIZE, FAINT_COLOR))
        values = [p.value for p in row.points]
        lo, hi = min(values), max(values)
        bar_x = x_of(lo) - BAR_HEIGHT / 2
        bar_w = max(x_of(hi) - x_of(lo), 0.0) + BAR_HEIGHT
        add(f'<rect x="{_num(bar_x)}" y="{_num(cy - BAR_HEIGHT / 2)}" width="{_num(bar_w)}" height="{BAR_HEIGHT}" '
            f'rx="{BAR_HEIGHT / 2}" fill="{row.color}" fill-opacity="{BAR_OPACITY}"/>')
        for k, p in enumerate(row.points):
            dy = JITTER * JITTER_PATTERN[k % len(JITTER_PATTERN)]
            px, py = x_of(p.value), cy + dy
            if p.passed:
                add(f'<circle cx="{_num(px)}" cy="{_num(py)}" r="{DOT_RADIUS}" fill="{row.color}" '
                    f'fill-opacity="{DOT_OPACITY}"/>')
            else:
                add(f'<circle cx="{_num(px)}" cy="{_num(py)}" r="{DOT_RADIUS}" fill="{CARD_FILL}" '
                    f'stroke="{row.color}" stroke-width="{HOLLOW_STROKE_WIDTH}"/>')
        mx = x_of(statistics.median(values))
        add(f'<line x1="{_num(mx)}" y1="{_num(cy - MEDIAN_HALF_HEIGHT)}" x2="{_num(mx)}" '
            f'y2="{_num(cy + MEDIAN_HALF_HEIGHT)}" stroke="{MEDIAN_COLOR}" stroke-width="{MEDIAN_WIDTH}" '
            f'stroke-linecap="round"/>')
        low_t, high_t = fmt.format(lo), fmt.format(hi)
        label = low_t if low_t == high_t else f"{low_t} to {high_t}"
        label_x = min(x_of(hi) + BAR_HEIGHT / 2 + 8,
                      range_limit - len(label) * RANGE_CHAR_WIDTH)
        add(_text(label_x, cy + 4, label, RANGE_TEXT_SIZE, TEXT_COLOR))
        if annotate:
            cells = (f"in {humanize(row.maxima.input)}", f"out {humanize(row.maxima.output)}",
                     f"cached {humanize(row.maxima.cache_read)}", f"ctx {humanize(row.maxima.ctx)}",
                     f"sub ctx {humanize(row.maxima.sub_ctx)}")
            cell_x = float(annot_x)
            for cell, cell_w in zip(cells, ANNOT_FIELD_WIDTHS):
                add(f'<text x="{_num(cell_x)}" y="{_num(cy + 4)}" font-size="{ANNOT_FONT_SIZE}" fill="{MUTED_COLOR}" '
                    f'font-family=\'{ANNOT_FONT_STACK.replace(chr(34), "&quot;")}\'>{escape(cell)}</text>')
                cell_x += cell_w
    add('</svg>')
    return "\n".join(out) + "\n"


# ---- CLI --------------------------------------------------------------------------------------
def task_label(task: str) -> str:
    found = re.fullmatch(r"task(\d+)", task)
    return f"Task {found.group(1)}" if found else task


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs-dir", type=Path, required=True)
    p.add_argument("--task", required=True, help="task1..task4")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--scenario", choices=cost.SCENARIOS, default=cost.PRIMARY_SCENARIO)
    p.add_argument("--drop-status", nargs="+", default=(), metavar="STATUS",
                   help="exclude attempts with these statuses (only completed runs are charted anyway)")
    a = p.parse_args(argv)
    if not a.runs_dir.is_dir():
        print(f"charts: error: runs dir {a.runs_dir} does not exist", file=sys.stderr)
        return 1
    task = aggregate.short_task(a.task)
    runs = aggregate.load_runs(a.runs_dir)
    try:
        a.out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"charts: error: cannot create {a.out_dir}: {exc}", file=sys.stderr)
        return 1
    label = task_label(task)
    titles = {"cost": f"{label}: {a.scenario} cost per run", "time": f"{label}: wall time per run"}
    for metric, name in CHART_FILES.items():
        rows = build_rows(runs, task, metric, a.scenario, a.drop_status)
        if not rows:
            print(f"charts: error: no completed {task} runs with {metric} data in {a.runs_dir}", file=sys.stderr)
            return 1
        missing = sum(1 for r in runs if r.get("task") == task and r.get("status") == "completed"
                      and r.get("status") not in set(a.drop_status)) - sum(len(r.points) for r in rows)
        if missing:
            print(f"charts: warning: {missing} completed {task} run(s) lack {metric} data and are not drawn",
                  file=sys.stderr)
        path = a.out_dir / name.format(task=task)
        try:
            path.write_text(render_chart(rows, titles[metric], metric), encoding="utf-8")
        except OSError as exc:
            print(f"charts: error: cannot write {path}: {exc}", file=sys.stderr)
            return 1
        print(f"charts: wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
