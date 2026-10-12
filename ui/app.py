"""
TrendAnalyzer Pro -- Streamlit dashboard and single entry point of the system.

Presentation tier of a three-tier architecture:

    collectors/ + engine/     ingestion: plan, fetch, filter and score posts
    database/                 persistence: SQLite corpus and per-scan volume tallies
    ui/                       presentation: cached loaders, analytical views, controls

A search runs engine.scan() on a background thread (see scan.py); when it finishes
the caches are cleared and the page re-renders on the new corpus.

    app.py        bootstrap, page routing, topic banner and KPI row
    styles.py     theme colours and the global stylesheet
    utils.py      shared HTML, chart and formatting helpers
    data.py       memoised loaders and background services
    scan.py       scan callbacks, background ScanJob and its progress
    views/        homepage, search bar and one module per analytical tab
    components/   static front end of the wheel-click component

Must be launched through `python main.py`, which resets the stored corpus and sets
the TREND_APP_LAUNCHED flag checked below.
"""

import os
import streamlit as st

# Launch guard: running the script directly would display stale data.
if not os.getenv("TREND_APP_LAUNCHED"):
    st.error("TrendAnalyzer Pro must be started with `python main.py` from the project root.")
    st.stop()

import sys
import warnings
from html import escape as html_escape

warnings.filterwarnings("ignore", category=FutureWarning)
os.environ["HF_HUB_VERBOSITY"] = "error"

# Streamlit runs this file as a script, so the project root and ui/ must be on
# sys.path before any project import.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for _path in (PROJECT_ROOT, CURRENT_DIR):
    if _path not in sys.path:
        sys.path.append(_path)

# Must precede every other render call.
st.set_page_config(page_title="TrendAnalyzer Pro", page_icon="🧿",
                   layout="wide")

import engine
from database.schema import parse_keywords
from styles import inject_styles
from utils import kpi_grid, relative_time, shorten
from data import (fetch_balanced_trends, build_semantic_graph, load_topic_profile,
                  start_background_services)
from scan import cancel_search, finish_scan, go_home, render_scan_progress, \
    scan_locked, start_scan
from views.search import render_hero, render_search_bar, render_scan_feedback
from views.home import is_cold_start, render_cold_start, render_intelligence_brief, \
    render_footer
from views.wheel_tab import render_wheel_tab
from views.hype_tab import render_hype_tab
from views.connections_tab import render_connections_tab
from views.gatekeeper_tab import render_gatekeeper_tab

inject_styles()


def render_results(df, profile):
    """Renders the topic banner, KPI row and the four analytical tabs."""
    topic = (profile or {}).get('target_topic') or "your topic"

    last_collected = df['collected_at'].max() if 'collected_at' in df else None
    plan = "Keyword search"
    st.markdown(
        f"<div class='topic-banner'><span class='label'>Trends for</span>"
        f"<span class='topic'>{html_escape(topic)}</span>"
        f"<span class='meta'>Updated {relative_time(last_collected)}</span>"
        f"<span class='badge'>{plan}</span></div>",
        unsafe_allow_html=True,
    )

    # Computed once and shared by the KPI row and Connections tab, so they never disagree.
    try:
        _, default_metrics = build_semantic_graph(97.0, 25)
    except Exception as exc:                      # noqa: BLE001
        # E.g. the embedding model cannot load; the other tabs still render.
        st.warning(f"Connections are unavailable right now: {exc}", icon="⚠️")
        default_metrics = {}
    bridges = default_metrics.get('emerging_bridges', [])

    distinct_keywords = len({k for raw in df['keywords'].dropna() for k in parse_keywords(raw)})
    # Corpus is sorted by trend score.
    top = df.iloc[0]
    kpis = [
        ("Posts analysed", f"{len(df)}", "the most talked-about right now"),
        ("Communities", f"{df['source_platform'].nunique()}", "GitHub · HN · Dev.to · Mastodon"),
        ("Key themes", f"{distinct_keywords}", "distinct ideas mentioned"),
        ("Cross-community links", f"{len(bridges)}", "cross-community connections"),
        ("Hottest source", html_escape(str(top['source_platform'])),
         html_escape(shorten(top['title'], 44))),
    ]
    st.markdown(kpi_grid(kpis), unsafe_allow_html=True)

    tab_wheel, tab_hype, tab_links, tab_gate = st.tabs(
        ["🌀 Ecosystem Wheel", "⚖️ Hype vs. Reality", "🔗 Connections",
         "🛡️ Gatekeeper's Filter"]
    )

    with tab_wheel:
        render_wheel_tab(df, topic)

    with tab_hype:
        render_hype_tab(df)

    with tab_links:
        render_connections_tab(bridges, default_metrics)

    with tab_gate:
        render_gatekeeper_tab()


def main():
    """
    Renders one pass of the app, routed on `st.session_state.current_view`:
        home   -- hero, search bar and the live Intelligence Brief
        search -- search bar plus either the running scan's progress and Cancel, or
                  the results for the stored corpus
    A first launch shows only the blocking cold-start screen.
    """
    services = start_background_services()
    worker = services["worker"]
    # Cold start: nothing else renders until the first background results exist.
    if is_cold_start():
        render_cold_start(services)
        return
    # Collect a finished scan first, so the controls below draw unlocked this run.
    job = st.session_state.get("scan_job")
    if job is not None and not job.running:
        finish_scan(job)
        job = None
    view = st.session_state.setdefault("current_view", "home")

    if view == "home":
        render_hero()
        requested = render_search_bar()
        if requested is None:
            render_scan_feedback()
            render_intelligence_brief(worker)
            render_footer()
            return
        # Defensive: a submission always routes to the search view first.
        st.session_state["current_view"] = "search"
    else:
        # Locked during a scan: Cancel is the only live control while one runs.
        st.button("🏠 Back to Home", key="back_home", on_click=go_home,
                  disabled=scan_locked())
        requested = render_search_bar()

    if requested is not None and job is None:
        # Validated up front, so invalid input costs no network work.
        topic_ok, invalid = engine.validate_topic(requested)
        if invalid:
            st.session_state["scan_feedback"] = {"kind": "invalid", "message": invalid}
            st.session_state["scan_running"] = False
        else:
            start_scan(topic_ok)
        # Redraw: locked while the scan runs, or unlocked with invalid-topic feedback.
        st.rerun()
    if job is not None:
        # While the scan runs, only its progress and Cancel are shown.
        st.button("🛑 Cancel Search", key="cancel_search", on_click=cancel_search,
                  disabled=job.cancel.is_set())
        render_scan_progress()
        return
    if st.session_state.get("scan_running"):
        # Defensive: a lock set by a callback with no scan behind it.
        st.session_state["scan_running"] = False
        st.rerun()
    render_scan_feedback()

    df = fetch_balanced_trends()
    if df.empty:
        st.info("Search for a topic to see its full cross-platform analysis.", icon="🔍")
        return
    render_results(df, load_topic_profile())


if __name__ == "__main__":
    main()
