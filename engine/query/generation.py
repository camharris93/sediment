"""L2 — Constrained generation. Intent + grounded schema -> DuckDB SQL.

The prompt constrains the model to the grounded schema (the sole source of
truth). On an L3/L4 failure this is called again with the prior SQL + structured
violations so the generator gets a concrete diff to fix, not a generic retry.
"""
from __future__ import annotations

import re

from .grounding import GroundingContext, to_prompt_summary
from .intent import Intent
from .static_validation import ValidationResult

_SYSTEM = """\
You are the GENERATION layer (L2) of a trust-first NL->SQL agent. Translate a
structured Intent into DuckDB SQL. Your output is validated by L3 (independent
static check) and L4 (DuckDB EXPLAIN); violations trigger a retry with a specific
error you must fix.

Hard rules:
  1. Dialect = DuckDB SQL.
  2. Reference tables as `schema.table` exactly as in the grounded schema.
  3. Reference ONLY tables/columns present in the schema. Never invent columns.
     If the question implies a column that isn't there, use the closest real
     proxy and note it with a `-- assumption: ...` comment.
  4. Use `JOIN ... ON ...`, never `USING (col)`.
  5. Respect derived cardinalities — pre-aggregate a 1:many child before
     aggregating parent columns, or you fan out rows.
  6. Add an explicit `LIMIT` for "top/list" questions. Avoid `SELECT *` in
     aggregates.
  7. Output ONLY the SQL — no markdown, no fences, no commentary.

-------- Grounded schema (source of truth) --------
{schema}
"""


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:sql)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip().rstrip(";")


def _format_intent(intent: Intent) -> str:
    lines = ["Intent:",
             f"  restated_question: {intent.restated_question}",
             f"  target_tables: {', '.join(intent.target_tables) or '(none)'}",
             f"  expected_result_grain: {intent.expected_result_grain}"]
    if intent.metrics:
        lines.append(f"  metrics: {', '.join(intent.metrics)}")
    if intent.filters:
        lines.append(f"  filters: {', '.join(intent.filters)}")
    if intent.date_range:
        lines.append(f"  date_range: {intent.date_range}")
    if intent.assumptions:
        lines.append("  assumptions:")
        lines += [f"    - {a}" for a in intent.assumptions]
    return "\n".join(lines)


def generate_sql(intent: Intent, ctx: GroundingContext, *,
                 prior_sql: str | None = None,
                 prior_validation: ValidationResult | None = None):
    from .._llm import complete
    from ..config import get_ai_settings

    system = _SYSTEM.format(schema=to_prompt_summary(ctx))
    parts = [_format_intent(intent)]
    if prior_sql and prior_validation and not prior_validation.ok:
        parts.append(
            "\nYour PREVIOUS attempt failed validation against the live schema "
            "(the schema is the source of truth — change your SQL to comply):\n"
            f"Previous SQL:\n{prior_sql}\n\n{prior_validation.to_feedback()}\n\n"
            "Emit corrected SQL that resolves every violation. Output ONLY the SQL."
        )
    text, usage = complete(
        system=system, user="\n\n".join(parts),
        model=get_ai_settings().model_l2, max_tokens=2000,
    )
    return _strip_fence(text), usage
