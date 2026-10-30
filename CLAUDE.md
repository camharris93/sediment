# sediment — Guide for Coding Agents

**A local-first analytics-engineering framework built on dbt.** Take any tabular dataset and run it through a complete modern data stack—ingestion → warehouse → transformation → testing → docs → visualization—with AI layered at the build, orchestration, and consumption seams, and deliberately kept out of the deterministic transform path.

## What This Repo Is

sediment is a dataset-agnostic analytics framework demonstrating the principle: **deterministic core, AI at the edges**. The raw → staging → marts flow is hand-written, version-controlled, tested dbt SQL that produces identical results on every run. AI assists in *building* it (scaffolding), *operating* it (orchestration/monitoring), and *querying* it (NL→SQL)—but never executes the transforms. One command, no cloud accounts, no API keys for the core data flow, runs offline.

The reference dataset is AnAge (Animal Ageing & Longevity Database, ~4,600 species). Two curated marts answer questions like "which animals live far longer than their body size predicts?"

## Setup & Running

**Prerequisites:** Python 3.10+ only. Optional: Anthropic API key for AI layers; `make` for convenience aliases.

```bash
# Install
pip install -r requirements.txt

# Build the entire pipeline (offline, no key needed for core)
python run.py up                      # download → load → profile → dbt run → dbt test
# or: make up

# Explore
python run.py dashboard               # Streamlit dashboard at http://localhost:8501
python run.py docs                    # dbt lineage graph

# AI layers (need ANTHROPIC_API_KEY env or anthropic.txt file)
python run.py scaffold <name> --write # generate staging + tests, wire into dbt
python run.py orchestrate anage --break  # run + monitor + explain injected failure
python run.py ask "which animals live longest for their size?"

# Test & lint
pytest -m "not live"                  # deterministic suite (offline, no key)
ruff check .                          # must be clean before PR
python evals/harness.py               # NL→SQL golden questions (replay mode, no key)
```

Every target takes an optional dataset name (default `anage`). With `make`, pass `DATASET=...`.

**Install as a tool:** `pip install -e .` installs the `sediment` command (same as `python run.py`). Use `sediment init ~/my-project` to scaffold a fresh workspace.

## Directory Layout

```
engine/                 dataset-agnostic framework (written once)
  config.py             paths, dataset config, key resolution
  ingest.py             any file → raw.<table>
  profile.py            raw table → profile.json (nulls, cardinality, types)
  scaffold.py           profile → proposed dbt code (AI, build-time)
  orchestrate.py        run/monitor/explain the core (AI, run-time)
  modeling.py           promote chat answer → dbt model (AI, build, gated)
  charting.py           suggest chart specs for reports (AI, build, gated)
  report_config.py      persist custom report blocks
  registry.py           dataset→marts mapping via dbt manifest
  query/                NL→SQL agent over marts (AI, consumption)
    grounding.py  intent.py  generation.py  static_validation.py
    dry_run.py  execution.py  plausibility.py  translation.py  orchestrator.py
  investigate/          planted-truth eval framework (investigation layer)

datasets/               per-dataset human-authored contracts
  anage/
    config.yml          source, table name, delimiter—entire dataset contract
    data/               source file (AnAge ships committed, runs fully offline)
    scaffold/           generated proposals for human review
    report_blocks.json  AI-built charts pinned to the report (committable)
  saasco/               planted-truth synthetic dataset (investigation evals)
  saasco_holdout/       holdout dataset (investigation evals)

dbt_project/            dbt Core project (DuckDB warehouse)
  models/staging/       stg_anage + _sources.yml + inferred tests
  models/marts/         mart_longevity_by_class, mart_aging_outliers (curated)
  dbt_project.yml       dbt config (dataset-prefixed model names + alias)
  profiles.yml          DuckDB connection

dashboard/app.py        Streamlit BI—Report / Ask (chat) / Build tabs
run.py                  cross-platform wrapper (the `sediment` command)
Makefile                thin delegator to run.py (Linux/macOS convenience)

tests/                  pytest suite—incl. adversarial SQL-guard tests
evals/                  NL→SQL golden-question harness + investigation evals
  golden_questions.yaml curated questions with result invariants
  investigations/       planted-truth investigation scenarios

warehouse.duckdb        entire warehouse—one file (git-ignored)
pyproject.toml          packaging (pip install → sediment CLI) + ruff/pytest config
```

## Key Concepts & Conventions

### The One Design Decision That Matters

**Deterministic core, AI at the edges.** The transform path is tested dbt SQL. AI proposes models, explains failures, and answers questions—it never regenerates SQL at runtime. Reproducibility is the product. This boundary is a deliberate engineering stance. Contributions that put an LLM inside the deterministic transform path will be declined.

### Dataset-Agnostic Contract

