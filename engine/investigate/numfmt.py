"""The one number formatter. Reports, verdict reasons and skeptic reasons all
format through here, so the traceability check can match every numeral in a
report to an EvidenceCell formatted the same way."""
from __future__ import annotations

import math
from typing import Any


def fmt(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) >= 1):
        return f"{int(v):,}"
    if not math.isfinite(v):
        return str(v)
    if abs(v) < 1:
        return f"{v:.3g}"
    return f"{v:,.4g}" if abs(v) < 1e4 else f"{v:,.0f}"


def pct(v: Any) -> str:
    return f"{v:+.0%}" if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else str(v)
