# sediment — backlog

## Multi-table dataset onboarding

**Status:** not started · **Logged:** 2026-06-02

The analytical core is already multi-table (the NL→SQL agent derives cross-table
cardinality and warns on join fan-out; dbt, orchestrate, dashboard, and the
model-builder all enumerate/handle many tables). The single-table assumption lives
only in dataset *onboarding*. To support relational datasets:

- [ ] **Config** — let `datasets/<name>/config.yml` declare a `tables:` list
  (each with `source`/`table`/`delimiter`), plus an optional `relationships:` list
  of known FKs. Keep single-table config working (treat as a 1-item list).
- [ ] **Ingest** (`engine/ingest.py`) — loop over the table list; each file → `raw.<table>`.
- [ ] **Profile** (`engine/profile.py`) — loop over tables; emit a multi-table
  `profile.json` and reuse the grounding relationship-inference to surface DERIVED
  FK/cardinality (don't just trust declared ones).
- [ ] **Scaffold** (`engine/scaffold.py`) — generate one staging model per raw table,
  and propose join-based `int_`/`mart_` stubs from the inferred relationships (this is
  where multi-table gets valuable).
  - [x] Single-table: `scaffold --write` installs the staging model + tests and merges
    the `raw.<table>` sources entry (`ensure_source_entry` / `install_staging`).
    Multi-table just needs to loop this over the table list.
- [ ] **Runner** (`run.py`) — `up` loops ingest+profile across all tables.
- [ ] **Dashboard** — make the built-in Report charts dataset-agnostic (currently the
  three AnAge longevity sections are hardcoded). Either drive the whole Report tab from
  `report_blocks.json`, or detect AnAge vs. generic and fall back to AI-built charts.
- [ ] **Prove it** — load a small real relational dataset (Chinook/Northwind subset)
  end-to-end: `up` → chat with a join → confirm the L6 fan-out warning fires.

See README §"Sharing & governance" and the query agent's `grounding._infer_relationships`.
