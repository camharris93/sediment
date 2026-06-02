# PRD: Stack-in-a-Box — A Dataset-Agnostic Analytics Engineering Framework

**Status:** Draft v1
**Author:** Cam Harris
**Last updated:** June 2, 2026
**Working name:** `sediment` (was: `coldstack`)

---

## 1. Summary

A reproducible, local-first analytics platform that takes *any* tabular dataset and runs it through a complete modern data stack — ingestion, warehousing, transformation, testing, documentation, and visualization — with AI layered at the build, orchestration, and consumption seams (and deliberately **not** in the deterministic transform path).

The framework is dataset-agnostic: a generic engine handles everything that can be safely automated (ingest → profile → stage → test → orchestrate → query), while dataset-specific business logic (mart semantics) stays human-curated. A worked example using a longevity-science dataset (AnAge) ships in the repo as the reference implementation.

One command (`make up`) clones-and-runs the entire pipeline with no cloud accounts, no API keys for the core data flow, and no SaaS dependencies.

---

## 2. Goals & Non-Goals

### Goals
1. Demonstrate end-to-end analytics-engineering ownership: ingestion through dashboard, tested and documented.
2. Be genuinely dataset-agnostic for the automatable layers — drop in any CSV/Parquet/JSON and get a clean, typed, tested staging layer.
3. Use AI where it adds value (build-time scaffolding, run-time orchestration/monitoring, natural-language consumption) and demonstrate the judgment to keep it *out* of the deterministic core.
4. Be fully reproducible: one command, runs offline, identical result on any clone.
5. Serve as a portfolio artifact that reads as a *framework/product*, not a one-off analysis.

### Non-Goals
- Not a hosted SaaS product (no multi-tenant infra, no auth, no billing).
- Not a replacement for dbt Cloud / Fivetran / a real orchestrator at scale.
- No streaming, no multi-GPU, no distributed compute.
- The framework will **not** auto-generate mart semantics without human curation — this is a deliberate design stance, not a limitation to fix.
- Not attempting Option C (full config-driven semantic-layer codegen) in v1; architected to grow toward it later.

---

## 3. Design Principles

1. **Deterministic core, AI at the edges.** The raw → staging → marts flow is hand-written, version-controlled, tested dbt SQL. It produces the same result every run. AI assists in *building* it and *operating* it, never in *executing* the transforms.
2. **Automate what's safe, curate what matters.** Ingestion, profiling, staging scaffolds, and tests are generic and automatable. What the marts *mean* is a human judgment call and stays that way.
3. **Local-first and reproducible.** No cloud accounts required for the core. One portable warehouse file. One command to run.
4. **Stageable.** Every layer works standalone; the build proceeds in independently-shippable phases.

---

## 4. Architecture

### 4.1 The generic engine (dataset-independent, written once)

| Component | Role | Tech |
|---|---|---|
| **Ingestion layer** | Takes any `path/to/file.{csv,parquet,json}`, infers types, lands it untouched in `raw.<tablename>` | Python + DuckDB auto-typing |
| **Profiler** | Profiles whatever landed: column names, types, null rates, cardinality, min/max, sample values, candidate keys. Emits `profile.json` | Python + DuckDB |
| **AI scaffolding step** *(build-time only)* | Feeds `profile.json` to an agent → generates `stg_<table>` model, `schema.yml` with inferred tests, and *proposed* mart stubs with explanatory comments. Output is deterministic dbt code for human review | LLM (Claude API) |
| **Orchestration agent** | Drives `load → dbt run → dbt test → build`; reads results; explains test failures in plain English; flags anomalies; proposes fixes for approval | Python + LLM at decision points only |
| **Query agent** | Natural-language → SQL over whatever marts exist; schema pulled dynamically from DuckDB so it adapts to any dataset | Reuse existing NL→SQL agent |

### 4.2 The warehouse
A single DuckDB file (e.g. `warehouse.duckdb`) holding `raw`, `staging`, and `marts` schemas. This file *is* the warehouse — no server, no cloud. This is the key enabler of local-first reproducibility.

### 4.3 The transform layer (dbt Core)
- **Staging** (`stg_<table>`): 1:1 with source — snake_case rename, type casts, unit standardization, junk-row filtering. No business logic.
- **Intermediate** (optional): joins, enrichment, derived fields.
- **Marts** (`mart_*`): business-ready tables the dashboard reads. **Human-curated per dataset.**
- **Tests**: `not_null`, `unique`, `accepted_values`, relationships. Run with one command; prove data soundness.
- **Docs**: `dbt docs generate` builds the raw→staging→marts lineage graph.

### 4.4 The visualization layer
Evidence.dev (BI-as-code, Markdown + SQL) reading marts directly from DuckDB, building to a static site. Streamlit is the pure-Python fallback if avoiding Node.js.

### 4.5 The wrapper
A `Makefile` (or `make up`) running: `download → load → profile → dbt run → dbt test → build dashboard`. Plus a README with architecture diagram and the one-command bootstrap.

