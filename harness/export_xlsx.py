#!/usr/bin/env python3
"""Export experiment results to one Excel workbook (standard library only).

Per task the workbook holds a `T<n> summary` sheet (one row per arm, the statistics of the
Markdown tables with every range split into numeric min/max columns) and a `T<n> runs` sheet (one
row per attempt, including attempts excluded from the summary, flagged by `in_results` and
`excluded_reason`). Each data range is an Excel Table. The workbook is written with `zipfile` and
hand-written SpreadsheetML; the output is deterministic for a given input (fixed zip timestamps,
stable ordering, and a generation time taken from the newest run unless `--generated-at` is given).
"""
from __future__ import annotations

import argparse
import io
import re
import statistics
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

import aggregate
import charts
import cost

# ---- Cell kinds and styles --------------------------------------------------------------------
# Kind -> index into the cellXfs of STYLES_XML.
STYLE_OF_KIND: dict[str, int] = {"text": 0, "int": 1, "usd": 2, "dec1": 3, "datetime": 4, "ratio": 2, "bool": 0}
README_STYLE = 5
TABLE_STYLE = "TableStyleMedium2"
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
MAX_CELL_CHARS = 32767          # Excel's per-cell text limit
MAX_COLUMN_WIDTH = 60
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
# Characters XML 1.0 cannot carry (control characters other than tab, LF, CR; lone noncharacters).
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")

STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm:ss"/>'
    '<numFmt numFmtId="165" formatCode="0.0"/></numFmts>'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')

Cell = str | int | float | bool | datetime | None


@dataclass(frozen=True)
class Column:
    """One table column: its header text, value kind (a STYLE_OF_KIND key) and minimum width."""
    name: str
    kind: str
    width: int = 10


@dataclass(frozen=True)
class Sheet:
    """A worksheet: its name, an optional Excel Table name, header columns and data rows."""
    name: str
    table: str | None
    columns: tuple[Column, ...]
    rows: tuple[tuple[Cell, ...], ...]


# ---- Run classification -----------------------------------------------------------------------
def classify(runs: Sequence[dict[str, Any]], superseded: dict[str, str],
             drop_status: Sequence[str]) -> list[tuple[dict[str, Any], str | None]]:
    """Each run with its exclusion reason, or None when it belongs to the results.

    A superseded run is excluded first (with the reason text from the superseded file); of the
    rest, those whose status is in `drop_status` are excluded. This is the order aggregate.py uses.
    """
    dropped = set(drop_status)
    out: list[tuple[dict[str, Any], str | None]] = []
    for r in runs:
        if r["_dir"] in superseded:
            out.append((r, f"superseded: {superseded[r['_dir']]}"))
        elif r.get("status") in dropped:
            out.append((r, f"dropped status: {r.get('status')}"))
        else:
            out.append((r, None))
    return out


