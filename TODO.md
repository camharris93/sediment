# sediment — backlog

## ✅ Multi-table dataset onboarding + dataset selector — DONE (2026-06-02)

Shipped: `config.yml` `tables:` list (single-table form still works); `ingest`/`profile`
loop over tables; `profile` derives cross-table relationships (`infer_relationships`);
`scaffold --write` installs a staging model per table + merges all sources entries and
proposes join marts (`llm_propose_join_marts`); dashboard has a **dataset selector**
(`engine/registry.py` maps dataset→marts via the dbt manifest) with per-dataset scoped
grounding and a generic auto-report for non-AnAge datasets. Verified end-to-end with a
2-table customers/orders dataset (derived `customers→orders [1:many]`, built a join mart
with no fan-out), then removed the fixture.

## ✅ Per-dataset schema namespacing — DONE (2026-06-02)

Each dataset now lives in `<dataset>_raw` / `<dataset>_staging` / `<dataset>_marts`.
ingest lands `<dataset>_raw`; staging/marts models carry `{{ config(schema='<dataset>_…') }}`
(scaffold emits it; AnAge migrated); per-dataset source blocks in `_sources.yml`;
`engine/registry.py` derives a dataset's models exactly from its schemas; grounding,
the dashboard selector, `engine/modeling.py`, and `orchestrate` are all schema-aware.
Verified: AnAge + a 2-table `shop` dataset coexisted in fully isolated schemas, scoped
ask worked for each, 23 tests green; then removed the fixture.

## ✅ Dataset-prefixed model names — DONE (2026-06-02)

Model node names are now `<dataset>__<name>` (globally unique) with a dbt `alias` back to the
clean relation (`<dataset>_staging.stg_<table>` / `<dataset>_marts.<mart>`). scaffold,
modeling (promote/materialize), registry, and orchestrate's `--break` all use the prefixed
names; refs are `{{ ref('<dataset>__<table>') }}`. Verified: two datasets both with an `orders`
table built into `north_staging.stg_orders` / `south_staging.stg_orders` with no collision.
**Identical table names across datasets are now fully supported.**

## Other

- [ ] Optionally ship a small permanent second worked-example dataset (relational) so the
  dataset selector + multi-table story is demoable out of the box.
