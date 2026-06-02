# sediment — a dataset-agnostic analytics-engineering framework

Take *any* tabular dataset and run it through a complete modern data stack —
ingestion → warehouse → transformation → testing → docs → visualization — with AI
layered at the **build, orchestration, and consumption** seams, and deliberately
**kept out of the deterministic transform path**.

One command, no cloud accounts, no API keys for the core data flow, runs offline:

```bash
python run.py up          # download → load → profile → dbt run → dbt test
python run.py dashboard   # open the BI dashboard
# (with make installed: `make up`, `make dashboard`)
```

The reference dataset is **AnAge** (the Animal Ageing & Longevity Database) — ~4,600
species. The marts answer questions like *"which animals live far longer than their
body size predicts?"* (answer: deep-sea rockfish, the olm, tortoises, naked
mole-rats — and yes, the data backs it up).

---

## The one design decision that matters

> **Deterministic core, AI at the edges.**
> The `raw → staging → marts` flow is hand-written, version-controlled, **tested dbt
> SQL**. It produces the same result on every run, on every clone. AI assists in
> *building* it, *operating* it, and *querying* it — but **never executes the
> transforms**. No LLM regenerates SQL at runtime. Reproducibility is the product.

This boundary is a deliberate engineering stance, not a limitation. "Automate what's
safe; curate what matters."

```
                         ┌─────────────────────────────────────────────┐
   AI (build-time)  ───▶ │  scaffold:  profile.json → proposed dbt code │
                         └─────────────────────────────────────────────┘
                                          │  (human reviews & commits)
                                          ▼
 ┌──────────┐   ┌──────────┐   ┌───────────────── DETERMINISTIC CORE ─────────────────┐
 │  any     │   │ ingest   │   │   raw.*  ──▶  staging.stg_*  ──▶  marts.mart_*         │
 │ csv/tsv/ │──▶│ (DuckDB  │──▶│   (1:1, typed)   (curated semantics)  + dbt tests      │
 │ parquet/ │   │ autotype)│   │            one local warehouse.duckdb file             │
 │  json    │   └──────────┘   └───────────────────────────────────────────────────────┘
 └──────────┘         │                         │                         │
                      ▼                         ▼                         ▼
                 profile.json            Streamlit dashboard      NL→SQL query agent
                                                              ┌─────────────────────────┐
                  AI (run-time)  ───▶  orchestrate:           │ AI (consumption)        │
                  run / monitor / explain test failures /     │ L1 intent → L2 gen →    │
                  flag row-count drift                        │ L3 static → L4 dry-run →│
                                                              │ L5 exec → L6 plausible →│
                                                              │ L7 explain + trust badge│
                                                              └─────────────────────────┘
```

---

## Quickstart

Prereqs: **Python 3.10+** (only hard requirement). Optional: an Anthropic API key
for the three AI layers; `make` if you want the `make` aliases.

```bash
pip install -r requirements.txt

python run.py up                      # build the whole pipeline, green
python run.py dashboard               # http://localhost:8501
python run.py docs                    # dbt lineage graph (raw → staging → marts)

# AI layers (need a key: ANTHROPIC_API_KEY env, or a one-line anthropic.txt):
python run.py scaffold <name> --write # generate staging+tests, wire them into dbt
python run.py orchestrate anage --break  # run + monitor + explain an injected failure
python run.py ask "which animals live longest for their size?"
```

Every target takes an optional dataset name (default `anage`); `make` users pass
`DATASET=…`. The **core** (`up`, `dashboard`, `docs`) needs no key; the **AI layers**
(`scaffold`'s LLM tier, `orchestrate`'s explanations, `ask`, and the dashboard's Ask /
Build / chart features) each need an Anthropic key — set once, used by all.

---

## Repo shape

