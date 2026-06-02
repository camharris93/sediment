"""Persistence for custom Report blocks — the "editable Report tab".

Saved charts live in `datasets/<name>/report_blocks.json` (a reviewable, committable
artifact). They render for EVERYONE (view + build); only adding/removing them is the
authoring capability, gated by build mode — the same governance as model-building.
A block is just {id, title, sql, spec}: a SQL query + a ChartSpec to render it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .charting import ChartSpec
from .config import DATASETS_DIR, is_build_mode
from .modeling import require_build_mode


@dataclass
class ReportBlock:
    id: str
    title: str
    sql: str
    spec: dict[str, Any] = field(default_factory=dict)

    def chart_spec(self) -> ChartSpec:
        return ChartSpec.from_dict(self.spec)


def _path(dataset: str):
    return DATASETS_DIR / dataset / "report_blocks.json"


def load_blocks(dataset: str) -> list[ReportBlock]:
    p = _path(dataset)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [ReportBlock(id=b["id"], title=b.get("title", b["id"]),
                            sql=b["sql"], spec=b.get("spec", {})) for b in raw]
    except Exception:
        return []


def _save(dataset: str, blocks: list[ReportBlock]) -> None:
    p = _path(dataset)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(b) for b in blocks], indent=2), encoding="utf-8")


def _next_id(blocks: list[ReportBlock]) -> str:
    n = 1
    existing = {b.id for b in blocks}
    while f"block_{n}" in existing:
        n += 1
    return f"block_{n}"


def add_block(dataset: str, *, title: str, sql: str, spec: ChartSpec) -> ReportBlock:
    require_build_mode()
    blocks = load_blocks(dataset)
    block = ReportBlock(id=_next_id(blocks), title=title.strip() or "Untitled",
                        sql=sql.strip(), spec=spec.to_dict())
    blocks.append(block)
    _save(dataset, blocks)
    return block


def delete_block(dataset: str, block_id: str) -> None:
    require_build_mode()
    blocks = [b for b in load_blocks(dataset) if b.id != block_id]
    _save(dataset, blocks)


def move_block(dataset: str, block_id: str, delta: int) -> None:
    require_build_mode()
    blocks = load_blocks(dataset)
    idx = next((i for i, b in enumerate(blocks) if b.id == block_id), None)
    if idx is None:
        return
    j = max(0, min(len(blocks) - 1, idx + delta))
    blocks[idx], blocks[j] = blocks[j], blocks[idx]
    _save(dataset, blocks)
