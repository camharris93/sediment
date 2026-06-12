#!/usr/bin/env python
"""sediment task runner — the one cross-platform entry point.

`make up` delegates here; on machines without `make` (e.g. Windows), call it
directly:

    python run.py up            # download -> load -> profile -> dbt run -> dbt test
    python run.py dashboard     # launch the Streamlit dashboard
    python run.py docs          # build the dbt lineage docs
    python run.py scaffold      # AI build-time scaffolding (needs a key)
    python run.py orchestrate   # AI run/monitor/explain over the core (needs a key)
    python run.py ask "question"# NL->SQL over the marts (needs a key)

Every target takes an optional dataset name (default: $DATASET or "anage").
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# Make stdout UTF-8 so box-drawing/emoji don't crash the legacy Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from engine.config import (
    DBT_PROJECT_DIR,
    REPO_ROOT,
    WAREHOUSE_PATH,
    active_dataset,
    load_dataset_config,
)

PY = sys.executable


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    print(f"\n$ {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if res.returncode != 0:
        raise SystemExit(res.returncode)


def _dbt_cmd() -> list[str]:
    exe = shutil.which("dbt")
    return [exe] if exe else [PY, "-m", "dbt.cli.main"]


def _dbt_env() -> dict:
    env = os.environ.copy()
    env["DBT_PROFILES_DIR"] = str(DBT_PROJECT_DIR)
    return env


# ─────────────────────────────────────────────────────────────────────────────
# Targets
# ─────────────────────────────────────────────────────────────────────────────

def t_download(dataset: str) -> None:
    cfg = load_dataset_config(dataset)
    if not cfg.download_url:
        print(f"[download] '{dataset}' has no download_url — assuming local file at {cfg.resolve_source()}")
        return
    dest = cfg.resolve_source()
    if dest.exists():
        print(f"[download] {dest.name} already present — skipping fetch.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] {cfg.download_url} -> {dest}")
    urllib.request.urlretrieve(cfg.download_url, dest)
    print(f"  [ok] {dest.stat().st_size:,} bytes")


def t_load(dataset: str) -> None:
    _run([PY, "-m", "engine.ingest", dataset])


def t_profile(dataset: str) -> None:
    _run([PY, "-m", "engine.profile", dataset])


def t_dbt_run(dataset: str) -> None:
    _run(_dbt_cmd() + ["run"], cwd=DBT_PROJECT_DIR, env=_dbt_env())


def t_dbt_test(dataset: str) -> None:
    _run(_dbt_cmd() + ["test"], cwd=DBT_PROJECT_DIR, env=_dbt_env())


def t_docs(dataset: str) -> None:
    _run(_dbt_cmd() + ["docs", "generate"], cwd=DBT_PROJECT_DIR, env=_dbt_env())
    print("\n[docs] generated. Serve with:  python run.py docs-serve")


def t_docs_serve(dataset: str) -> None:
    _run(_dbt_cmd() + ["docs", "serve"], cwd=DBT_PROJECT_DIR, env=_dbt_env())


def t_up(dataset: str) -> None:
    print(f"== sediment: building the full pipeline for '{dataset}' ==")
    t_download(dataset)
    t_load(dataset)
    t_profile(dataset)
    t_dbt_run(dataset)
    t_dbt_test(dataset)
    print(
        "\n[ok] Pipeline is green. The warehouse is at "
        f"{WAREHOUSE_PATH.name} (raw + staging + marts).\n"
        "  • Dashboard:   python run.py dashboard\n"
        "  • Lineage doc: python run.py docs\n"
        "  • Ask in NL:   python run.py ask \"which animals live longest for their size?\""
    )


def t_dashboard(dataset: str) -> None:
    app = REPO_ROOT / "dashboard" / "app.py"
    # Launching via the local authoring runner grants BUILD capability (chat can
    # promote answers into dbt models). A bare/deployed `streamlit run` does NOT
    # set this, so a shared report stays read-only "view" mode by default.
    env = os.environ.copy()
    env.setdefault("SEDIMENT_MODE", "build")
    _run([PY, "-m", "streamlit", "run", str(app)], env=env)


def t_scaffold(dataset: str, *flags: str) -> None:
    _run([PY, "-m", "engine.scaffold", dataset, *flags])


def t_orchestrate(dataset: str, *flags: str) -> None:
    _run([PY, "-m", "engine.orchestrate", dataset, *flags])


def t_ask(dataset: str, question: str | None = None) -> None:
    cmd = [PY, "-m", "engine.query.cli"]
    if question:
        cmd.append(question)
    _run(cmd)


def t_init(target_dir: str) -> None:
    """Scaffold a new sediment WORKSPACE at `target_dir` so the installed tool can
    run against a project anywhere: copies the dbt project config + macros, creates
    empty datasets/ and model dirs, and writes a sample dataset config to edit."""
    import engine
    template_root = Path(engine.__file__).resolve().parents[1]
    dest = Path(target_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        print(f"[init] {dest} is not empty — refusing to scaffold over existing files.")
        raise SystemExit(1)

    # dbt project: config files + macros, with empty model dirs.
    src_dbt = template_root / "dbt_project"
    (dest / "dbt_project" / "models" / "staging").mkdir(parents=True, exist_ok=True)
    (dest / "dbt_project" / "models" / "marts").mkdir(parents=True, exist_ok=True)
    for rel in ["dbt_project.yml", "profiles.yml"]:
        src = src_dbt / rel
        if src.exists():
            shutil.copyfile(src, dest / "dbt_project" / rel)
    if (src_dbt / "macros").is_dir():
        shutil.copytree(src_dbt / "macros", dest / "dbt_project" / "macros")
    for keep in ["models/staging", "models/marts"]:
        (dest / "dbt_project" / keep / ".gitkeep").write_text("", encoding="utf-8")

    # A sample dataset config to fill in.
    sample = dest / "datasets" / "example"
    (sample / "data").mkdir(parents=True, exist_ok=True)
    (sample / "config.yml").write_text(
        "name: example\n"
        "source: data/your_file.csv   # path (relative to this dir) or a URL\n"
        "table: your_table            # lands as example_raw.your_table\n"
        '# delimiter: "\\t"            # optional; auto-sniffed for csv/tsv\n',
        encoding="utf-8",
    )
    print(
        f"[init] scaffolded a sediment workspace at {dest}\n"
        "  Next:\n"
        f"    cd {dest}\n"
        "    # drop a file in datasets/example/data/ and edit datasets/example/config.yml\n"
        "    sediment up example          # load -> profile -> dbt run -> dbt test\n"
        '    sediment ask "..."           # NL->SQL over the marts\n'
        "  (sediment finds this workspace via the current directory, or set SEDIMENT_HOME.)"
    )


def t_clean(dataset: str) -> None:
    for p in [WAREHOUSE_PATH, WAREHOUSE_PATH.with_suffix(".duckdb.wal"),
              REPO_ROOT / "profile.json"]:
        if p.exists():
            p.unlink()
            print(f"  removed {p.name}")
    tgt = DBT_PROJECT_DIR / "target"
    if tgt.exists():
        shutil.rmtree(tgt)
        print("  removed dbt_project/target/")


TARGETS = {
    "download": t_download, "load": t_load, "profile": t_profile,
    "dbt-run": t_dbt_run, "run": t_dbt_run, "test": t_dbt_test,
    "docs": t_docs, "docs-serve": t_docs_serve, "up": t_up,
    "dashboard": t_dashboard, "scaffold": t_scaffold,
    "orchestrate": t_orchestrate, "ask": t_ask, "clean": t_clean,
    "init": t_init,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("Targets:", ", ".join(sorted(TARGETS)))
        return 0
    target = argv[0]
    rest = argv[1:]
    fn = TARGETS.get(target)
    if fn is None:
        print(f"Unknown target '{target}'. Try one of: {', '.join(sorted(TARGETS))}")
        return 2

    # `init` takes a target DIRECTORY, not a dataset name.
    if target == "init":
        if not rest:
            print("Usage: sediment init <new-workspace-dir>")
            return 2
        t_init(rest[0])
        return 0

    # `ask` takes a free-text question; everything else takes an optional dataset.
    if target == "ask":
        dataset = active_dataset()
        question = " ".join(rest) if rest else None
        t_ask(dataset, question)
        return 0

    # Split a positional dataset name from any -/-- flags (e.g. scaffold mydata
    # --write, or orchestrate anage --break).
    positional = [a for a in rest if not a.startswith("-")]
    flags = [a for a in rest if a.startswith("-")]
    dataset = positional[0] if positional else active_dataset()
    if target in ("scaffold", "orchestrate"):
        fn(dataset, *flags)
        return 0
    fn(dataset)
    return 0


def cli_main() -> None:
    """Console-script entry point (the installed `sediment` command)."""
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