```
engine/                 the reusable, dataset-AGNOSTIC framework (written once)
  config.py             paths, dataset config, key resolution
  ingest.py             any file  → raw.<table>            (generic)
  profile.py            raw table → profile.json           (generic)
  scaffold.py           profile  → proposed dbt code       (AI, build-time)
  orchestrate.py        run / monitor / explain the core   (AI, run-time)
  modeling.py           promote a chat answer -> dbt model  (AI, build, gated)
  charting.py           suggest a chart spec for the report (AI, build, gated)
  report_config.py      persist custom report blocks        (build, gated)
  query/                NL→SQL agent over the marts         (AI, consumption)
    grounding.py  intent.py  generation.py  static_validation.py
    dry_run.py    execution.py  plausibility.py  translation.py  orchestrator.py  cli.py
datasets/
  anage/
    config.yml          the entire human-authored contract for the dataset
    data/               the source file
    scaffold/           generated proposals for review
    report_blocks.json  AI-built charts pinned to the report (committable)
dbt_project/            dbt Core project (DuckDB)
  models/staging/       stg_anage + _sources.yml + tests
  models/marts/         mart_longevity_by_class, mart_aging_outliers (curated)
dashboard/app.py        Streamlit BI — Report / Ask (chat) / Build tabs
run.py / Makefile       the one-command wrapper
warehouse.duckdb        the entire warehouse — one file (git-ignored)
```

---

## Bring your own data

The core is **dataset-agnostic for everything that's safely automatable**; mart
*semantics* stay human-curated (that's the whole design — see §"The dataset-agnostic
contract"). Onboarding a new **single-table** dataset:

```bash
# 1. Drop your file in and declare the contract.
mkdir -p datasets/sales/data && cp ~/orders.csv datasets/sales/data/
cat > datasets/sales/config.yml <<'YAML'
name: sales
source: data/orders.csv      # path (relative to the dataset dir) or a URL
table: orders                # lands as raw.orders
# delimiter: "\t"            # optional; auto-sniffed for csv/tsv
YAML

# 2. Land it, profile it, and WIRE a typed/tested staging layer into dbt.
python run.py load sales
python run.py profile sales                 # -> datasets/sales/profile.json
python run.py scaffold sales --write        # installs stg_orders + tests, adds the
                                            # raw.orders entry to _sources.yml

# 3. Curate the marts (the human-judgment step). With a key, the scaffolder also
#    drops proposed mart stubs in datasets/sales/scaffold/*.proposed.sql — review,
#    move keepers into dbt_project/models/marts/, and edit to taste. (Without a key,
#    you write the marts yourself against the freshly-built staging layer.)

# 4. Build + test, then consume.
python run.py up sales
python run.py dashboard                      # Ask tab + AI chart builder work on any data
python run.py ask "top 10 orders by value"
```

`scaffold --write` collapses the old manual wiring (copy the staging model in, hand-add
a `sources.yml` entry) into one idempotent command. It **never** auto-installs marts —
their semantics are yours to decide. Re-running is safe; it won't overwrite a curated
staging model unless you pass `--force`.

**Multi-table datasets** are supported — declare a `tables:` list instead of a single
`source`/`table`, and the engine loops ingest/profile over them, **derives the
relationships** between them, installs a staging model per table, and (with a key)
proposes **join marts** across the inferred relationships:

```yaml
name: shop
tables:
  - {source: data/customers.csv, table: customers}
  - {source: data/orders.csv,    table: orders}
```

```bash
python run.py load shop && python run.py profile shop   # derives customers→orders [1:many]
python run.py scaffold shop --write                     # installs both staging models + join-mart proposals
```

**Multiple datasets coexist** in the one warehouse file, each in its **own schemas** —
`<dataset>_raw` / `<dataset>_staging` / `<dataset>_marts`. So AnAge lives in `anage_marts`
etc. and a second dataset never touches it. The dashboard's **dataset selector** scopes
the Report, the chat's grounding, and model-building to one dataset at a time (AnAge gets
its curated charts; every other dataset gets an **auto-report** + the AI chart builder).

**Even identical table names across datasets are fine.** dbt requires globally-unique
*model names*, so each model's node name is dataset-prefixed (`anage__stg_anage`) while a
dbt `alias` keeps the warehouse relation clean (`anage_staging.stg_anage`). Two datasets can
both have an `orders` table — they build as `north__stg_orders` / `south__stg_orders` into
`north_staging.stg_orders` / `south_staging.stg_orders` with no collision. Refs use the
prefixed node names; everything querying the warehouse sees the clean relation.

