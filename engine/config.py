"""Central, dataset-agnostic config.

Resolves repo paths, the single warehouse file, the active dataset's `config.yml`,
and (for the AI edge layers only) the Anthropic key. Mirrors the credential
fallbacks used in the sibling sql-engine project: env var first, then a one-line
`anthropic.txt` at the repo root.

The deterministic core (ingest/profile/dbt/dashboard) imports the path helpers
here but NEVER touches `resolve_anthropic_key` — keeping the AI strictly at the
edges, per the PRD's design boundary.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

try:  # python-dotenv is only needed for the AI layers; degrade gracefully.
    from dotenv import load_dotenv

    _DOTENV = True
except Exception:  # pragma: no cover
    _DOTENV = False


REPO_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_PATH = REPO_ROOT / "warehouse.duckdb"
DATASETS_DIR = REPO_ROOT / "datasets"
DBT_PROJECT_DIR = REPO_ROOT / "dbt_project"

if _DOTENV:
    load_dotenv(REPO_ROOT / ".env")


# ─────────────────────────────────────────────────────────────────────────────
# Dataset config — the per-dataset contract (datasets/<name>/config.yml)
# ─────────────────────────────────────────────────────────────────────────────

def _norm_delim(delim):
    # A delimiter may arrive as the literal string "\\t"; normalize common escapes.
    if isinstance(delim, str):
        return delim.encode("utf-8").decode("unicode_escape")
    return delim


@dataclass
class TableSpec:
    """One source file → one raw table. A dataset has one or more of these."""
    source: str                      # path (relative to the dataset dir) or URL
    table: str                       # raw table name to land it as: raw.<table>
    delimiter: str | None = None     # e.g. "\t"; None → auto-sniff
    raw_glob: str | None = None      # optional: member to extract from a zip


@dataclass
class DatasetConfig:
    """The human-authored contract for one dataset.

    A dataset declares one OR MORE source tables. Single-table form
    (`source:`/`table:` at top level) and multi-table form (a `tables:` list)
    are both supported; both normalize to `self.tables` (a list of TableSpec)."""
    name: str
    tables: list[TableSpec]
    description: str = ""
    download_url: str | None = None  # dataset-level fetch (single-source datasets)

    @property
    def dir(self) -> Path:
        return DATASETS_DIR / self.name

    # ── Back-compat single-table accessors (first table) ──────────────────
    @property
    def table(self) -> str:
        return self.tables[0].table

    @property
    def source(self) -> str:
        return self.tables[0].source

    @property
    def delimiter(self) -> str | None:
        return self.tables[0].delimiter

    @property
    def raw_glob(self) -> str | None:
        return self.tables[0].raw_glob

    @property
    def is_multi_table(self) -> bool:
        return len(self.tables) > 1

    def resolve_source(self, spec: "TableSpec | None" = None) -> Path:
        spec = spec or self.tables[0]
        p = Path(spec.source)
        return p if p.is_absolute() else (self.dir / spec.source)


def load_dataset_config(name: str) -> DatasetConfig:
    cfg_path = DATASETS_DIR / name / "config.yml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No dataset config at {cfg_path}. A dataset folder needs a config.yml "
            "declaring at least one source: either `source:`/`table:` or a `tables:` list."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    specs: list[TableSpec] = []
    if raw.get("tables"):
        for t in raw["tables"]:
            specs.append(TableSpec(
                source=t["source"], table=t["table"],
                delimiter=_norm_delim(t.get("delimiter")),
                raw_glob=t.get("raw_glob"),
            ))
    else:
        specs.append(TableSpec(
            source=raw["source"], table=raw["table"],
            delimiter=_norm_delim(raw.get("delimiter")),
            raw_glob=raw.get("raw_glob"),
        ))

    return DatasetConfig(
        name=raw.get("name", name),
        tables=specs,
        description=raw.get("description", ""),
        download_url=raw.get("download_url"),
    )


def active_dataset() -> str:
    """The dataset the AI layers target by default. Override with DATASET env."""
    return os.environ.get("DATASET", "anage")


# ─────────────────────────────────────────────────────────────────────────────
# AI-layer settings (edges only)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AISettings:
    model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    model_l1: str = os.environ.get("ANTHROPIC_MODEL_L1", "claude-sonnet-4-6")
    model_l2: str = os.environ.get("ANTHROPIC_MODEL_L2", "claude-sonnet-4-6")
    model_l7: str = os.environ.get("ANTHROPIC_MODEL_L7", "claude-haiku-4-5")


@lru_cache(maxsize=1)
def get_ai_settings() -> AISettings:
    return AISettings()


def resolve_anthropic_key() -> str:
    """Return the Anthropic key: env var → repo-root `anthropic.txt`.

    Raised errors are caught by the AI targets, which then print a friendly
    "configure a key to use this layer" message — the core never calls this."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env.strip()
    fallback = REPO_ROOT / "anthropic.txt"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(
        "No Anthropic key found. Set ANTHROPIC_API_KEY or place a one-line "
        f"anthropic.txt at {REPO_ROOT}. (The core pipeline does not need this.)"
    )


def has_anthropic_key() -> bool:
    try:
        resolve_anthropic_key()
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Capability mode — the view/build governance seam.
#
# "view"  (default): read-only. NL->SQL chat works; model-building is refused.
#                    This is what a SHARED/deployed report runs as.
# "build" : the authoring capability — chat answers can be promoted into dbt
#           models. Granted by the local authoring entrypoint (`run.py dashboard`),
#           NOT by any user toggle, so a deployed app stays read-only by default.
#
# The boundary is enforced server-side (see engine/modeling.require_build_mode),
# not just by hiding UI. Per-user roles in one shared deployment is a deliberate
# non-goal (PRD §2); the upgrade path is a password / SSO in front of build.
# ─────────────────────────────────────────────────────────────────────────────

def app_mode() -> str:
    mode = os.environ.get("SEDIMENT_MODE", "view").strip().lower()
    return "build" if mode == "build" else "view"


def is_build_mode() -> bool:
    return app_mode() == "build"
