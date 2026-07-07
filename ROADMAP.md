# sediment — Roadmap

sediment today (v0.2) is a dataset-agnostic analytics stack in a box: point it at tabular data and one command builds warehouse → tested dbt models → dashboard, offline, with AI at exactly three seams — proposing models (`scaffold`), explaining runs (`orchestrate`), and answering questions (`ask`) — and never inside the deterministic transform path.

This document is deliberately **aspirational**. It describes where the project wants to go, in order of intent, not a schedule. Versions past v0.3 are directions, and real usage gets to reorder them — a feature nobody pulls for is a feature this roadmap will happily drop.

---

## v0.3 — The accumulation layer

*Theme: answers should leave something behind.*

Most analytics work evaporates: the question gets answered, the context gets lost, and six months later nobody can say what the number meant or why. v0.3 adds the small, boring artifacts that make work accumulate instead.

- **Grain tests (`assert_grain`).** Every table declares, in one sentence, what one row means — "one row per customer per month" — and a ~15-line test fails the build the moment that stops being true. This sounds trivial; it prevents the single most common analytics disaster, the silently duplicated join that double-counts everything downstream. You find out at build time, not in the meeting.
- **Metric registry.** One small YAML file per metric, in git: the definition in plain English, who owns it, since when, what changed and why — and a mandatory *honest caveats* section (what the metric can't see, where it misleads). A `sediment validate` command checks the registry in CI. The point: definitions live in version control, not in someone's head, and a metric that claims no weaknesses is refused on principle.
- **Spec and decision-log templates.** Markdown you can steal one file at a time: a scoring-system spec with a structural caveats section, a metric-change RFC, an append-only "why is it like this" log — so "why does churn exclude trials?" is answered by a dated entry, not an annual meeting.

## v0.4 — Point it at anything

*Theme: useful against the stack you already have, not just the one sediment builds.*

- **`sediment doctor`.** Run it against an *existing* warehouse and dbt project and get a health report: tables with no declared grain, joins at risk of fan-out, untested models, metrics computed in two places with two answers, row counts drifting week over week. Today sediment shines on greenfield builds; `doctor` makes it valuable in the first hour against a stack you inherited.
- **More sources.** Databases and APIs, not just files — with scheduled refresh, so a sediment workspace can stay current without a human re-running ingest.
- **Installable patterns.** `sediment add pattern growth-accounting` drops a complete, runnable reference implementation — growth accounting (new/retained/resurrected/churned), funnel & retention, composite health scoring — each on synthetic data, each a working example of the conventions rather than a blog post about them.

## v0.5 — Sentinel

*Theme: the pipeline watches itself.*

- **`orchestrate` becomes a standing watch.** Scheduled runs across one or many workspaces: it builds, tests, explains any failure in plain English with the offending rows, flags drift and anomalies, and notifies you (or opens an issue) instead of waiting to be run. The difference between a pipeline you check and a pipeline that tells you.
- **A published trust ledger.** The golden-question eval suite runs on every release and the accuracy numbers get published with it. If an AI is going to answer questions about your data, its error rate should be a number you can look up, not a vibe.

## v0.6 — Ask, everywhere

*Theme: the trust architecture becomes the interface.*

- **Shareable Ask.** A governed, read-only link where a stakeholder asks questions in English and every answer arrives with its SQL, its validation trace, and its trust badge. The server-side read-only guarantee already exists; this makes it a surface you'd hand to a non-technical colleague without flinching.
- **MCP server.** Expose the warehouse to any AI agent *through* the L1–L7 validation ladder and the read-only guard, instead of handing it a raw connection. "Let the agent query the database" is the obvious future and the obvious nightmare; sediment's whole trust stack is the difference between the two.
- **Registry-aware answers.** The agent cites canonical definitions from the metric registry and declines to invent new ones on the fly. When it's asked about "active users," it answers with *your* definition — caveats included.

## v1.0 — The layered stack

*Theme: the thesis, realized in software.*

- **Conversation becomes convention.** Every promoted model, accepted definition, and logged decision accumulates into the workspace's memory. The agent builds on last month's answers instead of rediscovering them; ask a question twice a quarter apart and the second answer knows about the first. Analytics work that leaves layers — not as a document about the idea, but as default behavior.
- **Portability where pulled.** DuckDB-native today, and DuckDB goes further than most people think. Adapters for other warehouses arrive when real usage demands them, not before.

## Beyond — the north star

**The one-person data platform.** A single practitioner credibly runs many data stacks at once: `scaffold` does the first draft of the building, sentinel does the watching, Ask does the answering, and the accumulated layers do the remembering — while the human does the part that's actually human: judgment. The stack a small team deserves, without the platform team.

## What this roadmap will not do

- Put AI in the deterministic transform path. Ever. That boundary is the product.
- Grow enterprise governance features — committees, stewardship workflows, forty-page policies.
- Chase features that only make sense for large platform teams. The audience is the practitioner carrying the whole stack alone.
- Treat this document as a commitment device. It's a compass; pull requests and real usage are the map.