---

## The dataset-agnostic contract (scoped precisely)

| Layer | Status |
|---|---|
| ingestion, profiling, staging generation, test inference, orchestration, NL querying | **fully generic & automatic** |
| **mart semantics** — *what business questions the marts answer* | **human-curated per dataset** |

No tool can know what *matters* in arbitrary data; pretending otherwise breaks the
trust model. So the engine automates everything up to the marts, and the marts stay
a human judgment call.

---

## The AI layers

1. **Build-time copilot** (`engine/scaffold.py`) — profiles the source and proposes a
   staging model, `schema.yml` with conservative inferred tests, and *proposed* mart
   stubs with explanatory comments. A **deterministic baseline** runs with no key
   (mechanical snake_case staging + key tests); the LLM *enhances* it. Output is
   written to `datasets/<name>/scaffold/` for human review — never auto-committed. Add
   `--write` to install the staging model + tests into the dbt project and merge the
   `raw.<table>` sources entry (idempotent; marts are never auto-installed).

2. **Orchestration brain** (`engine/orchestrate.py`) — drives `load → dbt run → dbt
   test`, reads dbt's structured artifacts, **explains test failures in plain English
   with the offending rows**, and **flags row-count drift** vs. the last run. The LLM
   only acts at the decision points; the pipeline runs (and fails loudly) without a
   key. Try `python run.py orchestrate anage --break` to watch it catch and explain a
   deliberately-injected failing test.

3. **Consumption layer** (`engine/query/`) — a **trust-first, layered NL→SQL agent**
   (ported from the sibling `sql-engine` project and adapted to DuckDB). Schema is
   pulled dynamically from the warehouse, so it adapts to any dataset. The layers:

   | Layer | Role |
   |---|---|
   | L1 intent | restate the question, **surface assumptions** (auditable) |
   | L2 generation | constrained DuckDB SQL over the grounded schema only |
   | L3 static check | sqlglot validates columns/tables **without touching the db** |
   | L4 dry-run | DuckDB `EXPLAIN` — authoritative bind/semantic check, no execution |
   | L5 execution | read-only guard + row cap, full provenance |
   | L6 plausibility | join fan-out + result-value sanity checks |
   | L7 translation | plain-English answer + **trust badge**; every number traces to the SQL |

   L3/L4 failures feed structured violations back to L2 in a bounded **self-correction
   loop**. A query that can't be validated is a *transparent failure*, not a confident
   guess. Available as a CLI (`python run.py ask "…"`) **and built into the dashboard's
   💬 Ask tab, which renders a live L1→L7 trace** (each layer lights up pass/retry/fail
   with its intent, SQL, validation detail, and trust badge) as the answer is computed.

4. **Build a model from chat** (`engine/modeling.py` + dashboard 🛠 Build tab) — promote
   a validated chat answer into a dbt model. The chat SQL is rewritten from
   `marts.x`/`staging.x` into `{{ ref('x') }}`, then you can **preview it in an isolated
   `_sandbox` schema**, **save it as a reviewable `models/marts/<name>.sql`**, or
   **`dbt run` it straight into `marts`**. Even here the unit of work is reviewable dbt
   SQL — the chat never silently mutates the curated marts.

5. **AI chart builder + editable report** (`engine/charting.py`, `engine/report_config.py`)
   — the dashboard's 📊 Report tab is editable in build mode: point it at a mart (or a
   chat answer's SQL), and the agent proposes a chart spec (revived from sql-engine's L7
   `VizHint`), which you preview and **pin to the report**. Build a model from chat, then
   chart it in two clicks — the Build tab seeds the Report customizer with the new mart.
   Saved charts persist to `datasets/<name>/report_blocks.json` and render for *everyone*
   (a heuristic fallback picks a sensible chart when no key is set); only *editing* the
   report is gated to build mode.

