# sediment investigate

`sediment ask` answers one question with one trustworthy number. `sediment investigate` takes a question with no single query behind it ("why is churn up?"), proposes competing explanations, tests each one with aggregated cuts through the unchanged L1–L7 pipeline, judges every cut with deterministic Python, sends a skeptic after every confirmed finding, and proposes what it learned to an append-only memory a human commits. The LLM writes hypotheses, cut wording and the choice of skeptic checks. It never assigns a label, computes a number, or decides whether a check passed.

This is a learning experiment capped at a week, not a product. Part 1 is what got built and what the build turned up. Part 2 is the design it was built against, with section numbers the code cites.

```bash
python run.py up saasco && python run.py up saasco_holdout   # seeded planted-truth data, offline
python run.py investigate-demo                               # no key, zero LLM calls, ~3 s
python run.py investigate "why is churn up in Q2 2026?" saasco   # live, needs a key
```

The offline demo, verbatim from this branch:

```
2. Simpson's paradox — "Does having a salesperson involved help retention?"
   judge: sales-assisted 0.0385 vs 0.0316 monthly churn (+22%, n = 13,954 vs 18,654) -> CONFIRMED
   skeptic simpsons on `size_band`: direction REVERSES within size_band strata covering 100% of n
   -> REFUTED.  False findings (scored vs answer key): judge-only 1, with skeptic 0
3. Decoy and fan-out — "Why is churn up in Q2 2026?"
   healthcare Q1 churn 0.281 vs 0.121 (+132%, n = 32) -> judge CONFIRMED;
     skeptic small_n: too few events behind the rates (9 vs 218; need >= 10 per arm) -> SUGGESTIVE
   lost MRR per churned subscription +29% after 2026 -> L6 ['fanout_risk'] -> badge flagged -> SUGGESTIVE
4. Traceability
   220 numerals across 3 reports' findings; 0 not traceable to an EvidenceCell
```

Read that demo for what it is. The plans it replays (`evals/investigations/reference_plans/`) are **hand-written by someone who knew the answer key**. They prove the deterministic half works on real planted data: the judge, the skeptic check functions, L6, the report tracer and the scorer. They say nothing about whether an LLM planner finds these stories. Only the live matrix measures that, and it has not been run yet (see "Status").

---

# Part 1 — As built

## Status against the week plan

| Day | Exit check | State |
|---|---|---|
| 1 | `pytest -m "not live"` green; `up saasco` and `up anage` green; truth replay passes on dev and holdout; Cam signs off on the marts | Done except the mart sign-off. 188 offline tests pass (182 + 6 that skip when no warehouse is built). Truths reproduce 21/21 on both variants. **Marts are waiting on Cam's leakage review** (`dbt_project/macros/saasco.sql`). |
| 2 | One live `investigate` on Q1 under budget; offline runner test with a fake LLM; scorer works on a recorded run; JudgeConfig frozen | Built and tested offline. **The live run was not possible:** the build container had no API key. JudgeConfig is frozen as committed (`engine/investigate/evaluate.py`). |
| 3 | First live matrix + Cam's blind rating → Gate 1 | Tooling ready (`harness.py --investigate --live`, `ratings.py sheet/agree`). Not run. |
| 4 | Skeptic + tests; H vs H−skeptic on dev | Skeptic built and unit-tested with planted rows. Live comparison not run. |
| 5 | Memory, append-only tests, one sequence run | Built and tested; `--sequence` protocol implemented. Not run live. |
| 6–7 | Holdout matrix, RESULTS.md | Not run. RESULTS.md is deliberately absent: there are no results to write. |

Cam went to the gym and said not to stop for review, so the Day-1 and Day-3 gates were crossed without his sign-off. Gate 1 (kill or continue) still needs live data and his ratings, and nothing built here pre-empts that decision.

### Running the experiment (needs a key)

```bash
python evals/harness.py --investigate --live --config full --reps 1          # day 2: one Q1 run first
python evals/harness.py --investigate --live --config B2   --reps 1          # day 3 matrix, dev
python evals/harness.py --investigate --live --config B1   --reps 1
python evals/investigations/ratings.py sheet runs/investigations/eval_<ts>   # blinded sheet + key
python evals/investigations/ratings.py agree runs/investigations/eval_<ts>   # Spearman vs auto score
python evals/harness.py --investigate --live --config no_skeptic --reps 3 --dataset saasco_holdout
python evals/harness.py --investigate --live --sequence Q6,Q1,Q3 --reps 3   # memory protocol
python evals/harness.py --investigate --live --sequence Q6,Q1,Q3 --reps 3 --no-memory
```

