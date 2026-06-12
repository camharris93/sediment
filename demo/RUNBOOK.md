# sediment — live demo runbook (macOS)

The terminal demo that follows the presentation site. Tuned for a ~20-minute "full
show" to data engineers, presented from a **fresh reclone** on your MacBook.

> Read once end-to-end, then **rehearse the whole thing the day before**. The only
> things that can really bite you are (1) the Anthropic key and (2) presenting from a
> repo that has leftover datasets. Both are handled below.

---

## The arc (what you're proving, and when)

| # | Act | Idea it lands | ~min |
|---|---|---|---|
| 1 | **Cold start** — `sediment up` | one command → a green, tested stack | 4 |
| 2 | **Ask in English** — terminal + dashboard trace | trust, by construction (L1→L7) | 5 |
| 3 | **Curate a mart from an answer** — Build tab | ad-hoc question → tested infrastructure | 4 |
| 4 | **Catch & explain a failure** — `orchestrate --break` | AI operates; the pipeline fails loudly on its own | 3 |
| 5 | **Bring your own data** — onboard a new file live | dataset-agnostic; minutes to a tested stack | 4 |

Total ~20 min + buffer. Acts 4 and 5 are the "full show" extras — cut either if short on time; Acts 1–3 are the spine.

---

## Part A — The day before (provision & rehearse — do NOT skip)

### A1. Fresh reclone

```zsh
# verify prereqs (you almost certainly have these on a dev MacBook)
python3 --version        # need 3.10+        ── if missing: brew install python@3.12
git --version

cd ~/Projects            # wherever you keep repos
rm -rf sediment          # delete the old clone
git clone https://github.com/camharris93/sediment.git
cd sediment

python3 -m venv .venv
source .venv/bin/activate
pip install -e .         # installs deps + the `sediment` command
```

> If `pip install -e .` is slow or fussy on the day, `pip install -r requirements.txt`
> also works — you'd just type `python run.py …` instead of `sediment …`. The commands
> are otherwise identical.

### A2. Anthropic key (the AI acts need it; the core doesn't)

Get a key at **console.anthropic.com → API keys**, then either:

```zsh
echo "sk-ant-xxxxxxxx" > anthropic.txt      # git-ignored; never committed
# …or, equivalently, for the current shell only:
export ANTHROPIC_API_KEY="sk-ant-xxxxxxxx"
```

`anthropic.txt` is the robust choice for a demo — it survives new terminal tabs and the
dashboard subprocess, so you won't get a surprise "needs a key" mid-demo.

### A3. Pre-flight checklist — every line must be green before you trust the demo

```zsh
sediment up                                                    # ~30–60s → "[ok] Pipeline is green."
sediment ask "which animals live far longer than their body size predicts?"
sediment dashboard                                             # opens http://localhost:8501 — click Report/Ask/Build, then Ctrl-C
sediment orchestrate anage --break                             # → exactly ONE failing test + a plain-English explanation
```

If `orchestrate --break` shows **more than one** failing test, you have a stray dataset
onboarded — reclone clean (a fresh clone is AnAge-only). On a clean clone it's exactly
one injected failure.

### A4. Rehearse once, end-to-end, with a timer. Then reset (Part B).

---

## Part B — Reset to a clean slate

Run this between rehearsals **and right before you go live**, so Act 1 builds from nothing on stage:

```zsh
./demo/reset.sh
```

It drops the warehouse (so `sediment up` rebuilds live) and removes anything the
bring-your-own-data act created. Safe to run anytime.

---

## Part C — The live demo, beat by beat

> Format per beat — **SAY** (your line) · **RUN** (exact command) · **SHOW** (what to point
> at) · **LAND** (the takeaway that ties back to the slides). Keep your terminal font large.

### Act 1 · Cold start

**SAY:** "There's nothing built here — no warehouse, no models. One command takes a raw file all the way to a tested warehouse."

**RUN:**
```zsh
sediment up
```

**SHOW:** the steps scroll by — download → load → profile → **dbt run** → **dbt test** — ending in `[ok] Pipeline is green.` Point out that the test step is real dbt tests passing. (Talk over it; it takes ~30–60s.)

**LAND:** "That's `raw → staging → marts` with dbt tests — reproducible, version-controlled, and it ran fully offline. No cloud account, no API key for any of that. The determinism is the point."

### Act 2 · Ask in English (the trust pipeline)

**SAY:** "Now the consumption edge. A plain-English question, answered over the curated marts — but only after it clears a validation gauntlet."

**RUN:**
```zsh
sediment ask "which animals live far longer than their body size predicts?"
```

**SHOW:** the answer (Rougheye rockfish 13.1×, the olm, the box turtle…), the **SQL** it ran, and the **Trust** badge at the bottom. "Every number you see traces to that SQL."

Then go visual:
```zsh
sediment dashboard          # http://localhost:8501  → the 💬 Ask tab
```
Ask the **same question** in the Ask tab and let the **live L1→L7 trace** light up — each layer passing in turn (intent → generation → static check → dry-run → execute → plausibility → answer). Then show a **follow-up** works conversationally:

> *"now just mammals"*

**LAND:** "Seven layers verify the query against the real schema before a single row is read. A query it can't validate is a **transparent failure** — it tells you it can't answer, instead of guessing. That's the difference between this and a chatbot bolted onto a database."

