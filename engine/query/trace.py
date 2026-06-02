"""Structured trace events for the L1-L7 pipeline view (single-hop)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LayerId(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"
    L6 = "L6"
    L7 = "L7"


@dataclass
class TraceEvent:
    kind: str          # layer_start | layer_result | validation_fail | retry | final_answer
    layer: LayerId | None = None
    payload: dict[str, Any] = field(default_factory=dict)
