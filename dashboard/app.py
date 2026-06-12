"""sediment dashboard — Report + Ask (NL->SQL chat) + Build, over one local DuckDB
file that holds every dataset. A dataset SELECTOR scopes the view to one dataset's
marts/staging; the Report adapts (curated charts for AnAge, an auto-report for any
other dataset).

Governance: Ask is read-only at the engine level (safe for everyone). Editing the
report and building models are AUTHORING capabilities — refused server-side in the
default view mode, granted by `python run.py dashboard` (build mode).
"""
from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import registry
from engine.config import WAREHOUSE_PATH, active_dataset, app_mode, has_anthropic_key, is_build_mode

st.set_page_config(page_title="sediment", layout="wide")


def query_df(sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def table_exists(schema: str, table: str) -> bool:
    try:
        df = query_df(f"select 1 from information_schema.tables "
                      f"where table_schema='{schema}' and table_name='{table}'")
        return len(df) > 0
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def scoped_grounding(dataset: str):
    """Ground only the selected dataset's schemas (<dataset>_marts/_staging), so the
    NL→SQL agent only ever sees and queries that dataset."""
    from engine.query.grounding import build_grounding_context
    return build_grounding_context(dataset)


# ── Shared chart renderer — ChartSpec -> Altair ──────────────────────────────
def render_chart(df: pd.DataFrame, spec) -> bool:
    ct = spec.chart_type
    if ct == "none" or df.empty:
        return False
    try:
        if ct == "bar":
            ch = alt.Chart(df).mark_bar().encode(
                x=alt.X(f"{spec.x}:N", sort="-y", title=spec.x), y=alt.Y(f"{spec.y}:Q"),
                tooltip=list(df.columns)[:6])
        elif ct == "horizontal_bar":
            ch = alt.Chart(df).mark_bar().encode(
                x=alt.X(f"{spec.y}:Q"), y=alt.Y(f"{spec.x}:N", sort="-x", title=spec.x),
                tooltip=list(df.columns)[:6])
        elif ct == "line":
            series = spec.series or ([spec.y] if spec.y else [])
            if len(series) > 1:
                m = df.melt(id_vars=[spec.x], value_vars=series, var_name="series", value_name="value")
                ch = alt.Chart(m).mark_line(point=True).encode(
                    x=alt.X(f"{spec.x}:O", title=spec.x), y="value:Q", color="series:N")
            else:
                ch = alt.Chart(df).mark_line(point=True).encode(
                    x=alt.X(f"{spec.x}:O", title=spec.x), y=alt.Y(f"{series[0]}:Q"))
        elif ct == "scatter":
            xs = alt.Scale(type="log") if spec.log_x else alt.Undefined
            ys = alt.Scale(type="log") if spec.log_y else alt.Undefined
            enc = dict(x=alt.X(f"{spec.x}:Q", scale=xs, title=spec.x),
                       y=alt.Y(f"{spec.y}:Q", scale=ys, title=spec.y), tooltip=list(df.columns)[:6])
            if spec.color and spec.color in df.columns:
                enc["color"] = alt.Color(f"{spec.color}:N")
            ch = alt.Chart(df).mark_circle(opacity=0.5).encode(**enc)
        elif ct == "comparison_bar":
            series = [s for s in spec.series if s in df.columns]
            if not series:
                return False
            m = df.melt(id_vars=[spec.x], value_vars=series, var_name="metric", value_name="value")
            ch = alt.Chart(m).mark_bar().encode(
                x=alt.X(f"{spec.x}:N", title=spec.x), y="value:Q", color="metric:N", xOffset="metric:N")
        else:
            return False
        if spec.title:
            ch = ch.properties(title=spec.title)
        st.altair_chart(ch.properties(height=400).interactive(), use_container_width=True)
        return True
    except Exception as exc:
        st.caption(f"(couldn't render chart: {exc})")
        return False


# ── Live L1-L7 trace (sql-engine style) ──────────────────────────────────────
_LAYER_NAMES = {"L1": "Intent", "L2": "Generation", "L3": "Static valid.",
                "L4": "Dry-run (EXPLAIN)", "L5": "Execution", "L6": "Plausibility", "L7": "Translation"}


class LiveTrace:
    """Hop-aware live trace: shows the transparent L1→L7 pipeline as it runs. For a
    multi-hop question each hop gets its own group of layer blocks; the plan and
    hop boundaries are shown so you can see exactly what's happening under the hood."""
    def __init__(self, container):
        self.root = container
        self.n_hops = 1
        self.hop_box = None          # current hop's container
        self.cur: dict[tuple, object] = {}   # (hop_index, layer) -> st.status handle

    def __call__(self, ev) -> None:
        k, p = ev.kind, ev.payload
        layer = ev.layer.value if ev.layer else None
        hop = p.get("hop_index", 1)

        if k == "plan_proposed":
            hops = p.get("hops", [])
            self.n_hops = len(hops)
            if self.n_hops > 1:
                self.root.markdown(f"**🧭 Plan — {self.n_hops} hops**")
                for i, h in enumerate(hops, 1):
                    self.root.markdown(f"{i}. {h[:90]}")
            return
        if k == "hop_start":
            self.hop_box = self.root.container()
            if self.n_hops > 1:
                self.hop_box.markdown(f"**↳ Hop {hop}: {p.get('description','')[:80]}**")
            return
        if k == "synthesis":
            return
        if layer is None:
            return
        name = _LAYER_NAMES.get(layer, layer)
        box = self.hop_box or self.root
        if k == "layer_start":
            attempt = p.get("attempt")
            label = f"{layer} · {name}" + (f"   ↻ attempt {attempt}" if attempt and attempt > 1 else "")
            self.cur[(hop, layer)] = box.status(label, state="running", expanded=False)
        elif k in ("layer_result", "validation_fail"):
            self._finish(hop, layer, name, p, ok=(k == "layer_result"))

    def _finish(self, hop, layer, name, payload, ok) -> None:
        h = self.cur.get((hop, layer))
        if h is None:
            return
        try:
            if layer == "L1" and ok:
                it = payload.get("intent", {})
                if it.get("restated_question"):
                    h.markdown(f"**Restated:** {it['restated_question']}")
                if it.get("assumptions"):
                    h.markdown("**Assumptions:** " + "; ".join(it["assumptions"]))
            elif layer == "L2" and ok:
                h.code(payload.get("sql", ""), language="sql")
            elif layer == "L3":
                if ok:
                    h.markdown("Columns & tables resolve against the live schema.")
                else:
                    for v in payload.get("violations", []):
                        h.markdown(f"- `{v.get('offending_token','')}` — {v.get('message','')}")
            elif layer == "L4":
                h.markdown("DuckDB planned the query (EXPLAIN passed)." if ok
                           else f"EXPLAIN failed: {payload.get('error_summary','')}")
            elif layer == "L5" and ok:
                h.markdown(f"{payload.get('row_count',0)} rows · {payload.get('elapsed_ms',0)} ms")
            elif layer == "L6":
                if ok:
                    h.markdown("No fan-out or value anomalies.")
                else:
                    for w in payload.get("warnings", []):
                        h.markdown(f"- ⚠️ {w.get('message','')}")
            elif layer == "L7" and ok:
                h.markdown(f"Trust badge: **{payload.get('trust_badge','')}**")
        except Exception:
            pass
        icon = "✅" if ok else ("🔧" if layer in ("L3", "L4", "L6") else "⛔")
        h.update(label=f"{layer} · {name}   {icon}", state=("complete" if ok else "error"), expanded=not ok)


# ═════════════════════════════════════════════════════════════════════════════
# Header + dataset selector
# ═════════════════════════════════════════════════════════════════════════════
mode = app_mode()
left, mid, right = st.columns([3, 1.4, 1])
with left:
    st.title("🪨 sediment")
    st.caption("raw → staging → marts in one local DuckDB file, transformed by tested dbt SQL.")
with right:
    (st.success if mode == "build" else st.info)("🛠 build mode" if mode == "build" else "🔒 view mode")

datasets = registry.list_datasets()
if not WAREHOUSE_PATH.exists() or not datasets:
    st.warning("No built datasets found. Run **`python run.py up`** at the repo root first.")
    st.stop()
with mid:
    default = active_dataset() if active_dataset() in datasets else datasets[0]
    sel = st.selectbox("Dataset", datasets, index=datasets.index(default))

RAW_S, STG_S, MART_S = registry.raw_schema(sel), registry.staging_schema(sel), registry.marts_schema(sel)
models = registry.dataset_models(sel)
marts, staging = models["marts"], models["staging"]

tab_report, tab_ask, tab_build = st.tabs(["📊 Report", "💬 Ask", "🛠 Build a model"])
_BADGE = {"clean": "✅ clean", "self_corrected": "🔧 self-corrected",
          "flagged": "⚠️ flagged", "refused": "⛔ refused", "failed": "❌ failed"}


# ═════════════════════════════════════════════════════════════════════════════
# REPORT
# ═════════════════════════════════════════════════════════════════════════════
def render_anage_report():
    by_class = query_df(
        "select class, n_species, n_with_longevity, avg_longevity_yrs, max_longevity_yrs "
        "from anage_marts.mart_longevity_by_class where n_with_longevity >= 5 "
        "order by avg_longevity_yrs desc limit 20")
    st.subheader("Longevity by taxonomic class")
    st.altair_chart(alt.Chart(by_class).mark_bar().encode(
        x=alt.X("avg_longevity_yrs:Q", title="Avg max longevity (yrs)"),
        y=alt.Y("class:N", sort="-x", title=None),
        tooltip=["class", "n_species", "avg_longevity_yrs", "max_longevity_yrs"],
        color=alt.Color("avg_longevity_yrs:Q", legend=None, scale=alt.Scale(scheme="viridis")),
    ).properties(height=420), use_container_width=True)

    st.subheader("The size–lifespan law, and who defies it")
    sc = query_df("select common_name, class, adult_weight_g, max_longevity_yrs, "
                  "predicted_longevity_yrs, longevity_ratio, overperformer_rank from anage_marts.mart_aging_outliers")
    base = alt.Chart(sc)
    pts = base.mark_circle(opacity=0.45).encode(
        x=alt.X("adult_weight_g:Q", scale=alt.Scale(type="log"), title="Adult weight (g, log)"),
        y=alt.Y("max_longevity_yrs:Q", scale=alt.Scale(type="log"), title="Max longevity (yrs, log)"),
        color=alt.Color("class:N", legend=alt.Legend(title="Class", columns=2)),
        tooltip=["common_name", "class", "adult_weight_g", "max_longevity_yrs", "longevity_ratio"])
    trend = base.transform_regression("adult_weight_g", "max_longevity_yrs", method="pow").mark_line(
        color="black", strokeDash=[6, 4]).encode(x="adult_weight_g:Q", y="max_longevity_yrs:Q")
    st.altair_chart((pts + trend).interactive().properties(height=460), use_container_width=True)


def render_generic_report(marts: list[str]):
    """Auto-report for any dataset: a heuristic chart + table per mart (no LLM cost)."""
    from engine.charting import heuristic_chart
    if not marts:
        st.info(f"No marts built for **{sel}** yet. Curate one into `dbt_project/models/marts/` "
                f"(the scaffolder left proposals in `datasets/{sel}/scaffold/`), then `python run.py up {sel}`.")
        return
    for m in marts:
        st.subheader(m.replace("mart_", "").replace("_", " ").title())
        df = query_df(f"select * from {MART_S}.{m} limit 1000")
        spec = heuristic_chart(list(df.columns), df.head(15).to_dict("records"))
        if not render_chart(df, spec):
            st.dataframe(df.head(50), hide_index=True, use_container_width=True)
        else:
            with st.expander("data"):
                st.dataframe(df.head(50), hide_index=True, use_container_width=True)


with tab_report:
    # Generic header — works for any dataset.
    raw_tables = registry.dataset_tables(sel)
    total_rows = 0
    for t in raw_tables:
        if table_exists(RAW_S, t):
            total_rows += int(query_df(f'select count(*) c from {RAW_S}."{t}"').iloc[0]["c"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Source tables", len(raw_tables))
    c2.metric("Staging models", len(staging))
    c3.metric("Marts", len(marts))
    c4.metric("Rows (raw)", f"{total_rows:,}")
    st.divider()

    if sel == "anage" and {"mart_longevity_by_class", "mart_aging_outliers"} <= set(marts):
        render_anage_report()
    else:
        render_generic_report(marts)

    # Custom (AI-built) blocks — render for everyone, scoped to this dataset.
    from engine.report_config import delete_block, load_blocks, move_block
    blocks = load_blocks(sel)
    if blocks:
        st.divider()
        st.subheader("Custom charts")
    for b in blocks:
        st.markdown(f"#### {b.title}")
        try:
            df = query_df(b.sql)
            if not render_chart(df, b.chart_spec()):
                st.dataframe(df.head(50), hide_index=True, use_container_width=True)
        except Exception as exc:
            st.error(f"Block `{b.id}` query failed: {exc}")
        if is_build_mode():
            a, bb, cc, _ = st.columns([1, 1, 1, 6])
            if a.button("▲", key=f"up_{b.id}"):
                move_block(sel, b.id, -1); st.rerun()
            if bb.button("▼", key=f"dn_{b.id}"):
                move_block(sel, b.id, +1); st.rerun()
            if cc.button("🗑", key=f"del_{b.id}"):
                delete_block(sel, b.id); st.rerun()

    if is_build_mode():
        st.divider()
        with st.expander("✏️ Customize report — add an AI-built chart",
                         expanded=bool(st.session_state.get("report_seed"))):
            seed = st.session_state.get("report_seed", {})
            src = st.radio("Chart source", ["A mart", "Custom SQL"], horizontal=True,
                           index=1 if seed.get("sql") else 0, key="rep_src")
            if src == "A mart" and marts:
                tbl = st.selectbox("Mart", marts, key="rep_tbl")
                sql = f"select * from {MART_S}.{tbl} limit 1000"
            else:
                sql = st.text_area("SQL", value=seed.get("sql", f"select * from {MART_S}.{marts[0]} limit 500" if marts else ""),
                                   height=120, key="rep_sql")
            instr = st.text_input("What do you want to see? (optional)", value=seed.get("instruction", ""), key="rep_instr")
            title = st.text_input("Chart title", value=seed.get("title", ""), key="rep_title")
            if st.button("🤖 Suggest a chart (AI)", key="rep_suggest"):
                from engine.charting import suggest_chart
                try:
                    df = query_df(sql)
                    spec = suggest_chart(list(df.columns), df.head(15).to_dict("records"),
                                         instruction=instr, title_hint=title)
                    st.session_state.rep_preview = {"sql": sql, "spec": spec.to_dict(),
                                                    "title": title or (spec.title or "Untitled")}
                    st.caption(f"Suggested: **{spec.chart_type}** — {spec.reasoning or ''}")
                except Exception as exc:
                    st.error(str(exc))
            prev = st.session_state.get("rep_preview")
            if prev:
                from engine.charting import ChartSpec
                spec = ChartSpec.from_dict(prev["spec"])
                st.markdown("**Preview**")
                df = query_df(prev["sql"])
                if not render_chart(df, spec):
                    st.dataframe(df.head(30), hide_index=True, use_container_width=True)
                if st.button("➕ Add to report", type="primary", key="rep_add"):
                    from engine.report_config import add_block
                    add_block(sel, title=prev["title"], sql=prev["sql"], spec=spec)
                    st.session_state.pop("rep_preview", None)
                    st.session_state.pop("report_seed", None)
                    st.success("Added to this dataset's report.")
                    st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# ASK  (scoped to the selected dataset)
# ═════════════════════════════════════════════════════════════════════════════
def _render_assistant(item: dict) -> None:
    """Render one stored assistant turn in the conversation."""
    kind = item["kind"]
    if kind == "answer":
        if item.get("plan") and len(item["plan"]) > 1:
            with st.expander(f"🧭 Plan — {len(item['plan'])} hops", expanded=False):
                for i, h in enumerate(item["plan"], 1):
                    st.markdown(f"{i}. {h}")
        st.write(item.get("text", ""))
        if item.get("sql"):
            st.code(item["sql"], language="sql")
        for w in item.get("warnings", []):
            st.warning(w)
        if item.get("rows"):
            st.dataframe(pd.DataFrame(item["rows"]).head(50), hide_index=True, use_container_width=True)
        st.caption(f"Trust: {_BADGE.get(item.get('badge'), item.get('badge'))} · "
                   f"{item.get('attempts', 0)} attempt(s)"
                   + ("  ·  multi-hop" if item.get("plan") and len(item["plan"]) > 1 else ""))
    elif kind in ("built", "review", "refused", "error"):
        icon = {"built": "🛠", "review": "🔎", "refused": "🔒", "error": "⚠️"}.get(kind, "")
        st.markdown(f"{icon} {item.get('text','')}")
        for f in item.get("findings", []):
            sev_icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(f["severity"], "•")
            st.markdown(f"- {sev_icon} **{f['title']}** — {f['detail']}")


def _store_response(resp) -> dict:
    item = {"kind": resp.kind, "text": resp.text}
    if resp.kind == "answer" and resp.mh is not None:
        mh = resp.mh
        item.update({
            "sql": mh.sql, "rows": mh.rows, "badge": mh.trust_badge.value,
            "attempts": mh.attempts,
            "plan": [h.description for h in mh.plan.hops] if mh.plan else [],
            "warnings": [w["message"] for h in mh.hops
                         for w in (h.result.final_answer.plausibility_warnings if h.result.final_answer else [])],
        })
    if resp.review is not None:
        item["findings"] = [{"severity": f.severity, "title": f.title, "detail": f.detail}
                            for f in resp.review.findings]
        if resp.kind == "review" and resp.review.summary:
            item["text"] = resp.review.summary
    return item


with tab_ask:
    hint = ("Ask, follow up ('now just mammals'), or chain multi-step analysis. In build mode: "
            "'save that as mart_x', 'review mart_y'." if is_build_mode()
            else "Ask about the data in plain English; follow-ups work ('now just mammals').")
    st.caption(f"Conversation over **{sel}**. {hint}")
    if not has_anthropic_key():
        st.warning("The chat needs an Anthropic key. Set `ANTHROPIC_API_KEY` or add a one-line "
                   "`anthropic.txt` at the repo root, then reload.")
    elif not marts and not staging:
        st.info(f"Nothing to query for **{sel}** yet — build its models first.")
    else:
        from engine.query.agent import handle_message
        from engine.query.conversation import Session
        # One Session + display log per dataset (resets when you switch datasets).
        if st.session_state.get("conv_dataset") != sel:
            st.session_state.conv_dataset = sel
            st.session_state.conv_session = Session(dataset=sel)
            st.session_state.conv_log = []
        session = st.session_state.conv_session

        for turn in st.session_state.conv_log:
            with st.chat_message(turn["role"]):
                if turn["role"] == "user":
                    st.write(turn["text"])
                else:
                    _render_assistant(turn)

        msg = st.chat_input(f"ask about {sel}…")
        if msg:
            with st.chat_message("user"):
                st.write(msg)
            st.session_state.conv_log.append({"role": "user", "text": msg})
            with st.chat_message("assistant"):
                # The transparent pipeline — every layer of every hop, live.
                with st.expander("🔬 Under the hood (L1→L7 pipeline)", expanded=True):
                    trace_box = st.container()
                resp = handle_message(session, msg, build_mode=is_build_mode(),
                                      on_event=LiveTrace(trace_box))
                item = {"role": "assistant", **_store_response(resp)}
                _render_assistant(item)
                st.session_state.conv_log.append(item)

                # Make the answer available to the Build tab (self-contained SQL) and
                # the chart seeder.
                if resp.kind == "answer" and resp.mh and resp.mh.sql:
                    from engine.query.grounding import inline_synthetic_ctes
                    ctx_syn = session.base_ctx().with_synthetic_tables(session.prior_synthetics())
                    inlined = inline_synthetic_ctes(resp.mh.sql, ctx_syn)
                    st.session_state.last_answer = {"q": msg, "sql": inlined}
                    if is_build_mode() and st.button("📈 Chart this in the Report"):
                        st.session_state.report_seed = {"sql": inlined, "instruction": msg, "title": msg[:60]}
                        st.toast("Seeded Report → Customize. Open the 📊 Report tab.")


# ═════════════════════════════════════════════════════════════════════════════
# BUILD
# ═════════════════════════════════════════════════════════════════════════════
with tab_build:
    if not is_build_mode():
        st.subheader("🔒 Building is disabled in view mode")
        st.markdown("This is the read-only / shared deployment. Anyone can **Ask**, but turning "
                    "answers into models (or editing the report) is the authoring capability.\n\n"
                    "Run the authoring app locally to build:\n```bash\npython run.py dashboard\n```")
        st.stop()
    st.caption("Promote a validated chat answer into a dbt model: preview in a sandbox, save for "
               "review, or build straight into `marts`.")
    last = st.session_state.get("last_answer")
    if not last:
        st.info("Ask a question in the **💬 Ask** tab first.")
        st.stop()
    st.markdown(f"**From your question:** {last['q']}")
    sql = st.text_area("Model SQL (editable):", value=last["sql"], height=180)
    cc = st.columns([2, 3])
    name = cc[0].text_input("Model name", value="mart_")
    rationale = cc[1].text_input("Rationale (one line)", value=last["q"][:120])
    b1, b2, b3 = st.columns(3)
    if b1.button("🔬 Preview in sandbox", use_container_width=True):
        from engine.modeling import sandbox_build
        try:
            r = sandbox_build(name, sql)
            if not r.ok:
                st.error(f"Sandbox build failed: {r.error}")
            else:
                st.success(f"Built `{r.relation}` — {r.row_count:,} rows, {len(r.columns)} columns.")
                for w in r.warnings:
                    st.warning(w)
                if r.sample_rows:
                    st.dataframe(pd.DataFrame(r.sample_rows), hide_index=True, use_container_width=True)
        except Exception as exc:
            st.error(str(exc))
    if b2.button("💾 Save as dbt model (review)", use_container_width=True):
        from engine.modeling import propose_model
        try:
            path = propose_model(name, sql, dataset=sel, question=last["q"], rationale=rationale)
            st.success(f"Wrote {path.relative_to(REPO_ROOT)} — review, add tests, then commit.")
            st.code(path.read_text(encoding="utf-8"), language="sql")
        except Exception as exc:
            st.error(str(exc))
    if b3.button("🚀 Build into marts (dbt run)", use_container_width=True, type="primary"):
        from engine.modeling import materialize_model, validate_model_name
        try:
            with st.spinner("Writing model + dbt run…"):
                r = materialize_model(name, sql, dataset=sel, question=last["q"], rationale=rationale)
            if r.ok:
                st.success(f"Built into {MART_S}: {Path(r.model_path).name}. It's now a real, tested mart.")
                st.cache_resource.clear()
                st.session_state.report_seed = {
                    "sql": f"select * from {MART_S}.{validate_model_name(name)} limit 1000",
                    "instruction": last["q"], "title": last["q"][:60]}
                st.info("Open **📊 Report → Customize** to chart it (already seeded), or commit the model.")
                with st.expander("dbt output"):
                    st.code(r.dbt_output)
            else:
                st.error(r.error); st.code(r.dbt_output)
        except Exception as exc:
            st.error(str(exc))

st.divider()
st.caption("Deterministic core: every number traces to tested dbt SQL. AI lives only at the edges "
           "(scaffold / orchestrate / ask / build / chart) — never in the transform path.")
