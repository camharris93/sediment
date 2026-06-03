"""Conversational agent — the front door for the chat.

Routes each message to one of:
  • query   — answer a data question (multi-hop, with conversation memory)
  • build   — promote the last answer into a dbt model (preview / save / materialize),
              then auto-review the result
  • review  — critique an existing mart

So you can ask, follow up, then say "save that as mart_x" or "review mart_y" without
leaving the conversation. Build actions respect view/build governance; review is
read-only and always available.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .conversation import Session, run_turn
from .reviewer import ReviewResult, review_mart, review_sql


@dataclass
class ChatResponse:
    kind: str                      # answer | built | review | refused | error
    text: str = ""
    mh: Any = None                 # MultiHopResult (for kind=answer)
    review: ReviewResult | None = None
    model_path: str | None = None
    sandbox: Any = None            # SandboxResult (for preview)


_ROUTER_SYSTEM = """\
Classify the user's message in a data-analysis chat. Return a SINGLE JSON object:
{
  "action": "query | build | review",
  "model_name": "<mart_... if the user named one, else null>",
  "mode": "preview | save | build"
}

- "query": the user is asking a question about the data, or following up on a prior
  answer ("now just X", "break that down", "which of those..."). DEFAULT to this.
- "build": the user wants to turn the LAST answer into a saved model/mart
  ("save that as a model", "make that mart_x", "materialize it", "build it into marts").
  mode: "preview" (sandbox/try it), "save" (write the model for review — DEFAULT),
  "build" (run it into marts — only if they clearly say build/materialize/ship).
- "review": the user wants a critique of an existing mart ("review mart_x",
  "is mart_y sound?", "check that mart for fan-out").

Output ONLY the JSON.
"""


def _classify(message: str, has_last_answer: bool) -> dict:
    from .._llm import complete
    from ..config import get_ai_settings, has_anthropic_key
    if not has_anthropic_key():
        return {"action": "query"}
    try:
        text, _ = complete(system=_ROUTER_SYSTEM,
                           user=f"has_prior_answer={has_last_answer}\nmessage: {message}",
                           model=get_ai_settings().model_l7, max_tokens=200, cache_system=False)
        m = re.search(r"\{[\s\S]*\}", text)
        return json.loads(m.group(0)) if m else {"action": "query"}
    except Exception:
        return {"action": "query"}


def _auto_name(question: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (question or "").lower()).strip("_")[:40] or "model"
    return f"mart_{slug}"


def handle_message(session: Session, message: str, *, build_mode: bool,
                   on_event=None) -> ChatResponse:
    cls = _classify(message, has_last_answer=bool(session.turns))
    action = cls.get("action", "query")

    # ── REVIEW (read-only) ──────────────────────────────────────────────
    if action == "review" and cls.get("model_name"):
        rev = review_mart(session.dataset, cls["model_name"])
        return ChatResponse(kind="review", review=rev,
                            text=f"Review of `{cls['model_name']}`: {rev.summary}")

    # ── BUILD (governed) ────────────────────────────────────────────────
    if action == "build":
        last = next((t for t in reversed(session.turns)
                     if t.result.status in ("ok",) and t.result.sql), None)
        if last is None:
            return ChatResponse(kind="error",
                                text="Ask a question first — then I can turn that answer into a model.")
        if not build_mode:
            return ChatResponse(kind="refused",
                                text="Building models is disabled in view mode. Run the authoring "
                                     "app (`python run.py dashboard`) to build.")
        # Make the SQL self-contained: inline any conversational/hop synthetic
        # tables (turn_N_result / hop_N_result) it references, so the committed
        # model has no dangling refs.
        from .grounding import inline_synthetic_ctes
        ctx_syn = session.base_ctx().with_synthetic_tables(session.prior_synthetics())
        sql = inline_synthetic_ctes(last.result.sql, ctx_syn)
        sample = last.result.rows[:8]
        name = cls.get("model_name") or _auto_name(last.question)
        mode = cls.get("mode") or "save"
        from ..modeling import (BuildModeError, materialize_model, propose_model,
                                sandbox_build, validate_model_name)
        try:
            name = validate_model_name(name)
            # Always review the proposed SQL first.
            review = review_sql(name, sql, sample, session.base_ctx())
            if mode == "preview":
                r = sandbox_build(name, sql)
                txt = (f"Sandbox preview `{r.relation}`: {r.row_count:,} rows. "
                       if r.ok else f"Sandbox build failed: {r.error}")
                return ChatResponse(kind="built", text=txt, sandbox=r, review=review)
            if mode == "build":
                r = materialize_model(name, sql, dataset=session.dataset,
                                      question=last.question, rationale=last.question[:120])
                if r.ok:
                    session.refresh()  # new mart now visible to the chat
                    return ChatResponse(kind="built", review=review, model_path=r.model_path,
                                        text=f"Built `{session.dataset}_marts.{name}` and saved the model. "
                                             "Review notes below.")
                return ChatResponse(kind="error", text=f"dbt build failed: {r.error}")
            # default: save (propose) for review
            path = propose_model(name, sql, dataset=session.dataset,
                                 question=last.question, rationale=last.question[:120])
            return ChatResponse(kind="built", review=review, model_path=str(path),
                                text=f"Saved model `{name}` for review at {path.name}. Review notes below.")
        except BuildModeError as exc:
            return ChatResponse(kind="refused", text=str(exc))
        except Exception as exc:
            return ChatResponse(kind="error", text=f"Build failed: {exc}")

    # ── QUERY / follow-up (default) ─────────────────────────────────────
    mh = run_turn(session, message, on_event=on_event)
    return ChatResponse(kind="answer", mh=mh, text=mh.explanation)