Every live run enforces a total-spend hard stop (`--max-total-usd`, default 120) on top of the per-investigation budget. `ANTHROPIC_MODEL_STRONG` sets B2's model; it defaults to `ANTHROPIC_MODEL`. Prices in `engine/investigate/budget.py` are list prices as I know them and need checking before the cost numbers mean anything.

## Findings while building

Building the harness turned up four problems in the existing engine. Two of them would have corrupted the experiment without anyone noticing.

**1. L6 fan-out detection was silently off (fixed).** sqlglot ≥ 28 renamed the Select arg `from` to `from_`. `plausibility._resolve_alias_map` read `args.get("from")`, so the FROM table never entered the alias map and `check_fanout` returned nothing for every query. `requirements.txt` pins `sqlglot>=25.0`, and this container resolved 30.26, so a fresh install has never had fan-out warnings. S7 (the invoice-attempt fan-out) is caught *only* through L6, so without the fix the planted trap could not be detected. The fix is one line accepting both names, plus `tests/test_plausibility_fanout.py`. It is the only change to L1–L7 behaviour beyond the `explain` kwarg.

**2. L3 rejects some valid SQL (not fixed: the design rules L3 out of scope).** Repro in a test-style snippet:

```sql
-- rejected: "column `first_start` is ambiguous — it exists on (na, sp)"
with sp as (select s.account_id, min(s.start_date) as first_start from saasco_staging.stg_subscriptions s group by 1),
na as (select a.region, sp.first_start from sp join saasco_staging.stg_accounts a on a.account_id = sp.account_id)
select count(*) from na where first_start < date '2025-01-01'
-- also rejected: any outer reference to a column of a `select *` CTE
with x as (select * from saasco_marts.mart_monthly_kpis) select month from x
```

Nine of the 21 answer-key queries failed L3 before I alias-qualified every column. Analytic cuts are exactly the CTE-heavy SQL that hits this. The first case gives L2 actionable feedback ("qualify it"), so it probably costs a retry. The second tells L2 the column doesn't exist, which it may not recover from. Watch L2 attempts and the `transparent_failure` share on Day 3. If Gate 1's ">30% untestable" fires, check this first.

**3. L6's `COUNT(*)`-over-a-join rule fires on intended child counts.** `count(*)` over `lifecycle ⋈ accounts` is flagged as fan-out even when the analyst means to count subscriptions. A flagged badge caps a cut at *suggestive*, so this lowers recall rather than causing false findings. The reference plans use `count(child_key)` instead. It goes in the failure taxonomy for Day 7.

**4. Two already-known gaps, unchanged:** `DuckDBWarehouse.execute` accepts `timeout_sec` and ignores it, and `t_ask` ignores its dataset argument. `investigate` passes the dataset explicitly.

## Decisions made (Cam delegated the open calls)

- **D1, where the data lives:** on this branch, `.gitignore` un-ignores `datasets/saasco*`, `dbt_project/models/*/saasco*__*` and ignores `datasets/*/findings.proposed/` and `runs/`. Nothing goes to `main` without a separate decision. The branch is `claude/dreamy-edison-ojd2q3`, the harness-assigned name, rather than `investigate`.
- **D2, dbt scope:** `up/run/test <dataset>` run `dbt --select "<dataset>__*"`, keyed on the same naming convention the `.gitignore` uses, so no per-model tags are needed. `up anage` no longer depends on any other dataset's raw schema. CI additionally builds both saasco variants and runs truth replay and the demo.
- **One model definition, two variants.** Staging and marts are Jinja macros in `dbt_project/macros/saasco.sql`; each model file is a two-line wrapper. Dev and holdout warehouses are built by byte-identical logic, which the holdout comparison depends on.
- **Truths come from staging, not marts.** Answer-key SQL reads only `*_staging`, so the marts are *tested by* the truths rather than being their source. `generate.py` computes them on a scratch copy of staging built with `STAGING_TYPES`, and a test checks those types against the built warehouse.
- **Claim.metric is a closed vocabulary** (`engine/investigate/types.py: METRICS`). It's generic analytics vocabulary, and it's what makes deterministic matching possible.
- **Small-n means too few *events*, not only too few rows.** The decoy group has n = 32, which clears `min_n = 30`, so a rows-only rule would pass it. The skeptic's `small_n` also requires at least 10 events per arm (`JudgeConfig.min_events`), the standard rule of thumb. This is the rule that downgrades the decoy, so it matters, and I set it before seeing any live output.
- **Skeptic cuts are built in Python, not by the LLM.** The LLM picks the check and names a validated confounder; the skeptic cut is the target cut's question plus a fixed suffix, with the same columns and the same TestSpec. "All checks untestable" leaves the label alone, marks `skeptic_outcome: not_run` and notes "skeptic could not test", so it never counts as *survived* for memory auto-accept.
- **Numbers in free text are masked.** Hypothesis statements come from the planner; any numeral in them (other than a year) renders as `#`. Reason strings come from Python, format through the same `numfmt.fmt` as the evidence cells, and every number they cite has a cell. `untraced_numbers()` enforces this, and tests fail on any untraced numeral.
- **B2's failed SQL makes the finding untestable.** Its labels are its own, but a finding that cannot show its number cannot be confirmed.

