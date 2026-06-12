"""AI chart suggestion — revives the sql-engine L7 "VizHint" idea as a standalone
build-time helper. Given a result's columns + a sample (and an optional natural-
language instruction), propose a chart spec for the Report tab.

The LLM knows the question's intent and the data's shape, so it picks a chart more
reliably than a column-name heuristic — but a deterministic heuristic fallback runs
when no key is configured, so the feature degrades gracefully. The spec is plain
data (no Altair import here); the dashboard renders it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

CHART_TYPES = ("none", "bar", "horizontal_bar", "line", "scatter", "comparison_bar")


@dataclass
class ChartSpec:
    chart_type: str = "none"
    x: str | None = None              # categorical / x-axis column
    y: str | None = None              # single metric (bar/horizontal_bar/line/scatter y)
    series: list[str] = field(default_factory=list)  # multi-metric (line / comparison_bar)
    color: str | None = None          # optional categorical color (scatter)
    title: str | None = None
    log_x: bool = False
    log_y: bool = False
    reasoning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> ChartSpec:
        return ChartSpec(
            chart_type=d.get("chart_type", "none"),
            x=d.get("x"), y=d.get("y"), series=list(d.get("series") or []),
            color=d.get("color"), title=d.get("title"),
            log_x=bool(d.get("log_x")), log_y=bool(d.get("log_y")),
            reasoning=d.get("reasoning"),
        )


def validate_against_columns(spec: ChartSpec, columns: list[str]) -> ChartSpec:
    """Drop a spec (to chart_type='none') if it references columns that aren't in
    the result — never crash the report on a phantom column."""
    cols = set(columns)

    def ok(c: str | None) -> bool:
        return c is None or c in cols

    if spec.chart_type not in CHART_TYPES:
        return ChartSpec(chart_type="none", reasoning=f"unknown chart_type {spec.chart_type!r}")
    if spec.chart_type == "none":
        return spec
    if not ok(spec.x):
        return ChartSpec(chart_type="none", reasoning=f"x column `{spec.x}` not in result")
    if spec.chart_type in ("bar", "horizontal_bar", "line", "scatter") and not ok(spec.y):
        return ChartSpec(chart_type="none", reasoning=f"y column `{spec.y}` not in result")
    if spec.chart_type in ("line", "comparison_bar"):
        spec.series = [s for s in spec.series if s in cols]
        if not spec.series and not ok(spec.y):
            return ChartSpec(chart_type="none", reasoning="no valid series columns")
    if not ok(spec.color):
        spec.color = None
    return spec


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic heuristic (no key) — pick a sensible default by column type.
# ─────────────────────────────────────────────────────────────────────────────

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def heuristic_chart(columns: list[str], sample_rows: list[dict]) -> ChartSpec:
    if not columns or not sample_rows:
        return ChartSpec(chart_type="none", reasoning="no data to chart")
    first = sample_rows[0]
    numeric = [c for c in columns if _is_number(first.get(c))]
    categorical = [c for c in columns if not _is_number(first.get(c))]
    if len(numeric) >= 2 and not categorical:
        return ChartSpec(chart_type="scatter", x=numeric[0], y=numeric[1],
                         reasoning="two numeric columns -> scatter")
    if categorical and numeric:
        long_labels = any(len(str(r.get(categorical[0], ""))) > 12 for r in sample_rows)
        ctype = "horizontal_bar" if (long_labels or len(sample_rows) > 10) else "bar"
        return ChartSpec(chart_type=ctype, x=categorical[0], y=numeric[0],
                         reasoning="one category + one metric -> bar")
    return ChartSpec(chart_type="none", reasoning="no clear category/metric split")


# ─────────────────────────────────────────────────────────────────────────────
# LLM suggestion
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You recommend ONE chart for a SQL result that will be pinned to a dashboard. You
are given the result's columns and a sample of rows, and optionally a natural-
language instruction for what the user wants to see.

Return a SINGLE JSON object, no markdown:

{
  "chart_type": "none | bar | horizontal_bar | line | scatter | comparison_bar",
  "x": "<exact column name for category/x-axis, or null>",
  "y": "<exact column name for the single metric, or null>",
  "series": ["<exact column name>", ...],
  "color": "<exact categorical column to color by, or null>",
  "title": "<short chart title>",
  "log_x": false, "log_y": false,
  "reasoning": "<one short sentence>"
}

Choose by the RESULT's shape:
  • none: a single scalar, or any shape a chart would mislead.
  • bar: short category list (<=30) + one metric, short labels.
  • horizontal_bar: same but long labels or 10+ rows (prefer for top-N rankings).
  • line: x is a time/sequence column with one or more numeric series.
  • scatter: two numeric columns (a relationship); set log_x/log_y when values span
    orders of magnitude (e.g. body mass). Use `color` for a categorical grouping.
  • comparison_bar: each category has 2+ grouped metrics — list them in series.

Use EXACT column names from the result. Honor the instruction if given. Output ONLY the JSON.
"""


def _strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    return m.group(0) if m else t


def suggest_chart(columns: list[str], sample_rows: list[dict], *,
                  instruction: str = "", title_hint: str = "") -> ChartSpec:
    """Propose a ChartSpec. Uses the LLM when a key is available, else a heuristic.
    Always validated against the actual columns before returning."""
    from .config import has_anthropic_key

    if not has_anthropic_key():
        spec = heuristic_chart(columns, sample_rows)
    else:
        try:
            from ._llm import complete
            from .config import get_ai_settings
            payload = {
                "columns": columns,
                "sample_rows": sample_rows[:15],
                "instruction": instruction or "(none — pick the most informative chart)",
                "title_hint": title_hint,
            }
            text, _ = complete(
                system=_SYSTEM, user=json.dumps(payload, default=str),
                model=get_ai_settings().model_l7, max_tokens=400, cache_system=False)
            spec = ChartSpec.from_dict(json.loads(_strip_json(text), strict=False))
        except Exception:
            spec = heuristic_chart(columns, sample_rows)

    if title_hint and not spec.title:
        spec.title = title_hint
    return validate_against_columns(spec, columns)
