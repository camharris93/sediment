"""AI mart reviewer — the critic that complements the scaffolder.

The scaffolder/builder PROPOSES models; the reviewer RED-TEAMS them. Given a
mart's SQL, the dataset's measured relationships, and a sample of its output, it
flags the things that quietly make a mart wrong:

  • join fan-out / double-counting (aggregating a parent column across a 1:many)
  • unclear or non-unique grain
  • missing tests (no not_null/unique on the key)
  • divide-by-zero / null-handling gaps
  • naming/readability

It returns structured findings — never rewrites the model. AI at the edge,
human commits.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .grounding import GroundingContext, to_prompt_summary


@dataclass
class ReviewFinding:
    severity: str          # "high" | "medium" | "low"
    title: str
    detail: str


@dataclass
class ReviewResult:
    summary: str
    findings: list[ReviewFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "high" for f in self.findings)


_SYSTEM = """\
You are the MART REVIEWER of a trust-first analytics framework. You are given a
dbt mart's SQL, the dataset's MEASURED table relationships (cardinality), and a
sample of the mart's actual output. Critique it for the failure modes that make a
mart silently wrong. You do NOT rewrite it — you flag issues for a human.

Respond as PLAIN LINES (no JSON, no markdown):

SUMMARY: <1-2 sentences: overall read on the mart's soundness>
FINDING: <high|medium|low> :: <short title> :: <what's wrong and how to fix, ONE line>
FINDING: ...

One FINDING per line. Each finding must stay on a single line. If the mart is
sound, write SUMMARY and zero findings (or only low-severity ones).

Look hard for:
  • JOIN FAN-OUT / double-counting: a parent-grain column aggregated (SUM/AVG/COUNT)
    across a 1:many join to a child without pre-aggregating. Use the relationships.
  • GRAIN: is one row clearly one thing? Is the key actually unique?
  • MISSING TESTS: no not_null/unique on the apparent key.
  • NULL / DIVIDE-BY-ZERO: ratios/averages that can divide by zero or propagate nulls.
  • NAMING / READABILITY: ambiguous column names, missing units.

Only report REAL issues justified by the SQL/relationships/sample.
"""


def _parse_review(text: str) -> ReviewResult:
    summary = ""
    findings: list[ReviewFinding] = []
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.upper().startswith("FINDING:"):
            parts = [p.strip() for p in line.split(":", 1)[1].split("::")]
            sev = (parts[0].lower() if parts else "low")
            sev = sev if sev in ("high", "medium", "low") else "low"
            title = parts[1] if len(parts) > 1 else ""
            detail = parts[2] if len(parts) > 2 else ""
            findings.append(ReviewFinding(severity=sev, title=title, detail=detail))
    return ReviewResult(summary=summary or "(no summary returned)", findings=findings)


def _relationships_block(ctx: GroundingContext) -> str:
    if not ctx.relationships:
        return "(no cross-table relationships measured for this dataset)"
    return "\n".join(
        f"  - {r.parent} -> {r.child} on {', '.join(r.columns)} [{r.cardinality}]"
        for r in ctx.relationships)


def review_sql(name: str, sql: str, sample_rows: list[dict[str, Any]], ctx: GroundingContext) -> ReviewResult:
    """Review a mart's SQL against the dataset's relationships + a sample of output."""
    from .._llm import complete
    from ..config import get_ai_settings, has_anthropic_key

    if not has_anthropic_key():
        return ReviewResult(summary="(AI review needs an Anthropic key; none configured.)")

    keys = list(sample_rows[0].keys()) if sample_rows else []
    sample_txt = "\n".join("    " + " | ".join(f"{k}={r.get(k)}" for k in keys)
                           for r in sample_rows[:8]) or "    (no rows)"
    user = (
        f"Mart: {name}\n\nSQL:\n{sql}\n\n"
        f"Measured relationships in this dataset:\n{_relationships_block(ctx)}\n\n"
        f"Output columns: {', '.join(keys)}\n"
        f"Sample rows:\n{sample_txt}"
    )
    try:
        text, _ = complete(system=_SYSTEM, user=user, model=get_ai_settings().model, max_tokens=1200)
        return _parse_review(text)
    except Exception as exc:
        return ReviewResult(summary=f"(review failed: {exc})")


def review_mart(dataset: str, mart_name: str) -> ReviewResult:
    """Review a BUILT mart by reading its relation + a sample from the warehouse."""
    import duckdb
    from ..config import WAREHOUSE_PATH
    from ..registry import marts_schema
    from .grounding import build_grounding_context

    schema = marts_schema(dataset)
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        cur = con.execute(f'select * from "{schema}"."{mart_name}" limit 8')
        cols = [d[0] for d in cur.description]
        sample = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    # Best-effort: read the model SQL file if present, else describe from the relation.
    from ..registry import MARTS_DIR
    sql = f"-- (model file not found; reviewing the built relation {schema}.{mart_name})"
    for f in MARTS_DIR.glob("*.sql"):
        txt = f.read_text(encoding="utf-8")
        if re.search(rf"alias\s*=\s*['\"]{re.escape(mart_name)}['\"]", txt) or f.stem.endswith(mart_name):
            sql = txt
            break
    return review_sql(mart_name, sql, sample, build_grounding_context(dataset))