# ---- Per-run metrics --------------------------------------------------------------------------
def parse_utc(text: Any) -> datetime | None:
    """A `...Z` or offset ISO timestamp as an aware datetime; None when absent or unparseable."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def serial(when: datetime | None) -> float | None:
    """Excel date serial (days since 1899-12-30, UTC) of an aware datetime."""
    return None if when is None else (when - EXCEL_EPOCH).total_seconds() / 86400.0


def is_codex_model(model: str) -> bool:
    return model.startswith("gpt")


def split_cost(run: dict[str, Any], scenario: str) -> tuple[float | None, float | None]:
    """(Claude USD, Codex USD) of a run in `scenario`; (None, None) without cost data."""
    report = run.get("cost")
    if not report:
        return None, None
    by_model = report["scenarios"].get(scenario, {}).get("by_model")
    if by_model is None:
        return None, None
    codex = sum(v for m, v in by_model.items() if is_codex_model(m))
    claude = sum(v for m, v in by_model.items() if not is_codex_model(m))
    return claude, codex


def token_total(run: dict[str, Any], keys: Sequence[str]) -> float | None:
    """Sum of the named token fields over every model and thread; None without token data."""
    tokens = (run.get("cost") or {}).get("tokens")
    if not tokens:
        return None
    return sum(float(t.get(k) or 0) for t in tokens.values() for k in keys)


def wall_minutes(run: dict[str, Any]) -> float | None:
    """Wall time in minutes, from the same `totals.wall_time_s` the Markdown table uses."""
    seconds = (run.get("totals") or {}).get("wall_time_s")
    return None if seconds is None else seconds / 60.0


def behavior_value(run: dict[str, Any], key: str) -> Any:
    return (run.get("behavior") or {}).get(key)


def clean_text(value: Any) -> str:
    """Text safe for a cell: invalid XML characters shown as \\xNN escapes, length capped."""
    text = _INVALID_XML.sub(lambda m: f"\\x{ord(m.group(0)):02x}", str(value))
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS - 3] + "..."


RUN_COLUMNS: tuple[Column, ...] = (
    Column("run_id", "text", 10), Column("arm", "text", 6), Column("arm_description", "text", 40),
    Column("rep", "int", 5), Column("status", "text", 12), Column("passed", "bool", 8),
    Column("rounds_used", "int", 11), Column("cap_hit", "bool", 8),
    Column("started_utc", "datetime", 20), Column("finished_utc", "datetime", 20),
    Column("wall_minutes", "dec1", 12), Column("billed_total_usd", "usd", 14),
    Column("claude_usd", "usd", 11), Column("codex_usd", "usd", 11),
    Column("turns", "int", 7), Column("spawns", "int", 8), Column("compactions", "int", 12),
    Column("input_tokens", "int", 13), Column("output_tokens", "int", 13),
    Column("cache_read_tokens", "int", 17), Column("cache_write_tokens", "int", 18),
    Column("main_ctx_peak_tokens", "int", 20), Column("subagent_ctx_peak_tokens", "int", 24),
    Column("in_results", "bool", 11), Column("excluded_reason", "text", 40), Column("error", "text", 40),
)


def run_row(run: dict[str, Any], reason: str | None, scenario: str) -> tuple[Cell, ...]:
    """One `runs` row, in RUN_COLUMNS order. The total column carries the chosen scenario's cost."""
    claude, codex = split_cost(run, scenario)
    totals = run.get("totals") or {}
    arm = run.get("arm", "?")
    error = run.get("error")
    row: tuple[Cell, ...] = (
        run.get("run_id") or run["_dir"], arm, aggregate.ARM_DESCRIPTIONS.get(arm, "-"),
        run.get("rep"), run.get("status"), run.get("passed") if isinstance(run.get("passed"), bool) else None,
        run.get("rounds_used"), run.get("cap_hit") if isinstance(run.get("cap_hit"), bool) else None,
        parse_utc(run.get("started_at")), parse_utc(run.get("finished_at")),
        wall_minutes(run), aggregate.scenario_cost(run, scenario), claude, codex,
        totals.get("turns"), behavior_value(run, "subagent_spawns"), behavior_value(run, "compaction_events"),
        token_total(run, ("input",)), token_total(run, ("output",)), token_total(run, ("cache_read",)),
        token_total(run, ("cache_write_5m", "cache_write_1h")),
        behavior_value(run, "max_context_main"), behavior_value(run, "max_context_subagent"),
        reason is None, reason, None if error is None else clean_text(error))
    assert len(row) == len(RUN_COLUMNS)
    return row


# ---- Summary ----------------------------------------------------------------------------------
def _col(name: str, kind: str, width: int = 10) -> Column:
    return Column(name, kind, width)


def _min_med_max(label: str, kind: str, width: int) -> tuple[Column, Column, Column]:
    return (_col(f"{label} min", kind, width), _col(f"{label} median", kind, width + 3),
            _col(f"{label} max", kind, width))


def _min_max_columns(label: str, kind: str, width: int) -> tuple[Column, Column]:
    return _col(f"{label} min", kind, width), _col(f"{label} max", kind, width)


SUMMARY_COLUMNS: tuple[Column, ...] = (
    _col("Arm", "text", 6), _col("Description", "text", 50), _col("Passes", "int", 8), _col("Attempts", "int", 9),
    _col("Spend per pass USD", "usd", 18), _col("Spend per pass vs A1", "ratio", 20),
    _col("Total spend USD", "usd", 15),
    *_min_med_max("Cost per run USD", "usd", 17), _col("Cost spread USD", "usd", 15),
    _col("Claude cost per run USD median", "usd", 29), _col("Codex cost per run USD median", "usd", 29),
    *_min_med_max("Wall min per run", "dec1", 16),
    *_min_max_columns("Turns per run", "int", 15), *_min_max_columns("Spawns", "int", 11),
    *_min_max_columns("Rounds", "int", 11), _col("Runs over one round", "int", 19),
    *_min_max_columns("Compactions", "int", 15),
    *_min_max_columns("Main ctx peak (K tokens)", "dec1", 24),
    *_min_max_columns("Subagent ctx peak (K tokens)", "dec1", 28),
)
assert len({c.name for c in SUMMARY_COLUMNS}) == len(SUMMARY_COLUMNS)
assert len({c.name for c in RUN_COLUMNS}) == len(RUN_COLUMNS)