## Deviations from the design (logged as protocol deviations)

- **2026-10-30, Step 0 (before any live run):** `run.py` `_dataset_select` used `--select <dataset>__*`, which matches nothing on dbt 1.11 (the fqn glob runs against the full dotted path), so `up saasco` reported green while building no models. Changed to `*.<dataset>__*`. `budget.py` `PRICES` gained the current model family; the prices for the models this experiment uses (sonnet-4-6, haiku-4-5) were already right. No judge thresholds, prompts or generator code touched.
- **2026-10-30, after the first live run, before the Day-3 matrix (Cam's call: "fix the bug before the matrix").** The first live Q1 run (`2026-10-30T14-07-06_…_0fd0c9`: $0.37, 256 s, recall@confirmed 0, 0 false findings, decoy avoided) lost 4 of 9 test cuts as untestable. Three were label punctuation: the planner's TestSpec said `Q2-2026` / `2026-Q2` / `1-6`, L2's SQL emitted `q2 2026` / `1-3 months`, and the judge compared exact lowercase strings. One was the documented L3 CTE false positive (two "ambiguous column `period_label`" rejections of valid SQL; attempt 3 passed but renamed the column). Three bug fixes, each with a test in `tests/test_investigate_labels.py`:
  1. `evaluate.py` `same_label`: arm labels compare by sorted alphanumeric tokens, so `Q2-2026` = `q2 2026` = `2026-Q2` while `1-6` ≠ `1-3 months`. No threshold changed.
  2. `cuts.py` `arm_labels_text`: the cut question now states the exact group labels the TestSpec will look for. The TestSpec named values L2 never saw; this is the §5.2 contract ("Return exactly these columns") extended to values. Planner and skeptic prompts untouched; `test_prompt_leak` passes.
  3. `report.py`: quoted labels inside Python-authored reason strings (`'1-3 months'`) and claim segment values are no longer counted as untraced numerals. Traceability on the first run was reported as 0 for this reason alone; replaying it after the fix gives 1.0 and recovers h2.c1 and h4.c1 as *refuted* (recall unchanged at 0).
  The matrix therefore measures H with these fixes and B2/B1 without any equivalent change (B2 labels its own findings; B1 is prose).

- **S3 magnitude:** about 50% of EU 2026 churners have 2+ failed attempts (observed 0.508 dev, 0.554 LATAM holdout, against ~4% elsewhere), not the designed ~70%. Reaching 70% needed EU churn to roughly quadruple, which swamped every other 2026 metric. Failures also hit only accounts at least 100 days old (card-on-file expiry), which keeps S3 out of 90-day new-account metrics and so out of S6's way.
- **S2 magnitude:** the starter post/pre 90-day churn ratio is 1.40, against a 1.08 control in growth + pro. That's a diff-in-diff of about 1.3x, not 1.8x.
- **S6 is "mix contributes", not "purely mix".** Other stories also move channel rates (S2 hits new starters in every channel). The answer key states the observed absolute mix effect (S6.t4: +1.2 pp dev, +1.0 pp holdout) instead of claiming every channel is flat.
- **Holdout seed 77015** was picked from four candidates so that the absent S4 reads as a clean null (ratio 0.93) and S5 reverses in all four bands. The dev decoy sub-seed is searched in code (`assign_post_hoc`), as designed.
- **Holdout Q3** reads "Latin America" instead of "Europe", and holdout Q2 and Q5 keep S4 out of recall because it does not exist there (`saasco_questions.yaml`).
- **Mart naming:** `mart_subscription_lifecycle` has `churned_within_90d` (no successor subscription) and `ended_by` (`churn | plan_change`) instead of `ended_within_90d`, because a plan change also "ends" a subscription.

## Planted truths, as observed (dev / holdout)

| id | what | dev | holdout |
|---|---|---|---|
| S1.t1 | renewal-month churn, annual subs due 2026-Q2 | 0.274 | 0.264 |
| S1.t2 | mean monthly logo churn, 2026-Q2 / 2026-Q1 | 1.18 | 1.13 |
| S2.t1 / t2 | 90-day churn post/pre price step: starter / control | 1.40 / 1.08 | 1.43 / 1.15 |
| S3.t1 / t2 | 2026 churners with 2+ failed attempts: segment / rest | 0.508 / 0.040 | 0.554 / 0.042 |
| S4.t1 | months 2–6 churn, low / high activation | 2.05 | 0.93 (absent) |
| S5.t1 / t2 | pooled sales-assisted churn ratio / bands where SA is lower | 1.22 / 4 of 4 | 1.27 / 4 of 4 |
| S6.t1 / t2 | growing channel's share of new accounts, before / 2026-H1 | 0.106 / 0.353 | 0.105 / 0.293 |
| S7.t1 / t2 | all invoice rows / paid rows, overall / segment 2026 | 1.09 / 1.34 | 1.08 / 1.32 |
| D1.t1 / t2 | decoy group churn / everyone, 2026-Q1 / 2025-Q4 | 2.27 / 0.98 | 2.46 / 1.19 |

Exact values and SQL are in `evals/investigations/saasco*_answer_key.yaml`, which `generate.py` writes and the build re-checks.

## File map

```
engine/_llm.py                    usage_meter(): contextvar stack fed by every complete()
engine/query/orchestrator.py      run_to_executed_answer(..., explain=True)
engine/query/translation.py       translate(..., explain=True): False skips the L7 LLM call
engine/query/plausibility.py      sqlglot from_/from fix
engine/investigate/
  types.py      the §4 shapes, JSON round-trip
  plan.py       planner prompt + deterministic validation + one repair
  cuts.py       run_cut (full L1-L7) / run_cut_sql (L3-L6 replay)
  evaluate.py   JudgeConfig, evaluate, combine, check_invariants
  skeptic.py    check chooser, deterministic skeptic cuts, five check functions
  memory.py     findings.yml load / propose / accept (append-only) + CLI
  report.py     template markdown, numeral masking, untraced_numbers()
  budget.py     Budget, BudgetTracker, prices
  runner.py     investigate(), replay(), write_run()
  baselines.py  B1 multi-hop, B2 one strong call
  cli.py        python -m engine.investigate.cli
datasets/saasco/generate.py       seeded generator, both variants, truths writer
dbt_project/macros/saasco.sql     staging + marts, once, for both variants
evals/investigations/
  build_answer_keys.py            both keys from one template (edit stories here)
  saasco*_answer_key.yaml         stories, match signatures, truths + observed
  saasco_questions.yaml           Q1-Q6, per-variant overrides
  score.py                        matching + every §8.3 metric, no LLM
  run.py                          --truths / --replay / --live (+ --sequence)
  ratings.py                      day-3 blinded sheet, Spearman agreement
  reference_plans/                hand-written plans for the offline demo
  demo.py                         python run.py investigate-demo
```

---

# Part 2 — Design (as written 2026-10-30 against `main` @ `d5bbf9d`)

## 1. Why this exists

`sediment ask` answers one question with one trustworthy number. L1 to L7 make sure the SQL is real, read-only, plausible and traceable. An analyst's real job is different. They investigate a question like "why is churn up?", which has no single query. That takes competing explanations, several cuts for each one, someone trying to explain the result away, and a memory of what was learned last time.

This layer adds that investigation loop on top of the existing pipeline. Every number still comes from SQL that passed L3 to L5, so AI stays at the edges.

The question being tested: does an analytics-specific harness (hypotheses, then checked cuts, then deterministic verdicts, then a skeptic, then memory) find more true stories and fewer false ones than one strong call or today's `run_multi_hop`, on data where the answer is known?

The lesson from Pulse is built in. The headline metric is ground truth from planted data, never an AI judging AI. A human (Cam) rates real outputs on day 3, and the experiment has kill criteria written down before any code exists.

## 2. What the repo already gives us (and its limits)

| Existing piece | What we reuse | Limit that shapes this design |
|---|---|---|
| `engine/query/orchestrator.py` `run_to_executed_answer(question, ctx, *, max_retries=3, on_event=None, history=None) -> PipelineResult` | Each cut runs through this function unchanged: L1 intent, L2 generation, L3–L4 retry loop, L5 guarded execute, L6 plausibility, L7 badge | NL in only, no SQL-in entry point; no slot for definitions besides `history`; drops token usage; every call makes an L7 LLM call for prose we don't need |
| `PipelineResult` | Raw material for evidence and verdicts | `rows` capped at `MAX_RESULT_ROWS = 1000`, so cuts must aggregate; a truncated result is untestable |
| `engine/query/grounding.py` `build_grounding_context(dataset, schemas, warehouse)` | Build once per investigation; `schemas=` gives the staging-only ablation | Profiling scans every column per build; `_derive_grain` finds only single-column keys, so every synthetic table needs a surrogate key |
| `_infer_relationships` + L6 `check_fanout` | The fan-out trap (S7) is caught here | Relationships come from matching key-like column names; synthetic data must use consistent `account_id` / `subscription_id` |
| `engine/query/multihop.py` `run_multi_hop` | Baseline B1 | Prose output: human-scored only |
| `engine/query/conversation.py` | Optional: skeptic reads a prior cut's result | Not needed in week 1 |
| `engine/_llm.py` `complete(...) -> (text, LLMUsage)` | Single choke point for every LLM call: where a meter belongs | No meter or budget today |
| `evals/harness.py` | Investigation eval mode sits next to these; `check_invariants` checks `columns`, `min_rows` | `GOLDEN` fixed path; `--dataset` defaults to `anage`; needs `--cases` |
| `run.py` `TARGETS` / `main` | New `investigate` and `findings` targets | `t_ask` ignores its dataset argument; investigate passes it explicitly |
| `warehouse.py` `DuckDBWarehouse.execute(..., timeout_sec=60)` | Unchanged | `timeout_sec` is never enforced; wall-clock is checked between cuts |
| `.gitignore` | Unchanged without Cam's say-so | "AnAge is the ONLY dataset that ships" — decision D1 |
| `run.py` `t_dbt_run` / `t_dbt_test` | Unchanged | They run all models; once saasco models exist, `up anage` fails without `saasco_raw` — decision D2 |
| ROADMAP v0.3 / v1.0 | `findings.yml` definitions are a small registry precursor; memory tests "conversation becomes convention" | Don't build the registry or `sediment validate` this week |

**D1. Where the planted dataset lives.** Recommendation: a local branch; un-ignore only `datasets/saasco*` and `dbt_project/models/*/saasco*__*` there. Shipping to `main` is a separate, later call.
**D2. dbt scope.** (a) `up <dataset>` runs `dbt run/test --select` scoped to that dataset, or (b) CI also runs `up saasco`. Recommendation (a).

## 3. The loop

```
sediment investigate "why is churn up in Q2 2026?" saasco [--config full] [--budget-usd 1.00]

 0. ctx = build_grounding_context(dataset, schemas=config.schemas)          (once)
    memory = load_findings(dataset)            (skipped if --no-memory)
 1. PLAN   1 LLM call  -> 3-4 Hypotheses, each with 1-3 Cuts + a TestSpec per cut
           validated deterministically (JSON shape, test kinds, budget fit)
 2. CUT    for each Cut: run_to_executed_answer(cut.pipeline_question(defs), ctx, explain=False)
           -> CutResult (status, sql, rows, badge, warnings, assumptions, usage)
 3. JUDGE  deterministic: evaluate(cut.test, cut_result) -> Verdict
           hypothesis label = combine(verdicts)          (no LLM)
 4. SKEPTIC for each CONFIRMED hypothesis (skipped if --no-skeptic):
           1 LLM call picks <=2 checks from a fixed menu -> more Cuts through step 2,
           deterministic check functions -> survived | downgraded | refuted
 5. REPORT template-rendered report.md + investigation.json; every number is an
           EvidenceCell from a checked result (no free-text numbers)
 6. PROPOSE memory delta -> datasets/<name>/findings.proposed/<run_id>.yml
           (a human runs `sediment findings accept <run_id>` to append)
```

In a full investigation the LLM writes the hypotheses, the cut wording and the skeptic's choice of checks, plus L1 and L2 inside every cut. It never assigns a label, computes a number, or decides whether a check passed. That is the main line drawn against Pulse.

## 4. Data shapes

As built in `engine/investigate/types.py`: `TestSpec` (group_diff | before_after | threshold | reconcile, with value/n/group/time/part/total columns, `a`/`b`, `split_at`, `direction`, `min_rel_diff = 0.20`, `min_n = 30`), `Cut`, `Claim` (structured so the answer key can match it without an LLM), `Hypothesis`, `EvidenceCell` (the only way a number reaches the report), `CutResult`, `Verdict`, `SkepticCheck`, `Finding`, `Investigation`. Everything serializes to `runs/investigations/<run_id>/investigation.json` beside `report.md` and `trace.jsonl` (events tagged with `cut_id`).

## 5. Components

### 5.1 Planner
One `complete()` call. Input: question, schema summary, memory block, a fixed test menu. Asks for 3–4 competing hypotheses including at least one "boring" one (mix shift, seasonality, definition artifact), 1–3 aggregated cuts each with named output columns and a TestSpec. Python validates the JSON shape, test kinds, that every column a test names is in `expected_columns`, budget fit, and that claim dimensions are real columns or `derived:`. One repair retry, then a transparent failure. The prompt must be dataset-agnostic; a test greps it for answer-key keywords.

### 5.2 Cut runner
`pipeline_question = cut.question + definitions + "Return exactly these columns: … One row per <grain>."`, through `run_to_executed_answer(..., explain=False)` — the whole L1–L7 path, no bypass. Definitions travel in the question text (known limitation; `intent.assumptions` is kept to see whether L1 respected them). Sequential in week 1. The only change to existing code: the `explain` kwarg; default behaviour unchanged, asserted by a test.

### 5.3 Judge — no LLM
1. Untestable if not executed, truncated, missing expected columns, arm rows absent, or badge refused/failed.
2. group_diff / before_after: `rel = (a − b) / |b|`. Confirmed: direction matches, `|rel| ≥ min_rel_diff`, both arms `n ≥ min_n`, badge clean/self_corrected. Suggestive: direction matches but effect under threshold, n under `min_n`, or badge flagged. Refuted: opposite direction, or effect under 5% with both arms at `n ≥ min_n`. A two-proportion z is stored as information only.
3. threshold: a single value against a bound.
4. reconcile: `sum(part_col)` vs `total_col`; a gap over 1% fails.
5. combine: confirmed if ≥1 test cut confirmed and none refuted; refuted if any refuted and none confirmed; suggestive if mixed or only suggestive; untestable if all untestable.

Thresholds live in one `JudgeConfig`, frozen before the first eval run; changing them later is a logged protocol deviation.

### 5.4 Skeptic
Confirmed findings only, ≤ 2 checks each. One LLM call picks checks from the menu and names a confounder (validated) or an alternative definition. Each check is a new Cut (`purpose="skeptic"`) plus a deterministic rule:

| Check | Extra query | Rule |
|---|---|---|
| `small_n` | no | both arms `n ≥ min_n`; the effect holds when the smallest contributing group is dropped |
| `mix_shift` | metric by period × confounder | standardize to the "before" mix; fail if more than half the change disappears |
| `simpsons` | comparison stratified by confounder | fail if direction reverses in strata covering more than half of n |
| `definition_change` | same cut, alternative definition | fail if direction flips or effect falls below `min_rel_diff / 2` |
| `fanout_or_bug` | sometimes: a reconcile cut | any `fanout_risk` warning fails; a reconcile gap over 1% fails |

Any failed `fanout_or_bug` or `simpsons` → refuted. Any other failure → downgraded to suggestive. All pass → survived. All untestable → stays confirmed with "skeptic could not test". The skeptic can only lower a label.

### 5.5 Findings memory
`datasets/<name>/findings.yml`: definitions, findings (claim, label, skeptic outcome, evidence SQL, run id, `supersedes`), data quirks, open questions, runs. AI proposes, human commits (the `scaffold` pattern). `load_findings` injects only accepted, non-superseded entries. `propose_delta` writes `findings.proposed/<run_id>.yml`, never `findings.yml`. `findings accept <run_id> [--ids]` appends with sequential ids; append-only is enforced by re-parsing and deep-comparing the prefix. Memory is context, not evidence: a remembered finding must be re-run to appear as confirmed.

### 5.6 Report
Template-rendered: findings by label, evidence from EvidenceCells, badge per cut, collapsible SQL, skeptic results, "what this didn't test". No LLM prose this week; any later summary must pass a number tracer.

### 5.7 Budget
A contextvar meter in `_llm.py`; `Budget(max_hypotheses=4, max_cuts=12, max_skeptic_cuts=6, max_queries=18, max_input_tokens=250_000, max_output_tokens=40_000, max_usd=1.00, max_wall_s=600)`. Checked between cuts; exhausted → remaining cuts untestable with `budget_*`, report as normal.

### 5.8 CLI and run targets
`python -m engine.investigate.cli "<question>" [--dataset] [--config full|no_skeptic|no_memory|staging_only] [--budget-usd]`; `run.py investigate` and `findings`; a Makefile target. No dashboard tab.

## 6. File layout
See "File map" in Part 1.

## 7. Planted-truth dataset: `saasco`

### 7.1 Generator
Pure Python with `random.Random(seed)`, byte-identical per seed and variant (a test checks it). About 3,000 accounts over 30 months (2024-01 to 2026-06); weekly usage rather than events. Single surrogate keys; shared join-key names. No column gives a story away. Background realism: size-band hazard, a December dip, random support load, ~1% null industries, plan upgrades. The generator computes the truths itself by running each answer-key `fixture_sql` on a scratch DuckDB and writing `observed`.

### 7.2 Tables
`accounts` (account_id, signup_date, company_size_band, industry, region, acquisition_channel, sales_assisted) · `subscriptions` (subscription_id, account_id, plan_tier, billing_interval, list_price_monthly, seats, start_date, end_date, status, cancel_reason) · `invoices` (invoice_id, subscription_id, account_id, invoice_period, invoice_date, amount, status, attempt_number — one row per payment attempt) · `usage_weekly` (usage_id, account_id, week_start, active_users, key_actions) · `support_tickets` (ticket_id, account_id, created_at, category, priority, first_response_hours, csat).

### 7.3 Planted stories (dev)

| id | kind | planted | found by | naive trap |
|---|---|---|---|---|
| S1 | driver | 2025-Q2 promo pushes a big annual cohort; low-usage annual accounts churn at renewal, usage falling 3–5 months before; main reason churn is up in Q2 2026 | churn by billing_interval × months-to-renewal, plus cohort size | reading it as a product or market problem |
| S2 | driver | starter price 29 → 49 on 2025-07-01, grandfathered; the new cohort's early churn rises | early churn by start_date before/after, within starter | averaging old and new |
| S3 | driver | from 2026-01, EU payment-failure spike; most EU cancellations follow 2+ failed attempts but say customer_request/null | share of churners with failed attempts, by region and period | trusting cancel_reason |
| S4 | driver | peak ≤ 2 active users in the first 8 weeks → ~3x churn in months 2–6 | churn by activation bucket | none |
| S5 | trap | sales-assisted accounts concentrated in the high-churn 1-10 band; lower churn within every band, higher pooled | stratify by size band | "sales-assisted churn more" is a false finding |
| S6 | driver | paid_social grows 10% → 35% of new accounts from 2025-10, constant high churn | churn by channel over time, or mix-standardized rate | "quality is getting worse everywhere" |
| S7 | quirk | invoices has one row per attempt; summing without `status = 'paid'` inflates revenue (~8%; ~30% EU 2026) | L6 fanout_risk, skeptic reconcile, proposed quirk | reporting inflated numbers |
| D1 | decoy | healthcare (~40 accounts) shows a 2026-Q1 blip from sampling luck alone | skeptic small_n, no persistence | confirming healthcare as a driver |

**Holdout** (`saasco_holdout`): same schema, different magnitudes and dates; pricing change 2025-03, failures in LATAM, the growing channel is `partner`, S4 absent (finding it is a false finding), decoy on region APAC. Final claims are made on the holdout only.

### 7.4 Answer key
Stories with match signatures (deterministic), truths with fixture SQL, observed values and tolerances; traps carry `match_correct` and `match_naive`.

### 7.5 Questions
Q1 "Why is churn up in Q2 2026?" (S1, S6, S3, S7, D1) · Q2 "What's driving churn among accounts that signed up in 2025?" (S2, S4, S6, S1) · Q3 "Why did churn in Europe jump this year?" (S3, S7) · Q4 "Does having a salesperson involved help retention?" (S5) · Q5 "What predicts which new accounts will churn in their first six months?" (S4, S2, S6, D1) · Q6 "Did anything change in early 2026 that we should worry about?" (S3, S1, D1). True stories outside the relevant list count as "bonus".

## 8. Evaluation

### 8.1 Modes
`--investigate --truths` (no key, CI-safe) · `--investigate --replay <run>` (no key) · `--investigate --live --config … --reps 3 [--questions]` (key) · `--cases <path>`.

### 8.2 Curated marts
`mart_account_months` (one row per account per subscribed month), `mart_subscription_lifecycle` (one row per subscription), `mart_monthly_kpis` (one row per month). Cam reviews them for leakage.

### 8.3 Metrics (all deterministic)
Recall@confirmed (headline) · Recall@any · False findings (headline: confirmed matches to a trap's naive signature, the decoy, or an absent story; plus Cam-marked unmatched) · Decoy avoidance · Trap handling (S5 direction; S7 flagged / carried) · Number correctness · Traceability (100% by construction) · Pipeline health · Cost and time · Stability (3 reps, ranges, Jaccard) · Match coverage (over 30% needing a human → fix the Claim schema first). No AI-graded metric in the headline.

### 8.4 Memory evaluation
Run Q6 → Q1 → Q3 on a fresh `findings.yml`, eval-auto-accepting definitions, quirks and survived findings (`added_by: eval-auto`); compare with `--no-memory`. Track recall/false findings and cost on questions 2–3, and poisoning. Eval memory goes through `SEDIMENT_FINDINGS_PATH`, never the real file.

### 8.5 Leakage guards
Prompt-leak test on planner and skeptic prompts and JudgeConfig defaults; no tell-tale columns; headline numbers from the holdout only.

## 9. Experiment plan

### 9.1 Configurations
B1 (multi-hop, Cam-scored) · B2 (single strong call, SQL through L3–L5, model's own labels) · H (full harness) · H−skeptic · H+memory (sequence) · H-staging. 6 questions × {B2, H, H−skeptic, H-staging} × 3 reps = 72 investigations, plus memory sequences and B1 × 6. Hard stop at $120.

### 9.2 Week plan
Day 1 M0 plumbing + M1 dataset → Day 2 M2 core, B2, scorer, freeze JudgeConfig → Day 3 first live matrix + blind rating → Gate 1 → Day 4 skeptic → Day 5 memory → Day 6 holdout matrix, bug fixes only, each logged → Day 7 RESULTS.md.

### 9.3 Day-3 human rating
20 outputs (6 × {B1, B2, H} + 2 repeat H on Q1), shuffled with random ids; key kept apart. Per finding: correct (y/n/can't tell), would I have checked this first. Per report: act on it/forward it (1–5), most wrong thing, minutes. Agreement: Spearman between "act on it" and auto (recall − false findings); below 0.3, Cam's judgment wins and the scorer is revisited.

### 9.4 Kill and keep criteria (fixed before code)
**Gate 1 (day 3):** stop if H's mean Recall@confirmed on dev is no better than B2's *and* Cam rates H no better than B2; or if more than 30% of H's cuts are untestable from pipeline failure, refusal, truncation or missing columns (then the bottleneck is NL→SQL for analytic cuts).
**Day 7 (holdout, means over 3 reps):** keep the harness if H beats the best baseline by ≥ 1.0 relevant story per question (or 25 pp) with no more false findings, confirms D1 in at most 1 of 3 runs, and stays under $1.00 and 10 minutes. Keep the skeptic if it cuts confirmed false + trap + decoy confirmations by ≥ 50% vs H−skeptic, losing ≤ 1 true story per question. Keep memory if questions 2–3 gain ≥ 0.5 stories or cost ≥ 30% less with zero poisoned propagations. Marts vs staging: report only. Overall kill: if H misses the bar on the holdout, write that down as the result.

## 10. Non-goals
No dashboard tab, UI, MCP server or link; no product; no causal claims; no changes to L3, L5, the read-only guard or `_DENIED`; no AI in the transform path; no LLM judge as a headline metric; no auto-commits to `findings.yml` outside evals; no more datasets, parallelism, metric registry or `doctor`; nothing to `main` or `.gitignore` on `main` without D1.

## 11. Risks and mitigations
Overfitting to the planted stories (leak test, frozen thresholds, holdout) · data too clean or too hard (noise, difficulty knobs, one logged re-tune) · definitions in question text ignored (track assumptions) · NL→SQL can't express analytic cuts (pipeline health, Gate 1, marts) · Claim matching too rigid/loose (match coverage) · nondeterminism (3 reps) · budget overrun (meter, caps) · memory poisoning (context not evidence, measured, human-accept default) · leakage via columns (no mechanism columns, mart review) · `.gitignore`/dbt collisions (D1/D2) · scope creep (one-week cap) · rater bias (blind ids) · unenforced query timeouts (small data, between-cut wall clock).