- **Fully generic & automatic:** ingestion, profiling, staging generation, test inference, orchestration, NL querying.
- **Human-curated per dataset:** mart semantics—*what business questions the marts answer*. No tool can know what matters in arbitrary data; pretending otherwise breaks the trust model.

### Per-Dataset Schema Namespacing

Each dataset lives in `<dataset>_raw` / `<dataset>_staging` / `<dataset>_marts`. Model node names are `<dataset>__<name>` (globally unique) with a dbt `alias` back to the clean relation. Multiple datasets coexist in one warehouse; identical table names across datasets are fully supported.

### Bring Your Own Data (Single-Table)

```bash
mkdir -p datasets/sales/data && cp ~/orders.csv datasets/sales/data/
cat > datasets/sales/config.yml <<YAML
name: sales
source: data/orders.csv
table: orders
YAML

python run.py load sales
python run.py profile sales
python run.py scaffold sales --write  # installs stg_orders + tests, merges _sources.yml
# Review proposed marts in datasets/sales/scaffold/, move keepers to dbt_project/models/marts/
python run.py up sales
python run.py dashboard
```

**Multi-table datasets:** Declare `tables:` list instead of single `source`/`table`. The engine infers relationships and proposes join marts.

### View vs. Build Mode

**Build** (local authoring: `python run.py dashboard` or `SEDIMENT_MODE=build`) enables model creation and chart editing. **View** (default/deployed) gives read-only chat and charts, refuses model creation server-side. Share reports in view mode; author locally in build mode.

## Testing & Trust Boundary

Everything in `tests/` runs offline, no API key:

- **Deterministic engine:** ingest auto-typing, profiling, scaffold baseline, config.
- **SQL trust-boundary guards:** `test_read_only_guard.py` is adversarial—every known way to turn "a SELECT" into a write, chained statement, file read, or extension load must be refused. Ordinary analytical SQL must pass. **If you extend the guard, add the attack it defends against here first.**
- **Modeling guard:** chat → dbt model promotion is similarly hardened (`test_modeling_guard.py`).

**CI** (`.github/workflows/ci.yml`) runs lint + test suite on Linux/Windows × Python 3.10/3.13, then proves a fresh clone builds a green pipeline with one command—fully offline.

**Evals:** `evals/harness.py` runs golden NL→SQL questions through L3–L6 (replay mode, no key, CI-safe). `--live` runs full L1→L7 (needs key). Investigation evals (`evals/investigations/`) validate the planted-truth datasets.

## Current Active Work (as of 2026-10-30)

**Branch `claude/dreamy-edison-ojd2q3`:** Investigation framework development—day-3 rating matrix, blinded reports, tolerant arm labels, state labels to L2, tracer ignores quoted labels, per-dataset dbt scope, saasco planted-truth dataset with full-fqn glob, usage metering, planner/runner/judge/skeptic/memory/report/CLI/baselines/scorer. CI truth replay and offline demo. Commits span Oct 30, 2026.

**Merged recently:** Per-dataset schema namespacing, dataset-prefixed model names, multi-table dataset onboarding, packaging as `sediment` CLI, adversarial SQL-guard hardening.

**Status:** Core phases (PRD §9) are complete—v0.2.0 shipped. See ROADMAP.md for next steps.

## Things NOT to Do

1. **Do NOT put AI in the deterministic transform path.** No LLM regenerates dbt SQL at runtime. This is the product's core constraint.
2. **Do NOT auto-install marts without human review.** `scaffold --write` installs staging models and tests idempotently; mart proposals stay in `datasets/<name>/scaffold/` until a human moves them to `models/marts/`.
3. **Do NOT propose tests the profile doesn't justify.** Conservative inference only—a wrong test is worse than no test.
4. **Do NOT bypass the read-only execution guard.** The query agent's security boundary is `engine/query/execution.py`. Add attacks to the test suite first.
5. **Do NOT merge without green CI.** Lint (`ruff check .`) and `pytest -m "not live"` must pass on both platforms.
6. **Do NOT hardcode `/` in paths or assume Unix.** This project develops and CIs on Windows and Linux. Use `pathlib` or `os.path`.
7. **Do NOT skip the reproducibility proof.** If `python run.py up` doesn't build a green pipeline on a fresh clone, it's a blocker.

## Pull Requests & Contributions

See CONTRIBUTING.md. Key points:

- Bug fixes need a failing test. Security-sensitive areas (SQL guards, modeling promotion, path handling) require the attack case first.
- Eval questions are valuable contributions. New golden questions in `evals/golden_questions.yaml` make the NL→SQL agent measurably better.
- PRs must be green: `ruff check .` + `pytest -m "not live"` on Linux/Windows.
- Windows matters: avoid Unicode in console output, use `pathlib` for paths.

## Open Questions

- Optionally ship a small permanent second worked-example dataset (relational) so the dataset selector + multi-table story is demoable out of the box.