def _min_max(values: Sequence[float]) -> tuple[float | None, float | None]:
    return (min(values), max(values)) if values else (None, None)


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def summary_rows(runs: Sequence[dict[str, Any]], task: str, scenario: str) -> list[tuple[Cell, ...]]:
    """One row per arm over `runs` (those in the results), with aggregate.render's statistics."""
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if r["task"] == task:
            by_arm[r["arm"]].append(r)
    per_pass: dict[str, float | None] = {}
    for arm, group in by_arm.items():
        passes = sum(1 for r in group if r.get("status") == "completed" and r.get("passed"))
        costs = [c for c in (aggregate.scenario_cost(r, scenario) for r in group) if c is not None]
        per_pass[arm] = sum(costs) / passes if passes else None
    baseline = per_pass.get("A1")
    rows: list[tuple[Cell, ...]] = []
    for arm in sorted(by_arm, key=charts.arm_number):
        group = by_arm[arm]
        passes = sum(1 for r in group if r.get("status") == "completed" and r.get("passed"))
        costs = [c for c in (aggregate.scenario_cost(r, scenario) for r in group) if c is not None]
        splits = [split_cost(r, scenario) for r in group]
        claude = [s[0] for s in splits if s[0] is not None]
        codex = [s[1] for s in splits if s[1] is not None]
        totals = [(r.get("totals") or {}) for r in group]
        behaviors = [(r.get("behavior") or {}) for r in group]
        walls = [t["wall_time_s"] / 60 for t in totals if t.get("wall_time_s") is not None]
        turns = [t["turns"] for t in totals if t.get("turns") is not None]
        spawns = [b["subagent_spawns"] for b in behaviors if "subagent_spawns" in b]
        rounds = [r.get("rounds_used", 0) for r in group]
        compactions = [b["compaction_events"] for b in behaviors if "compaction_events" in b]
        main_ctx = [b["max_context_main"] / 1000 for b in behaviors if b.get("max_context_main") is not None]
        sub_ctx = [b["max_context_subagent"] / 1000 for b in behaviors if b.get("max_context_subagent") is not None]
        relative = per_pass[arm] / baseline if baseline and per_pass[arm] is not None else None
        c_lo, c_hi = _min_max(costs)
        w_lo, w_hi = _min_max(walls)
        row: tuple[Cell, ...] = (
            arm, aggregate.ARM_DESCRIPTIONS.get(arm, "-"), passes, len(group),
            per_pass[arm], relative, sum(costs) if costs else None,
            c_lo, _median(costs), c_hi, None if not costs else c_hi - c_lo,
            _median(claude), _median(codex),
            w_lo, _median(walls), w_hi,
            *_min_max(turns), *_min_max(spawns), *_min_max(rounds), sum(1 for n in rounds if n > 1),
            *_min_max(compactions), *_min_max(main_ctx), *_min_max(sub_ctx))
        assert len(row) == len(SUMMARY_COLUMNS)
        rows.append(row)
    return rows


# ---- SpreadsheetML ----------------------------------------------------------------------------
def column_letters(index: int) -> str:
    """0-based column index as A, B, ..., Z, AA, ..."""
    assert index >= 0
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