### 4.6 Repo shape
```
/engine            # the reusable, dataset-agnostic framework
  ingest.py
  profile.py
  scaffold.py      # AI build-time scaffolding
  orchestrate.py   # AI run-time orchestration agent
  query/           # NL->SQL consumption agent
/datasets
  /anage           # worked example (longevity science)
    config.yml
    /models        # curated dbt marts for this dataset
    /data
/dbt_project        # dbt Core project
Makefile
README.md
```
A new user drops a file into a new `datasets/<name>/` folder, runs the scaffold, curates the proposed marts, and runs `make up`.

---

## 5. The AI Layers (explicit scope)

**Where AI lives:**
1. **Build-time copilot** — profiles the source, scaffolds staging + tests + proposed marts. Human reviews and commits. Portfolio framing: *"AI-assisted modeling — agent scaffolded the dbt layer; I reviewed, corrected, and committed."*
2. **Orchestration brain** — runs/monitors/explains the pipeline. On `dbt test` failure: reads the error, explains which test failed and why, proposes a fix. On row-count swings: flags. An agentic wrapper around a deterministic core.
3. **Consumption layer** — NL→SQL agent over the marts; schema pulled dynamically so it adapts to any dataset.

**Where AI explicitly does NOT live:**
- The steady-state transform path. No LLM regenerates dbt SQL at runtime. Determinism and reproducibility are the product's core value and must not be compromised. *(This boundary is itself a senior-engineer signal and should be stated in the README.)*

---

## 6. The Dataset-Agnostic Contract

"Works with any dataset" is scoped precisely (this is **Option A** of three considered):

- **Fully generic & automatic:** ingestion, profiling, staging-layer generation, basic test inference, orchestration, NL querying.
- **Human-curated per dataset:** mart semantics — *what business questions the marts answer*. No tool can know what *matters* in arbitrary data, and pretending otherwise breaks the trust model.

Rejected alternatives:
- **Option B (fully generic, no marts):** maximally agnostic but discards the modeling layer that makes this an analytics-engineering piece rather than a "chat with your CSV" toy.
- **Option C (config-driven semantic-layer codegen):** most powerful, but a multi-week build (effectively a mini-dbt). Deferred; v1 is architected to grow toward it.

---

## 7. Reference Implementation: AnAge (Longevity Science)

**Dataset:** AnAge — The Animal Ageing & Longevity Database (downloadable tab-delimited; quantitative ageing data across species: maximum lifespan, body mass, metabolic rate, etc., including species that appear not to age).

**Why it's a good worked example:** small enough to be tractable, messy enough to need real staging work, and it supports genuinely interesting marts (e.g. body-size→lifespan trend and the species that defy it). Memorable portfolio narrative.

**Example marts:**
- `mart_longevity_by_class` — lifespan stats rolled up by taxonomic class.
- `mart_aging_outliers` — species ranked by how far they over/under-perform the body-mass→lifespan trend (the "non-ageing outliers" view).

---

## 8. Tech Stack & Prerequisites

| Layer | Choice | Account needed? |
|---|---|---|
| Warehouse | DuckDB | No (it's a file) |
| Transform | dbt Core + `dbt-duckdb` | No (open-source CLI) |
| Visualization | Evidence.dev (or Streamlit) | No to build; generic static host only if publishing |
| Ingestion | Python (optionally `dlt`) | No |
| AI layers | Claude API | API key (AI layers only; core data flow needs none) |
| Orchestration | Makefile (or Dagster/Prefect if showing a DAG) | No |

**Local prerequisites:** Python (have it), Node.js (for Evidence — the one likely install; skippable if using Streamlit).

---

## 9. Build Phases (stageable)

1. **Generic ingest + profiler → DuckDB.** Any file lands in `raw`, `profile.json` emitted. *(Foundation — everything hangs on this.)*
2. **AnAge worked example through dbt** with staging, curated marts, tests, and Evidence dashboard. `make up` runs green.
3. **AI build-time scaffolding step** — profile → generated staging + tests + proposed marts.
4. **Orchestration agent** — run/monitor/explain layer over the deterministic core.
5. **Wire in the query agent** — NL→SQL over the marts, dynamic schema.

---

## 10. Success Criteria

- `git clone` + `make up` produces a green, tested pipeline and a viewable dashboard on a fresh machine with only documented prerequisites.
- Dropping a new arbitrary tabular file into a new `datasets/` folder and running the scaffold produces a clean, typed, tested staging layer without hand-editing the engine.
- `dbt test` passes; `dbt docs` renders the lineage graph.
- The orchestration agent correctly explains an intentionally-introduced test failure in plain English.
- The query agent answers a natural-language question against the marts with correct SQL shown.
- README clearly articulates the deterministic-core / AI-at-edges boundary as a deliberate design decision.

---

## 11. Open Questions

- Final name.
- Orchestration: keep Makefile simple, or invest in Dagster/Prefect for the DAG + UI signal?
- Visualization: Evidence (BI-as-code, slicker, needs Node) vs. Streamlit (pure Python, lower ceiling)?
- How far to push test inference in the scaffolding step before it risks proposing wrong tests?
- Publish a live hosted dashboard, or keep it clone-and-run only?
