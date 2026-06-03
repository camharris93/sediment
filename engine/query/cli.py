"""Conversational NL->SQL CLI over the marts.

    python -m engine.query.cli "which animals live longest for their size?"
    python -m engine.query.cli            # interactive conversation

A real conversation: follow up ("now just mammals"), chain multi-step analysis,
and — in build mode — say "save that as mart_x" or "review mart_y" without leaving
the chat. Needs an Anthropic key; the deterministic core never calls it.
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from ..config import WAREHOUSE_PATH, active_dataset, has_anthropic_key, is_build_mode
from .agent import handle_message
from .conversation import Session
from .trace import TraceEvent

_BADGE = {"clean": "[ok] clean", "self_corrected": "[fixed] self-corrected",
          "flagged": "[!] flagged", "refused": "[x] refused", "failed": "[x] failed"}


def _printer():
    def on_event(ev: TraceEvent) -> None:
        k = ev.kind
        p = ev.payload
        if k == "plan_proposed":
            hops = p.get("hops", [])
            if len(hops) > 1:
                print(f"  plan: {len(hops)} hops")
                for i, h in enumerate(hops, 1):
                    print(f"    {i}. {h[:80]}")
        elif k == "hop_start":
            print(f"  -> hop {p.get('hop_index')}: {p.get('description','')[:70]}")
        elif k == "layer_result" and ev.layer and ev.layer.value == "L5" and p.get("pass"):
            print(f"     ran: {p.get('row_count',0)} rows")
        elif k == "validation_fail" and ev.layer:
            print(f"     [{ev.layer.value}] retrying...")
    return on_event


def _render(resp) -> None:
    if resp.kind == "answer":
        mh = resp.mh
        if resp.mh and resp.mh.is_multi:
            print("-" * 70)
        if mh and mh.sql:
            print("\nSQL:\n" + mh.sql)
        print("\nAnswer: " + resp.text)
        if mh and mh.rows:
            keys = list(mh.rows[0].keys())
            print(f"\nRows ({len(mh.rows)} shown):")
            print("  " + " | ".join(keys))
            for r in mh.rows[:10]:
                print("  " + " | ".join(str(r[k]) for k in keys))
        if mh:
            print(f"\nTrust: {_BADGE.get(mh.trust_badge.value, mh.trust_badge.value)}  |  "
                  f"{mh.attempts} attempt(s)")
    elif resp.kind == "built":
        print("\n" + resp.text)
        if resp.review and resp.review.findings:
            print("\nReview findings:")
            for f in resp.review.findings:
                print(f"  [{f.severity}] {f.title}: {f.detail}")
        elif resp.review:
            print("Review: " + resp.review.summary)
    elif resp.kind == "review":
        print("\n" + (resp.review.summary if resp.review else resp.text))
        for f in (resp.review.findings if resp.review else []):
            print(f"  [{f.severity}] {f.title}: {f.detail}")
    else:  # refused / error
        print("\n" + resp.text)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not WAREHOUSE_PATH.exists():
        print("No warehouse found. Run `python run.py up` first.")
        return 1
    if not has_anthropic_key():
        print("The query agent needs an Anthropic key. Set ANTHROPIC_API_KEY or add "
              "anthropic.txt at the repo root.")
        return 1

    ds = active_dataset()
    bm = is_build_mode()
    session = Session(dataset=ds)
    print(f"Conversation over dataset '{ds}'" + (" (build mode)" if bm else "") + ".")

    def ask(msg: str) -> None:
        print(f"\n> {msg}")
        resp = handle_message(session, msg, build_mode=bm, on_event=_printer())
        _render(resp)

    if argv:
        ask(" ".join(argv))
        return 0

    print("Ask a question; follow up naturally. Empty line or Ctrl-C to quit.")
    if bm:
        print("Build mode: try 'save that as mart_x' or 'review mart_y'.")
    try:
        while True:
            msg = input("\n> ").strip()
            if not msg:
                break
            resp = handle_message(session, msg, build_mode=bm, on_event=_printer())
            _render(resp)
    except (EOFError, KeyboardInterrupt):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