### Sharing & governance (view vs. build)

The capability tension — *"we can't open the ability to build models to everyone we
share a report with"* — is resolved by making **build a property of the deployment, not
a per-user toggle**:

| | **view** (default) | **build** |
|---|---|---|
| NL→SQL **Ask** | ✅ (read-only at the engine: L5 guard + read-only DB) | ✅ |
| **Build a model** | ⛔ refused **server-side**, not just hidden | ✅ |
| Set by | a bare/deployed `streamlit run` (or `SEDIMENT_MODE=view`) | the local authoring app `python run.py dashboard` |

So you **share a report in view mode**: viewers get the charts and the chat, never model
creation. You author in build mode locally (or on a private instance), review the
generated dbt, and commit — the published report only ever ships the *built, tested*
marts. One shared app with per-user build rights is a deliberate non-goal (PRD §2); the
upgrade path is a password (`st.secrets`) / SSO in front of the Build tab.

---

## Reference implementation: AnAge

Two curated marts (`dbt_project/models/marts/`):

- **`mart_longevity_by_class`** — lifespan stats rolled up by taxonomic class.
- **`mart_aging_outliers`** — every species ranked by how far its actual longevity
  beats the body-size→lifespan power law (fit in log-log space with DuckDB's
  `regr_slope`/`regr_intercept` — deterministic SQL, no library, no AI). The top
  over-performers:

  | rank | species | class | weight | longevity | size-predicted | ratio |
  |---|---|---|---|---|---|---|
  | 1 | Rougheye rockfish | Teleostei | 495 g | 205 yr | 15.6 yr | 13.1× |
  | 2 | Olm | Amphibia | 17 g | 102 yr | 9.8 yr | 10.4× |
  | 3 | Eastern box turtle | Reptilia | 372 g | 138 yr | 15.0 yr | 9.2× |

---

## Tech stack

| Layer | Choice | Account needed? |
|---|---|---|
| Warehouse | **DuckDB** (one file) | No |
| Transform | **dbt Core** + `dbt-duckdb` | No |
| Visualization | **Streamlit** (pure-Python) | No |
| Ingestion | Python + DuckDB auto-typing | No |
| AI layers | **Claude API** (Anthropic SDK) | Key — *AI layers only* |
| Orchestration | Python runner / Makefile | No |

---

## Build status (PRD phases)

- ✅ **1.** Generic ingest + profiler → DuckDB
- ✅ **2.** AnAge worked example through dbt (staging, curated marts, tests) + dashboard; `up` runs green
- ✅ **3.** AI build-time scaffolding (deterministic baseline + LLM enhancement)
- ✅ **4.** Orchestration agent (run / monitor / explain, with `--break` demo)
- ✅ **5.** NL→SQL query agent over the marts (dynamic schema, self-correcting)
- ✅ **6.** In-app chat (dashboard Ask tab) + **build-a-model-from-chat** with view/build governance
- ✅ **7.** Multi-table datasets (looped ingest/profile, derived relationships, join-mart
  scaffolding) + a **dataset selector** that scopes the Report/Ask/Build to one dataset
- ✅ **8.** Per-dataset schema namespacing (`<dataset>_raw/_staging/_marts`) + dataset-prefixed
  model names with `alias` — multiple datasets coexist in one warehouse, even sharing table names

## Decisions taken (PRD §11 open questions)

- **Visualization → Streamlit** for v1: pure-Python, guaranteed-green offline, no
  Node install. Evidence.dev is the documented upgrade path when a slicker static
  site is wanted.
- **Orchestration → a Python runner** (`run.py`), not Dagster/Prefect. It keeps the
  one-command promise cross-platform (Windows has no `make`); a DAG engine is a clean
  later swap behind the same targets.
- **Test inference is deliberately conservative** — the scaffolder only proposes a
  test the profile *justifies* (a test that would fail on real data is worse than no
  test), and everything it proposes is for human review.
- **Live hosting**: clone-and-run only for now; the dashboard builds to a shareable
  static-ish Streamlit app if/when desired.