### Act 3 · Curate a mart from an answer

**SAY:** "A good answer shouldn't evaporate into a Slack thread. Watch it become tested infrastructure."

In the dashboard **💬 Ask** tab, ask something a little new:

> *"average longevity ratio by taxonomic class, for classes with at least 10 species"*

Then open the **🛠 Build a model** tab:
1. Name it `mart_longevity_ratio_by_class`.
2. Click **Preview in sandbox** — show the row count + sample, isolated from `marts`.
3. Click **🚀 Build into marts (dbt run)** — it writes a reviewable `models/marts/…sql` and builds it.
4. Pop to **📊 Report** and chart it (it's already seeded).

**LAND:** "Ad-hoc question → a reviewable, tested dbt model → a chart, in two minutes. And note the governance: this is **build mode**, which I got by launching locally. A shared, view-mode deployment refuses model-building **server-side** — viewers get the charts and the chat, never the ability to mutate the marts."

### Act 4 · Catch & explain a failure (the operate edge)

**SAY:** "AI also helps you *operate* the pipeline. Let me deliberately break a test."

**RUN:**
```zsh
sediment orchestrate anage --break
```

**SHOW:** it injects a deliberately-failing test, runs the suite, **catches the one failure**, and prints a **plain-English explanation with the offending rows** — then removes the injected test.

**LAND:** "The pipeline failed loudly on its own — no key required for that. The AI's job here is to *explain* the failure, not to paper over it. It operates at the decision point; it never silently rewrites the transform."

### Act 5 · Bring your own data

**SAY:** "None of this is about animals. It's dataset-agnostic — watch it cold-start a brand-new file."

**RUN** (paste-ready — a tiny sales file):
```zsh
mkdir -p datasets/sales/data
cat > datasets/sales/data/orders.csv <<'CSV'
order_id,region,product,units,revenue
1,West,Widget,12,480
2,East,Gadget,5,275
3,West,Gadget,9,495
4,South,Widget,20,800
5,East,Widget,7,280
6,North,Gizmo,3,210
7,West,Gizmo,11,770
8,South,Gadget,6,330
CSV
cat > datasets/sales/config.yml <<'YAML'
name: sales
source: data/orders.csv
table: orders
YAML

# order matters: scaffold (which installs the staging model) must come BEFORE up
sediment load sales                    # land orders.csv → sales_raw.orders
sediment profile sales                 # → datasets/sales/profile.json
sediment scaffold sales --write        # AI proposes a typed, tested staging model + wires it in
sediment up sales                      # now dbt builds + tests the sales staging, green
DATASET=sales sediment ask "total revenue by region"
```

**SHOW:** a brand-new dataset landing in its **own** schemas (`sales_raw/_staging/_marts`), a scaffolded staging model, and an NL answer over it — *"West region highest at 1745, then South, East, North."* (In the dashboard, the **dataset selector** switches the whole app to `sales`.)

> Each step mirrors the pipeline: **load → profile → scaffold → build → ask**. That's the
> story in five commands. (`scaffold` before `up` is the one ordering that matters —
> `up` builds the model that `scaffold` installed.)

**LAND:** "Same framework, any tabular file, minutes to a tested stack — each dataset isolated in its own schemas in the one warehouse file."

---

## Part D — If something breaks live (recovery)

| Symptom | Fix |
|---|---|
| `needs an Anthropic key` | `echo "sk-ant-…" > anthropic.txt`, re-run. (Core acts — `up`, Report — work without a key.) |
| Dashboard port busy | Streamlit auto-picks 8502, or `streamlit run dashboard/app.py --server.port 8600` |
| LLM slow / hangs on stage | The terminal `sediment ask` is faster than the dashboard — fall back to it; or re-ask once. |
| `orchestrate --break` shows many failures | A stray dataset is onboarded — you're not on a clean clone. Reclone, or `./demo/reset.sh`. |
| A live edit wedged something | `git checkout -- . && git clean -fd datasets/sales` then `./demo/reset.sh` |
| **Total fallback** | The presentation site + a single `sediment ask` in the terminal always work. That's enough to tell the story. |

**Known-good questions** (verified against the AnAge marts — safe to use live):
- "which animals live far longer than their body size predicts?"
- "how many species are in each taxonomic class?"
- "what is the average longevity for mammals?"
- "which taxonomic class has the most species?"

---

## Part E — Teardown

```zsh
# Ctrl-C the dashboard, then:
./demo/reset.sh                 # drop the warehouse + remove the `sales` demo dataset
# or, to return to a pristine checkout entirely:
git checkout -- . && git clean -fd
```

---

## Part F — Cheat sheet: command → idea

| When you run… | The idea you're landing |
|---|---|
| `sediment up` | Deterministic core. Reproducible, tested, offline. |
| `sediment ask "…"` | AI at the consumption edge — read-only, every number traces to SQL. |
| the L1→L7 trace | Trust is *engineered*, layer by layer — transparent failure, not a confident guess. |
| Build a model tab | Ad-hoc question → reviewable, tested dbt. View/build governance is server-side. |
| `orchestrate --break` | AI *operates* the pipeline; the pipeline fails loudly on its own. |
| `sediment up sales` | Dataset-agnostic. "AI at the edges" is a stance, not a one-off. |
