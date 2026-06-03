"""L1 — Intent & assumption surfacing.

Turn a natural-language question + grounded schema into a structured, AUDITABLE
intent whose `assumptions` are first-class. Ported from sql-engine; the schema is
the SOLE source of truth, and underspecified questions MUST surface assumptions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .grounding import GroundingContext, to_prompt_summary


@dataclass
class Intent:
    restated_question: str = ""
    target_tables: list[str] = field(default_factory=list)
    expected_result_grain: str = ""
    metrics: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    date_range: str | None = None
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "restated_question": self.restated_question,
            "target_tables": self.target_tables,
            "expected_result_grain": self.expected_result_grain,
            "metrics": self.metrics, "filters": self.filters,
            "date_range": self.date_range, "assumptions": self.assumptions,
        }


_SYSTEM = """\
You are the INTENT layer (L1) of a trust-first NL->SQL agent. Turn a natural-
language question into a structured, AUDITABLE intent. You do NOT write SQL.

Return a single JSON object, no markdown, EXACTLY this shape:

{{
  "restated_question": "<unambiguous restatement>",
  "target_tables": ["schema.table", ...],
  "expected_result_grain": "<what one result row represents>",
  "metrics": ["<measure>", ...],
  "filters": ["<filter in plain English>", ...],
  "date_range": "<window or null>",
  "assumptions": ["<every interpretive call you made>", ...]
}}

Rules:
  • Every target table MUST be a `schema.table` drawn EXACTLY from the schema below.
  • `assumptions` MUST have >=1 entry whenever the question is underspecified
    (ambiguous "best/top/most", a concept not directly in the schema, a column
    choice among several plausible ones, etc.).
  • If the question needs something not in the schema, say so as an assumption
    ("proxying X with Y because X is not present").
  • Output ONLY the JSON object.

When the question is a FOLLOW-UP that builds on a prior turn (see conversation
history, if present), resolve the reference: restate it in full, and prefer
reading from the named prior-result table (e.g. `turn_1_result`) when that's the
natural thing to build on. Surface how you resolved an ambiguous reference as an
assumption.

-------- Grounded schema (source of truth) --------
{schema}

{history}
"""


def _strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    return m.group(0) if m else t


def derive_intent(question: str, ctx: GroundingContext, *, history: list | None = None):
    from .._llm import complete
    from ..config import get_ai_settings
    from .conversation import render_history

    system = _SYSTEM.format(schema=to_prompt_summary(ctx),
                            history=render_history(history or []))
    text, usage = complete(
        system=system, user=f"Question: {question}",
        model=get_ai_settings().model_l1, max_tokens=1500,
    )
    payload = json.loads(_strip_json(text), strict=False)
    intent = Intent(
        restated_question=payload.get("restated_question", question),
        target_tables=payload.get("target_tables") or [],
        expected_result_grain=payload.get("expected_result_grain", ""),
        metrics=payload.get("metrics") or [],
        filters=payload.get("filters") or [],
        date_range=payload.get("date_range"),
        assumptions=payload.get("assumptions") or [],
    )
    return intent, usage
