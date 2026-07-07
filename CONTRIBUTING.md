# Contributing to sediment

Thanks for your interest. sediment is a small project with a strong point of view, so this guide is short but opinionated.

## The one rule that governs everything

**Deterministic core, AI at the edges.** The transform path (ingest → staging → marts) is plain, tested, reproducible dbt SQL. AI participates at three seams only: proposing models (`scaffold`), explaining runs (`orchestrate`), and answering questions (`ask` / the dashboard). Contributions that put an LLM inside the deterministic transform path will be declined regardless of how clever they are — that boundary is the product.

## Getting set up

```bash
git clone https://github.com/camharris93/sediment.git
cd sediment
pip install -r requirements.txt
python run.py up          # full offline build: download → load → profile → dbt run → dbt test
python run.py dashboard   # http://localhost:8501
```

No API key is needed for the core data flow or the test suite. The AI seams need an Anthropic key (see `.env.example`).

## Before you open a PR

```bash
ruff check .
python -m pytest -m "not live"
```

Both must be green. CI runs the same suite on Linux and Windows across Python 3.10 and 3.13, plus a from-scratch `run.py up` build and the replay evals — so a change that only works on your machine, with your warehouse state, will fail there.

## Security-sensitive areas

The read-only SQL guard (`engine/query/execution.py`) is a real security boundary: chat-generated SQL runs against it. If you change the guard or anything that feeds it:

1. **Write the attack first.** Add the motivating adversarial case to `tests/test_read_only_guard.py` before the fix — statement chaining, file-reading table functions, extension loads, and friends all have precedent there.
2. Prefer structural rules (e.g. "no FROM source backed by a function, allowlist excepted") over blocklists of known-bad names.

The same spirit applies to `engine/modeling.py` (chat → dbt model promotion) and path handling in `engine/config.py`.

## What contributions are welcome

- **Bug fixes with a failing test.** The test is the contribution; the fix is the follow-through.
- **Ingest/profile coverage** for new tabular formats and messier real-world files.
- **Eval questions.** New golden questions in `evals/golden_questions.yaml` with result invariants make the NL→SQL agent measurably better or expose where it isn't.
- **Docs and quickstart friction.** If step 3 confused you, that confusion is a bug report.
- **Reference datasets/patterns** that run offline on synthetic or public data. If it can't run on a laptop with fake data, it doesn't belong here.

For anything larger, open an issue first — see [ROADMAP.md](ROADMAP.md) for where the project is headed, and remember the audience: solo and small-team practitioners. Features that only make sense for a twelve-person platform team are out of scope.

## Style

- Python: `ruff` is the arbiter; match the surrounding code otherwise.
- SQL/dbt: every model states what one row means. If you can't write that sentence, the model isn't done.
- Windows matters: this project develops and CIs on Windows and Linux both. Avoid Unicode in console `print()` and hardcoded `/` path assumptions.

## License

MIT. By contributing you agree your contributions are licensed under the same terms.