class SharedStrings:
    """Insertion-ordered shared string table."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self.count = 0

    def add(self, text: str) -> int:
        self.count += 1
        return self._index.setdefault(text, len(self._index))

    def xml(self) -> str:
        items = "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in self._index)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                f'count="{self.count}" uniqueCount="{len(self._index)}">{items}</sst>')


def cell_xml(ref: str, value: Cell, kind: str, strings: SharedStrings, style: int | None = None) -> str:
    """One `<c>` element; empty for None. Numbers stay numeric, booleans use t="b", dates are serials."""
    if value is None:
        return ""
    s = STYLE_OF_KIND[kind] if style is None else style
    attr = f' s="{s}"' if s else ""
    if isinstance(value, bool):
        return f'<c r="{ref}"{attr} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, datetime):
        return f'<c r="{ref}"{attr}><v>{serial(value)!r}</v></c>'
    if isinstance(value, (int, float)):
        assert value == value and abs(value) != float("inf"), f"non-finite number at {ref}"
        return f'<c r="{ref}"{attr}><v>{value!r}</v></c>'
    return f'<c r="{ref}"{attr} t="s"><v>{strings.add(clean_text(value))}</v></c>'


def column_widths(columns: Sequence[Column], rows: Sequence[Sequence[Cell]]) -> list[int]:
    widths: list[int] = []
    for i, col in enumerate(columns):
        longest = max((len(str(r[i])) for r in rows if isinstance(r[i], str)), default=0)
        widths.append(min(MAX_COLUMN_WIDTH, max(col.width, len(col.name) + 4, min(longest, MAX_COLUMN_WIDTH) + 2)))
    return widths


def table_ref(sheet: Sheet) -> str:
    """A1-style range of the table: header plus data rows (at least one, as Excel requires)."""
    last_row = 1 + max(1, len(sheet.rows))
    return f"A1:{column_letters(len(sheet.columns) - 1)}{last_row}"


def sheet_xml(sheet: Sheet, strings: SharedStrings, has_table: bool) -> str:
    """The worksheet part: frozen header row, column widths, cells, and the table part reference."""
    ncols = len(sheet.columns)
    widths = column_widths(sheet.columns, sheet.rows)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           f'<dimension ref="{table_ref(sheet)}"/>',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
           'activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/>', "<cols>"]
    out += [f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)]
    out.append("</cols><sheetData>")
    out.append('<row r="1">' + "".join(
        cell_xml(f"{column_letters(i)}1", c.name, "text", strings) for i, c in enumerate(sheet.columns)) + "</row>")
    for n, row in enumerate(sheet.rows, start=2):
        assert len(row) == ncols, f"{sheet.name} row {n}: {len(row)} cells for {ncols} columns"
        cells = "".join(cell_xml(f"{column_letters(i)}{n}", v, sheet.columns[i].kind, strings)
                        for i, v in enumerate(row))
        out.append(f'<row r="{n}">{cells}</row>')
    if not sheet.rows:
        out.append('<row r="2"/>')
    out.append("</sheetData>")
    if has_table:
        out.append('<tableParts count="1"><tablePart r:id="rId1"/></tableParts>')
    out.append("</worksheet>")
    return "".join(out)


def readme_sheet_xml(lines: Sequence[str], strings: SharedStrings) -> str:
    rows = "".join(f'<row r="{n}">{cell_xml(f"A{n}", line, "text", strings, README_STYLE)}</row>'
                   for n, line in enumerate(lines, start=1) if line)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="A1:A{len(lines)}"/><sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/><cols><col min="1" max="1" width="130" customWidth="1"/></cols>'
            f'<sheetData>{rows}</sheetData></worksheet>')


def table_xml(sheet: Sheet, table_id: int) -> str:
    assert sheet.table is not None
    ref = table_ref(sheet)
    cols = "".join(f'<tableColumn id="{i + 1}" name="{escape(c.name, {chr(34): "&quot;"})}"/>'
                   for i, c in enumerate(sheet.columns))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'id="{table_id}" name="{sheet.table}" displayName="{sheet.table}" ref="{ref}" totalsRowShown="0">'
            f'<autoFilter ref="{ref}"/><tableColumns count="{len(sheet.columns)}">{cols}</tableColumns>'
            f'<tableStyleInfo name="{TABLE_STYLE}" showFirstColumn="0" showLastColumn="0" '
            'showRowStripes="1" showColumnStripes="0"/></table>')


def valid_sheet_name(name: str) -> bool:
    return 0 < len(name) <= 31 and not re.search(r"[\[\]:*?/\\]", name) and name == name.strip("'")


def valid_table_name(name: str) -> bool:
    """Excel table names: letters, digits, underscores and dots; no leading digit; not a cell reference."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name)) and not re.fullmatch(r"[A-Za-z]{1,3}\d+", name)


