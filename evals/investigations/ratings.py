"""Day-3 blind human rating (docs/investigate.md §9.3).

    python evals/investigations/ratings.py sheet runs/investigations/eval_<ts> [--extra-q1 2]
        -> evals/investigations/ratings/day3_sheet.csv   (blinded: random ids, no config)
           evals/investigations/ratings/day3_key.csv     (id -> run/config; DON'T open until done)
           evals/investigations/ratings/day3_reports/<id>.md
    python evals/investigations/ratings.py agree runs/investigations/eval_<ts>
        -> Spearman rho between "act on it" (1-5) and auto (recall@confirmed - false findings)
    python evals/investigations/ratings.py merge runs/investigations/eval_day3 runs/investigations/eval_<a> eval_<b> ...
        -> one eval dir (scores.json + runs/) from several harness invocations, so
           `sheet` and `agree` see the whole matrix (full + B2 + B1 + Q1 repeats)

Rate CORRECTNESS against the data, not taste. B1 is prose, so blinding is imperfect.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "ratings"
SHEET_COLS = ["rating_id", "question", "finding_or_report", "statement", "correct_y_n_cant_tell",
              "would_i_have_checked_first_y_n", "act_or_forward_1_5", "most_wrong_thing", "minutes"]


def sheet(eval_dir: Path, seed: int) -> None:
    data = json.loads((eval_dir / "scores.json").read_text(encoding="utf-8"))
    runs = [s for s in data["scores"] if s.get("run_id")]
    qtext = {s.get("question_id"): s["question"] for s in runs if s.get("question")}
    rng = random.Random(seed)
    ids = rng.sample(range(100, 1000), len(runs))
    rng.shuffle(runs)
    (OUT / "day3_reports").mkdir(parents=True, exist_ok=True)
    rows, key = [], []
    for rid, s in zip(ids, runs):
        rdir = eval_dir / "runs" / s["run_id"]
        if (rdir / "report.md").exists():
            text = (rdir / "report.md").read_text(encoding="utf-8")
            for token in (s["config"], s["run_id"]):
                text = text.replace(token, "■")
            # Config tells: baselines say `mode` baseline, B1 titles say (multi-hop),
            # run ids carry a timestamp that orders the batches.
            text = re.sub(r"`mode` \w+", "`mode` ■", text)
            text = re.sub(r"`run` \S+", "`run` ■", text)
            text = text.replace(" (multi-hop)", "")
            (OUT / "day3_reports" / f"{rid}.md").write_text(text, encoding="utf-8")
        q = s.get("question") or qtext.get(s.get("question_id"), s.get("question_id"))
        rows.append([rid, q, "REPORT", f"see day3_reports/{rid}.md", "", "", "", "", ""])
        for f in s.get("findings", []):
            if f["label"] in ("confirmed", "suggestive"):
                rows.append([rid, q, f["id"], f["statement"], "", "", "", "", ""])
        key.append([rid, s["run_id"], s["config"], s.get("question_id"), s.get("rep")])
    OUT.mkdir(exist_ok=True)
    with (OUT / "day3_sheet.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_COLS)
        w.writerows(rows)
    with (OUT / "day3_key.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rating_id", "run_id", "config", "question_id", "rep"])
        w.writerows(key)
    print(f"[ratings] {len(runs)} reports, {len(rows) - len(runs)} findings -> {OUT / 'day3_sheet.csv'}")
    print("[ratings] day3_key.csv is the unblinding key: do not open it until rating is done.")


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 4) if va and vb else None


def agree(eval_dir: Path) -> None:
    scores = {s["run_id"]: s for s in json.loads((eval_dir / "scores.json").read_text())["scores"] if s.get("run_id")}
    key = {r["rating_id"]: r for r in csv.DictReader((OUT / "day3_key.csv").open(encoding="utf-8"))}
    human, auto = [], []
    for r in csv.DictReader((OUT / "day3_sheet.csv").open(encoding="utf-8")):
        if r["finding_or_report"] != "REPORT" or not r["act_or_forward_1_5"].strip():
            continue
        s = scores.get(key[r["rating_id"]]["run_id"])
        if not s or s.get("human_only"):
            continue
        human.append(float(r["act_or_forward_1_5"]))
        auto.append((s["recall_confirmed"] or 0) - s["n_false_findings"])
    rho = spearman(human, auto)
    print(f"[ratings] n={len(human)} auto-scored reports · Spearman rho = {rho}")
    if rho is not None and rho < 0.3:
        print("[ratings] rho < 0.3: the automated metric misses what matters to a practitioner. "
              "Cam's judgment wins; revisit the scorer before day 6.")


def merge(out: Path, eval_dirs: list[Path]) -> None:
    """Concatenate several harness eval dirs into one: scores in invocation order,
    every run dir copied under out/runs/. Re-runnable: `out` is rebuilt."""
    scores, total = [], 0.0
    if out.exists():
        shutil.rmtree(out)
    (out / "runs").mkdir(parents=True)
    for d in eval_dirs:
        data = json.loads((d / "scores.json").read_text(encoding="utf-8"))
        scores += data["scores"]
        total += float(data.get("total_usd") or 0)
        for r in (d / "runs").iterdir():
            shutil.copytree(r, out / "runs" / r.name)
    (out / "scores.json").write_text(json.dumps({"scores": scores, "total_usd": round(total, 4),
                                                 "merged_from": [str(d) for d in eval_dirs]}, indent=2),
                                     encoding="utf-8")
    print(f"[ratings] merged {len(scores)} runs from {len(eval_dirs)} eval dirs -> {out} (${total:.2f})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["sheet", "agree", "merge"])
    ap.add_argument("eval_dir")
    ap.add_argument("more", nargs="*", help="merge: further eval dirs")
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args(argv)
    if a.command == "merge":
        merge(Path(a.eval_dir), [Path(x) for x in a.more])
    elif a.command == "sheet":
        if OUT.exists() and (OUT / "day3_key.csv").exists():
            shutil.copy(OUT / "day3_key.csv", OUT / "day3_key.prev.csv")
        sheet(Path(a.eval_dir), a.seed)
    else:
        agree(Path(a.eval_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