def assemble(readme: Sequence[str], sheets: Sequence[Sheet]) -> bytes:
    """Zip the workbook parts. Sheet 1 is the README; each `sheets` entry with a table gets a table part."""
    names = ["README"] + [s.name for s in sheets]
    assert len(set(n.lower() for n in names)) == len(names), "duplicate sheet names"
    assert all(valid_sheet_name(n) for n in names), f"invalid sheet name in {names}"
    tables = [s.table for s in sheets if s.table]
    assert len(set(t.lower() for t in tables)) == len(tables), "duplicate table names"
    assert all(valid_table_name(t) for t in tables), f"invalid table name in {tables}"
    strings = SharedStrings()
    parts: dict[str, str] = {}
    content_overrides = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.spreadsheetml.sheet.main+xml"/>',
                         '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.spreadsheetml.styles+xml"/>',
                         '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.spreadsheetml.sharedStrings+xml"/>']
    sheet_refs = ['<sheet name="README" sheetId="1" r:id="rId1"/>']
    rels = ['<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'worksheet" Target="worksheets/sheet1.xml"/>']
    parts["xl/worksheets/sheet1.xml"] = readme_sheet_xml(readme, strings)
    content_overrides.append('<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
                             'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    table_id = 0
    for n, sheet in enumerate(sheets, start=2):
        has_table = sheet.table is not None
        parts[f"xl/worksheets/sheet{n}.xml"] = sheet_xml(sheet, strings, has_table)
        sheet_refs.append(f'<sheet name="{escape(sheet.name)}" sheetId="{n}" r:id="rId{n}"/>')
        rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'relationships/worksheet" Target="worksheets/sheet{n}.xml"/>')
        content_overrides.append(f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/'
                                 'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        if has_table:
            table_id += 1
            parts[f"xl/tables/table{table_id}.xml"] = table_xml(sheet, table_id)
            parts[f"xl/worksheets/_rels/sheet{n}.xml.rels"] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                f'relationships/table" Target="../tables/table{table_id}.xml"/></Relationships>')
            content_overrides.append(f'<Override PartName="/xl/tables/table{table_id}.xml" ContentType="application/'
                                     'vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>')
    nrel = len(sheets) + 2
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/styles" Target="styles.xml"/>' % nrel)
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/sharedStrings" Target="sharedStrings.xml"/>' % (nrel + 1))
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    files: dict[str, str] = {
        "[Content_Types].xml": (
            head + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>' + "".join(content_overrides) + "</Types>"),
        "_rels/.rels": (
            head + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            head + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<bookViews><workbookView/></bookViews><sheets>' + "".join(sheet_refs) + "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": (
            head + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + "</Relationships>"),
        "xl/styles.xml": STYLES_XML,
        **parts,
        "xl/sharedStrings.xml": strings.xml(),    # last: every sheet has registered its strings
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


# ---- Workbook ---------------------------------------------------------------------------------
def task_number(task: str) -> tuple[int, str]:
    found = re.fullmatch(r"task(\d+)", task)
    return (int(found.group(1)) if found else 10**9, task)


def sheet_label(task: str) -> str:
    found = re.fullmatch(r"task(\d+)", task)
    return f"T{found.group(1)}" if found else task


def newest_run_time(runs: Sequence[dict[str, Any]]) -> datetime | None:
    stamps = [t for r in runs for t in (parse_utc(r.get("finished_at")), parse_utc(r.get("started_at"))) if t]
    return max(stamps) if stamps else None


def build_workbook(runs: Sequence[dict[str, Any]], *, scenario: str, runs_dir: str,
                   tasks: Sequence[str] | None = None, superseded: dict[str, str] | None = None,
                   superseded_path: str | None = None, drop_status: Sequence[str] = (),
                   generated_at: str | None = None) -> bytes:
    """The workbook bytes for `runs` (as aggregate.load_runs returns them). Raises ValueError for a
    requested task with no runs."""
    classified = classify(runs, superseded or {}, drop_status)
    present = {r["task"] for r in runs}
    wanted = sorted({aggregate.short_task(t) for t in tasks}, key=task_number) if tasks else sorted(present, key=task_number)
    missing = [t for t in wanted if t not in present]
    if missing or not wanted:
        raise ValueError(f"no runs for task(s) {', '.join(missing) or '(any)'} in {runs_dir}")
    newest = newest_run_time(runs)
    stamp = generated_at or (newest.strftime("%Y-%m-%d %H:%M:%S UTC") if newest else "unknown")
    sheets: list[Sheet] = []
    counts: list[str] = []
    for task in wanted:
        label = sheet_label(task)
        kept = [r for r, why in classified if why is None and r["task"] == task]
        mine = sorted(((r, why) for r, why in classified if r["task"] == task),
                      key=lambda p: (charts.arm_number(p[0].get("arm", "?")), p[0].get("rep") or 0,
                                     p[0].get("run_id") or p[0]["_dir"]))
        sheets.append(Sheet(f"{label} summary", f"Summary_{label}", SUMMARY_COLUMNS,
                            tuple(summary_rows(kept, task, scenario))))
        sheets.append(Sheet(f"{label} runs", f"Runs_{label}", RUN_COLUMNS,
                            tuple(run_row(r, why, scenario) for r, why in mine)))
        counts.append(f"{task}: {len(kept)} of {len(mine)} attempts in the results")
    dropped_note = ", ".join(sorted(set(drop_status))) or "none"
    readme = [
        "Experiment results workbook",
        "",
        f"Generated: {stamp}",
        f"Runs directory: {runs_dir}",
        f"Cost scenario: {scenario} (all USD columns). Claude USD and Codex USD split the scenario's per-model "
        "cost; models whose name starts with gpt are Codex, all others are Claude.",
        f"Superseded runs excluded using: {superseded_path or 'none'} ({len(superseded or {})} listed)",
        f"Statuses dropped: {dropped_note}",
        "Attempts per task: " + "; ".join(counts),
        "",
        "Sheets",
        "T<n> summary: one row per arm, the statistics of the Markdown results table for that task over the "
        "attempts in the results. Ranges are split into min/max numeric columns; medians are added where "
        "named. Spend per pass is total spend over all attempts divided by passes; the vs A1 column divides "
        "it by A1's (A1 = 1.00). Rows are sorted by arm number.",
        "T<n> runs: one row per attempt, including attempts excluded from the summary. Filter in_results "
        "to TRUE to get exactly the summary's runs; excluded_reason says why other attempts are left out.",
        "Times are UTC. Wall minutes, turns, spawns, compactions and context peaks are the values the "
        "summary uses (context peaks are in tokens here and in thousands of tokens in the summary). Token "
        "columns are summed over every model and thread; cache write is the 5m plus 1h writes.",
        "Blank cells mean the run recorded no such value.",
    ]
    return assemble(readme, sheets)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs-dir", type=Path, default=Path("/workspace/agent-experiments-runs"))
    p.add_argument("--out", type=Path, required=True, help="the .xlsx file to write")
    p.add_argument("--scenario", choices=cost.SCENARIOS, default=cost.PRIMARY_SCENARIO)
    p.add_argument("--task", nargs="+", help="only these tasks (task1..task4 or full directory names)")
    p.add_argument("--drop-status", nargs="+", default=(), metavar="STATUS",
                   help="mark attempts with these run statuses as excluded from the results")
    p.add_argument("--superseded", type=Path, help="JSON object of run ids replaced by reruns, to mark as excluded")
    p.add_argument("--generated-at", help="generation time to print in the README (default: the newest run's "
                                         "finish time, which keeps the output deterministic)")
    a = p.parse_args(argv)
    if not a.runs_dir.is_dir():
        print(f"export_xlsx: error: runs dir {a.runs_dir} does not exist", file=sys.stderr)
        return 1
    try:
        superseded = aggregate.load_superseded(a.superseded) if a.superseded else {}
        runs = aggregate.load_runs(a.runs_dir)
        data = build_workbook(runs, scenario=a.scenario, runs_dir=str(a.runs_dir), tasks=a.task,
                              superseded=superseded, superseded_path=str(a.superseded) if a.superseded else None,
                              drop_status=a.drop_status, generated_at=a.generated_at)
    except (OSError, ValueError) as exc:
        print(f"export_xlsx: error: {exc}", file=sys.stderr)
        return 1
    try:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(data)
    except OSError as exc:
        print(f"export_xlsx: error: cannot write {a.out}: {exc}", file=sys.stderr)
        return 1
    print(f"export_xlsx: wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
