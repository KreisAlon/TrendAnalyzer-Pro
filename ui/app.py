"""
TrendScope -- the dashboard, and the single entry point for the whole system.

Run with `streamlit run ui/app.py` (or `python main.py`). There is no background
collector to start: typing a topic into the search bar and pressing "Scan Trends"
calls engine.scan(), which plans the search, collects from all four platforms
concurrently, scores and stores the results, and the page refreshes onto them.
"""

import streamlit as st
import streamlit.components.v2 as components_v2
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import os
import sys
import json
import re
import textwrap
import warnings
from datetime import datetime
from html import escape as html_escape

warnings.filterwarnings("ignore", category=FutureWarning)
os.environ["HF_HUB_VERBOSITY"] = "error"

# NOTE: the dashboard never calls Gemini. The only model call in the system is the
# optional search planner inside engine.py, which falls back silently to the user's
# own search words; every explanation on this page is computed from the data.

# --- Dynamic Path Resolution ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for _path in (PROJECT_ROOT, CURRENT_DIR):
    if _path not in sys.path:
        sys.path.append(_path)

from graph_analyzer import (
    GraphBuilder, PLATFORM_COLORS as GRAPH_PLATFORM_COLORS,
    PLATFORMS as PLATFORM_ORDER, TEXT_PRIMARY, TEXT_SECONDARY, REGISTER_LABEL,
)
import engine
from config import TARGET_POSTS_PER_PLATFORM
from topics import TopicProfile
from database.manager import TrendManager

DB_PATH = os.path.join(PROJECT_ROOT, "trends_project.db")

st.set_page_config(page_title="TrendScope · Trend Intelligence", page_icon="🧿",
                   layout="wide", initial_sidebar_state="collapsed")

# ==========================================
# 🎨 THEME
# ==========================================
# UI chrome is deliberately monochrome slate. Every hue on the page belongs to the
# data -- the four platform colours below -- so a coloured button or banner can
# never be mistaken for "that's the GitHub one".
INK = "#0f172a"
INK_SOFT = "#1e293b"
MUTED = "#64748b"
LINE = "#e5e7eb"

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    .stApp {{ background: #f6f7fb; color: {INK};
             font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .block-container {{ padding-top: 1.6rem; padding-bottom: 4rem; max-width: 1360px; }}
    h1, h2, h3, h4, h5 {{ font-family: 'Inter', sans-serif; letter-spacing: -0.02em; color: {INK}; }}

    /* Hide the developer chrome: hamburger menu, deploy button, footer, top bar. */
    #MainMenu, footer, [data-testid="stDecoration"], [data-testid="stToolbar"] {{ display: none !important; }}
    header[data-testid="stHeader"] {{ background: transparent; height: 0; }}

    /* --- Hero ---------------------------------------------------------- */
    .hero {{
        background: radial-gradient(1200px 400px at 85% -20%, #334155 0%, transparent 60%),
                    linear-gradient(135deg, {INK} 0%, {INK_SOFT} 100%);
        border-radius: 22px; padding: 38px 44px 64px; color: #fff; position: relative;
        box-shadow: 0 20px 45px rgba(15, 23, 42, 0.18);
    }}
    .hero .eyebrow {{ font-size: 0.78rem; font-weight: 600; letter-spacing: 0.14em;
                      text-transform: uppercase; color: #94a3b8; }}
    .hero h1 {{ color: #fff; font-size: 2.7rem; font-weight: 800; margin: 6px 0 8px;
                line-height: 1.1; padding: 0; }}
    .hero p {{ color: #cbd5e1; font-size: 1.08rem; margin: 0; max-width: 720px; line-height: 1.55; }}
    .hero .platform-row {{ margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }}
    .hero .platform-row > span {{
        background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.14);
        border-radius: 999px; padding: 4px 12px; font-size: 0.82rem; color: #e2e8f0;
    }}
    .hero .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                  margin-right: 6px; vertical-align: 1px; }}

    /* --- Search card (floats over the bottom edge of the hero) ----------- */
    .st-key-search_card {{
        background: #fff; border: 1px solid {LINE}; border-radius: 18px;
        padding: 14px 16px; margin: -44px 28px 0; position: relative; z-index: 2;
        box-shadow: 0 14px 34px rgba(15, 23, 42, 0.10);
    }}
    .st-key-search_card [data-testid="stForm"] {{ border: none; padding: 0; }}
    .st-key-search_card [data-testid="stTextInput"] input {{
        font-size: 1.06rem; padding: 14px 16px; height: 54px;
    }}
    .st-key-search_card [data-testid="stTextInput"] > div > div {{
        border-radius: 12px; border: 1px solid {LINE}; background: #f8fafc;
    }}
    .st-key-search_card [data-testid="stFormSubmitButton"] button {{
        height: 56px; border-radius: 12px; font-size: 1.02rem; font-weight: 700;
    }}

    /* --- Buttons ------------------------------------------------------- */
    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"],
    [data-testid="stBaseLinkButton-primary"] {{
        background: {INK} !important; border: 1px solid {INK} !important; color: #fff !important;
        border-radius: 12px; font-weight: 600; transition: transform .12s ease, box-shadow .12s ease;
    }}
    [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover,
    [data-testid="stBaseLinkButton-primary"]:hover {{
        background: {INK_SOFT} !important; transform: translateY(-1px);
        box-shadow: 0 8px 18px rgba(15, 23, 42, 0.18);
    }}
    [data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"] {{
        border-radius: 12px; border: 1px solid {LINE}; background: #fff; font-weight: 500;
    }}
    .st-key-suggestions [data-testid="stBaseButton-secondary"] {{
        border-radius: 999px; padding: 2px 14px; min-height: 34px; font-size: 0.88rem;
        color: #334155;
    }}

    /* --- Topic banner & KPI cards --------------------------------------- */
    .topic-banner {{ display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
                     margin: 26px 2px 14px; }}
    .topic-banner .label {{ font-size: 0.8rem; font-weight: 600; letter-spacing: 0.08em;
                            text-transform: uppercase; color: {MUTED}; }}
    .topic-banner .topic {{ font-size: 1.7rem; font-weight: 800; letter-spacing: -0.02em; }}
    .topic-banner .meta {{ font-size: 0.9rem; color: {MUTED}; }}
    .badge {{ display: inline-block; background: #eef2f7; color: #334155; border-radius: 999px;
              padding: 3px 11px; font-size: 0.8rem; font-weight: 600; }}

    .kpi-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px;
                 margin-bottom: 22px; }}
    @media (max-width: 1000px) {{ .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    .kpi {{ background: #fff; border: 1px solid {LINE}; border-radius: 16px; padding: 18px 20px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }}
    .kpi .k-label {{ font-size: 0.76rem; font-weight: 600; letter-spacing: 0.06em;
                     text-transform: uppercase; color: {MUTED}; }}
    .kpi .k-value {{ font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; color: {INK};
                     margin-top: 6px; line-height: 1.1; }}
    .kpi .k-sub {{ font-size: 0.82rem; color: {MUTED}; margin-top: 4px; }}

    /* --- Tabs ---------------------------------------------------------- */
    [data-baseweb="tab-list"] {{ gap: 6px; background: #eceff4; padding: 6px; border-radius: 14px;
                                 width: fit-content; max-width: 100%; }}
    [data-baseweb="tab"] {{ border-radius: 10px; padding: 8px 18px !important; height: auto;
                            background: transparent; font-weight: 600; }}
    [data-baseweb="tab"][aria-selected="true"] {{ background: #fff;
                            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.12); }}
    [data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {{ display: none; }}
    [data-baseweb="tab-panel"] {{ padding-top: 18px; }}

    /* --- Cards --------------------------------------------------------- */
    .st-key-wheel_card, .st-key-inspector, [class*="st-key-insight_"], .st-key-panel_card {{
        background: #fff; border: 1px solid {LINE}; border-radius: 18px; padding: 20px 22px;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    .card-title {{ font-size: 1.1rem; font-weight: 700; margin: 0 0 2px; }}
    .card-sub {{ font-size: 0.9rem; color: {MUTED}; margin: 0 0 8px; }}

    .pill {{ display: inline-flex; align-items: center; gap: 7px; background: #f1f5f9;
             border-radius: 999px; padding: 4px 12px; font-size: 0.82rem; font-weight: 600;
             color: #334155; }}
    .pill .dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
    .post-title {{ font-size: 1.35rem; font-weight: 750; line-height: 1.3; margin: 12px 0 6px;
                   color: {INK}; letter-spacing: -0.015em; }}
    .post-meta {{ font-size: 0.86rem; color: {MUTED}; margin-bottom: 12px; }}
    .post-body {{ font-size: 0.95rem; line-height: 1.6; color: #334155; background: #f8fafc;
                  border-radius: 12px; padding: 14px 16px; white-space: pre-wrap;
                  word-wrap: break-word; }}
    .post-body.full {{ max-height: 380px; overflow-y: auto; }}
    .section-label {{ font-size: 0.76rem; font-weight: 700; letter-spacing: 0.07em;
                      text-transform: uppercase; color: {MUTED}; margin: 18px 0 8px; }}
    .reason {{ font-size: 0.94rem; line-height: 1.6; color: #1e293b; background: #f8fafc;
               border-left: 4px solid {INK}; border-radius: 10px; padding: 12px 14px; }}
    .term-chip {{ display: inline-block; background: #f1f5f9; border: 1px solid #e2e8f0;
                  border-radius: 999px; padding: 3px 11px; margin: 3px 5px 3px 0;
                  font-size: 0.82rem; color: #334155; }}
    .score-row {{ display: flex; justify-content: space-between; font-size: 0.86rem;
                  color: {MUTED}; margin-top: 4px; }}
    .score-track {{ height: 8px; background: #eef2f7; border-radius: 999px; overflow: hidden;
                    margin-top: 6px; }}
    .score-fill {{ height: 100%; border-radius: 999px; }}

    .empty-inspector {{ text-align: center; color: {MUTED}; padding: 40px 10px; }}
    .empty-inspector .big {{ font-size: 2.4rem; }}

    /* --- Insight cards ------------------------------------------------- */
    .pair-head {{ font-size: 1.15rem; font-weight: 750; margin: 0; }}
    .pair-sub {{ font-size: 0.93rem; color: #475569; line-height: 1.55; margin: 6px 0 12px; }}
    .mini-post {{ background: #f8fafc; border: 1px solid #eef2f7; border-radius: 14px;
                  padding: 14px 16px; min-height: 150px; }}
    .mini-post .t {{ font-weight: 650; font-size: 0.98rem; line-height: 1.4; margin-top: 8px;
                     color: {INK}; }}
    .pair-row {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
    .term-chip.shared {{ background: {INK}; border-color: {INK}; color: #fff; }}

    /* --- Connection evidence ------------------------------------------- */
    .ev-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    @media (max-width: 1000px) {{ .ev-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    .ev-tile {{ background: #f8fafc; border: 1px solid #eef2f7; border-radius: 14px;
                padding: 14px 16px; }}
    .ev-value {{ font-size: 1.5rem; font-weight: 800; letter-spacing: -0.02em; color: {INK};
                 margin: 4px 0 2px; }}
    .formula {{ margin-top: 14px; font-size: 0.85rem; color: {MUTED};
                font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                background: #f8fafc; border-radius: 10px; padding: 10px 12px; }}

    /* --- Hype vs. reality ---------------------------------------------- */
    .verdict {{ display: flex; gap: 18px; align-items: center; background: #fff;
                border: 1px solid {LINE}; border-radius: 18px; padding: 22px 24px;
                margin: 6px 0 18px; }}
    .verdict .v-icon {{ font-size: 2.6rem; }}
    .verdict .v-label {{ font-size: 2rem; font-weight: 800; letter-spacing: -0.03em;
                         color: {INK}; line-height: 1.15; }}
    .verdict .v-detail {{ color: #475569; font-size: 0.98rem; margin-top: 4px; }}
    .split-bar {{ display: flex; height: 22px; border-radius: 999px; overflow: hidden;
                  gap: 2px; background: #fff; }}
    .split-bar .seg {{ height: 100%; }}
    .split-legend {{ display: flex; justify-content: space-between; align-items: center;
                     margin: 8px 2px 18px; font-size: 0.92rem; }}
    .side-card {{ background: #fff; border: 1px solid {LINE}; border-radius: 18px;
                  padding: 20px 22px; height: 100%; }}
    .side-title {{ font-size: 1.1rem; font-weight: 750; margin: 12px 0 6px; }}
    .side-num {{ font-size: 1.6rem; font-weight: 800; letter-spacing: -0.02em; color: {INK}; }}
    .side-num span {{ font-size: 0.9rem; font-weight: 500; color: {MUTED}; letter-spacing: 0; }}
    .talk-row {{ display: flex; justify-content: space-between; align-items: center;
                 margin-top: 10px; }}
    .fill-row {{ display: flex; justify-content: space-between; font-size: 0.86rem;
                 color: {MUTED}; margin-top: 14px; }}

    /* --- No results ---------------------------------------------------- */
    .no-results {{ background: #fff; border: 1px solid {LINE}; border-radius: 18px;
                   padding: 30px; text-align: center; margin: 8px 0 10px; }}
    .no-results .big {{ font-size: 2.2rem; }}
    .no-results .nr-title {{ font-size: 1.3rem; font-weight: 800; margin: 6px 0 6px; }}

    /* --- Onboarding (empty state) --------------------------------------- */
    .steps {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px;
              margin-top: 30px; }}
    @media (max-width: 900px) {{ .steps {{ grid-template-columns: 1fr; }} }}
    .step {{ background: #fff; border: 1px solid {LINE}; border-radius: 18px; padding: 22px; }}
    .step .n {{ width: 32px; height: 32px; border-radius: 10px; background: {INK}; color: #fff;
                display: flex; align-items: center; justify-content: center; font-weight: 700; }}
    .step h4 {{ margin: 12px 0 4px; font-size: 1.05rem; }}
    .step p {{ margin: 0; color: {MUTED}; font-size: 0.93rem; line-height: 1.55; }}
</style>
""", unsafe_allow_html=True)

# Categorical palette, shared with graph_analyzer so the views cannot drift. These
# hues passed the palette validator at --pairs all (worst CVD dE 9.2); see
# graph_analyzer.PLATFORM_COLORS for the history.
PLATFORM_COLORS = dict(GRAPH_PLATFORM_COLORS)

# The sunburst's centre is a container, not a category, so it takes a neutral
# surface step rather than a hue.
ROOT_COLOR = "#eef1f5"

SUGGESTED_TOPICS = ["AI agents", "Rust", "Quantum computing", "Open-source LLMs",
                    "Cybersecurity", "Climate tech"]


def platform_pill(platform):
    """Platform badge: coloured dot for identity, the name in ink for reading."""
    color = PLATFORM_COLORS.get(platform, MUTED)
    return (f"<span class='pill'><span class='dot' style='background:{color}'></span>"
            f"{html_escape(str(platform))}</span>")


# ==========================================
# 📊 DATA ACCESS LAYER
# ==========================================

@st.cache_data(ttl=300)
def fetch_balanced_trends():
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Balanced representation
            df = pd.read_sql_query("""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY source_platform ORDER BY trend_score DESC) as rn
                    FROM unified_posts
                ) WHERE rn <= 25 ORDER BY trend_score DESC
            """, conn)
    except (sqlite3.Error, pd.errors.DatabaseError):
        # The table does not exist until the first scan creates it.
        return pd.DataFrame()

    # published_at is normalised to ISO8601 UTC by the collectors, so a single
    # parse works across all four platforms.
    df['published_dt'] = pd.to_datetime(df['published_at'], format='ISO8601',
                                        utc=True, errors='coerce')
    df['pub_date'] = df['published_dt'].dt.date
    return df


@st.cache_resource(show_spinner="Mapping how the posts connect…")
def build_semantic_graph(percentile, top_n):
    """
    Cached on (percentile, top_n). cache_resource rather than cache_data because a
    networkx Graph is a live object graph, not a serialisable value, and the encoder
    behind it is a process-wide singleton. Cleared after every scan.
    """
    builder = GraphBuilder(percentile=percentile, top_n_per_platform=top_n)
    return builder.build()


def explain_relevance(row, df, bridge_endpoints=None, link_count=None):
    """
    A short, plain-English reason this post is worth a reader's attention.

    Every clause is measured against the posts on screen rather than asserted, so
    the sentence changes when the data does. Deliberately not a model call: it is
    rendered for whichever post the reader clicks, and none of these facts need a
    model to state.

    Returns an HTML fragment: the caller places it inside a styled <div>, where
    markdown '**bold**' would render as literal asterisks.
    """
    platform = row['source_platform']
    score = float(row.get('trend_score') or 0.0)
    sentences = []

    # Ranked within its OWN platform: a GitHub star and a Mastodon favourite are
    # different units, which is also how trend_score is normalised upstream.
    peers = df[df['source_platform'] == platform]['trend_score'].dropna()
    if len(peers) > 1:
        top_share = max(1, round((peers >= score).mean() * 100))
        if top_share <= 50:
            sentences.append(f"It's in the <b>top {top_share}%</b> of the "
                             f"{len(peers)} {platform} posts we found on this topic.")
        else:
            sentences.append(f"It's getting moderate attention compared with the "
                             f"other {len(peers) - 1} {platform} posts we found.")

    total = df['trend_score'].sum()
    if total > 0:
        sentences.append(f"On its own it accounts for <b>{score / total * 100:.1f}%</b> "
                         f"of all the buzz we measured.")

    if bridge_endpoints and row.get('id') in bridge_endpoints:
        sentences.append("It's also one of today's <b>surprising connections</b>: it "
                         "links two communities that otherwise aren't talking about "
                         "this — see the Connections tab.")
    elif link_count:
        sentences.append(f"It's closely related to <b>{link_count}</b> other "
                         f"post{'s' if link_count != 1 else ''} in this scan.")

    return " ".join(sentences) or "Not enough posts yet to compare this one against."


@st.cache_data(ttl=300)
def load_topic_profile():
    """
    The topic the corpus on screen was collected under, if a scan has run.

    Read from disk rather than recomputed: the dashboard must not claim a topic the
    collectors were not actually pointed at. Returns None before the first scan.
    """
    profile = TopicProfile.load()
    return profile.to_dict() if profile else None


def safe_url(val):
    """The stored URL if it is a real web link, else ''."""
    if isinstance(val, pd.Series):
        val = val.iloc[0]
    text = str(val) if val is not None and pd.notna(val) else ''
    return text if text.startswith(('http://', 'https://')) else ''


def _relative_time(iso_text):
    """'just now' / '12 min ago' / '3 h ago' / '2 Oct' for a collected_at stamp."""
    try:
        then = datetime.fromisoformat(str(iso_text))
    except (TypeError, ValueError):
        return "—"
    now = datetime.now(then.tzinfo)
    minutes = (now - then).total_seconds() / 60
    if minutes < 1.5:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    if minutes < 24 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{then:%d %b}"


def _short(text, width):
    return textwrap.shorten(str(text or ''), width=width, placeholder="…") or "Untitled"


# ==========================================
# 🔎 SEARCH & SCAN
# ==========================================

def _queue_suggestion(topic):
    """Button callback: fill the search box and start a scan on this rerun."""
    st.session_state["scan_query"] = topic
    st.session_state["pending_scan"] = topic


def _reset_view_state():
    """Forgets per-corpus UI state, so a new topic does not inherit old selections."""
    for key in ("inspect_pick", "platform_filter", "briefed"):
        st.session_state.pop(key, None)


def render_hero():
    dots = "".join(
        f"<span><span class='dot' style='background:{PLATFORM_COLORS[p]}'></span>{p}</span>"
        for p in PLATFORM_ORDER
    )
    st.markdown(f"""
    <div class="hero">
        <div class="eyebrow">🧿 TrendScope · Trend intelligence</div>
        <h1>Spot what's rising before everyone else.</h1>
        <p>Search any topic and we'll scan four of the web's most influential tech
        communities, rank what's gaining momentum, and show you whether it's real
        or just hype.</p>
        <div class="platform-row">{dots}</div>
    </div>
    """, unsafe_allow_html=True)


def render_search_bar(show_suggestions):
    """The search card. Returns the submitted topic, or None."""
    st.session_state.setdefault("scan_query", "")
    with st.container(key="search_card"):
        with st.form("scan_form", border=False):
            c_input, c_button = st.columns([5, 1.25], vertical_alignment="center")
            query = c_input.text_input(
                "Topic", key="scan_query", label_visibility="collapsed",
                placeholder="What do you want to track?  e.g. AI agents, Rust, "
                            "quantum computing…",
                max_chars=engine.MAX_TOPIC_CHARS,
            )
            submitted = c_button.form_submit_button("🔍  Scan Trends", type="primary",
                                                    width="stretch",
                                                    disabled=engine.is_scanning())
        if show_suggestions:
            with st.container(key="suggestions", horizontal=True, gap="small"):
                st.caption("Popular:")
                for topic in SUGGESTED_TOPICS:
                    st.button(topic, key=f"suggest_{topic}", on_click=_queue_suggestion,
                              args=(topic,), disabled=engine.is_scanning())

    if submitted:
        return query
    return st.session_state.pop("pending_scan", None)


def run_scan(topic):
    """
    Runs one scan with a live progress panel, then refreshes the dashboard.

    engine.scan() runs the collectors' event loop right here on the script thread,
    so each progress callback below updates the status panel while the collectors
    are still working.
    """
    status = st.status(f"Scanning the web for **{topic}**…", expanded=True)

    def progress(stage, info):
        if stage == "loading":
            status.write("⚙️ Warming up the analysis engine…")
        elif stage == "planning":
            status.write("🧭 Working out the best places and phrases to search…")
        elif stage == "planned":
            how = ("an AI-planned search" if info['profile'].source == "gemini"
                   else "a direct search for your keywords")
            status.write(f"✅ Search plan ready — using {how}.")
        elif stage == "collecting":
            status.write("🚀 Reading GitHub, Hacker News, Dev.to and Mastodon in parallel. "
                         "This usually takes a minute or two…")
        elif stage == "platform_done":
            count = info['count']
            icon = "✅" if count else "⚪"
            vol = info.get('volume') or {}
            matching = vol.get('estimated_matches', count)
            more = (f" out of ≈{matching:,} matching" if matching > count else "")
            status.write(f"{icon} **{info['platform']}** — {count} "
                         f"post{'s' if count != 1 else ''} analysed{more}")
        elif stage == "scoring":
            status.write("📊 Ranking what's gaining momentum…")

    with st.spinner("TrendScope is on it…", show_time=True):
        result = engine.scan(topic, progress)

    if result.status == "no_results":
        # An answer, not a failure: nothing is stored and no empty charts are drawn.
        status.update(label="Scan finished — nothing matched", state="complete",
                      expanded=False)
        previous = (load_topic_profile() or {}).get('target_topic')
        keep = (f" Your previous results for <b>{html_escape(previous)}</b> are still "
                f"shown below." if previous else "")
        st.markdown(
            f"<div class='no-results'><div class='big'>🔍</div>"
            f"<div class='nr-title'>{html_escape(result.error)}</div>"
            f"<div class='k-sub'>We couldn't find posts about <b>{html_escape(topic)}</b> "
            f"on any of the four communities. Check the spelling, or try a broader or "
            f"more common term.{keep}</div></div>",
            unsafe_allow_html=True)
        return
    if result.error:
        status.update(label="The scan didn't finish", state="error", expanded=False)
        st.warning(result.error, icon="⚠️")
        return

    status.update(label=f"Scan complete — {result.fetched} posts in "
                        f"{result.duration_s:.0f}s", state="complete", expanded=False)

    # Everything downstream is cached on the old corpus; drop it all so the page
    # rebuilds on the new one.
    fetch_balanced_trends.clear()
    load_topic_profile.clear()
    build_semantic_graph.clear()
    load_scan_volume.clear()
    _reset_view_state()
    st.session_state["scan_notice"] = result.to_dict()
    st.rerun()


def render_onboarding():
    """Shown before the first scan, in place of an empty dashboard."""
    st.markdown("""
    <div class="steps">
        <div class="step"><div class="n">1</div><h4>Search a topic</h4>
            <p>Anything from a technology to an industry. Pick a suggestion above if you
            just want to look around.</p></div>
        <div class="step"><div class="n">2</div><h4>We scan the conversation</h4>
            <p>Code on GitHub, debate on Hacker News, tutorials on Dev.to and chatter on
            Mastodon — ranked by how much attention each post is getting.</p></div>
        <div class="step"><div class="n">3</div><h4>Get the takeaway</h4>
            <p>Explore the trend wheel, see whether it's hype or backed by code, and
            the evidence behind every surprising connection.</p></div>
    </div>
    """, unsafe_allow_html=True)


# ==========================================
# 🌀 ECOSYSTEM WHEEL
# ==========================================

def build_sunburst(df, topic):
    """
    Ring 1 is the platform, ring 2 is the post; segment angle is trend weight.

    Built with graph_objects rather than px.sunburst so every node carries a stable
    id ("post:<db id>", "platform:<name>", "root") and its own hover text. px keyed
    nodes by label, which merged posts with identical shortened titles and could
    only show those truncated labels on hover.
    """
    ids, labels, parents, values, colors, hover = [], [], [], [], [], []
    platforms = [p for p in PLATFORM_ORDER if p in set(df['source_platform'])]
    platforms += sorted(set(df['source_platform']) - set(platforms))
    total = df['trend_score'].sum() or 1.0

    ids.append("root")
    labels.append(html_escape(_short(topic, 22), quote=False))
    parents.append("")
    values.append(0)
    colors.append(ROOT_COLOR)
    hover.append(f"<b>{html_escape(str(topic), quote=False)}</b><br>{len(df)} posts across "
                 f"{len(platforms)} platforms<br><i>Click to zoom back out</i>")

    for platform in platforms:
        sub = df[df['source_platform'] == platform]
        color = PLATFORM_COLORS.get(platform, MUTED)
        ids.append(f"platform:{platform}")
        labels.append(platform)
        parents.append("root")
        values.append(0)
        colors.append(color)
        hover.append(f"<b>{platform}</b><br>{len(sub)} posts · "
                     f"{sub['trend_score'].sum() / total:.0%} of the buzz"
                     f"<br><i>Click to zoom in</i>")

        for _, r in sub.iterrows():
            title = str(r['title'] or 'Untitled').replace('\n', ' ')
            wrapped = "<br>".join(textwrap.wrap(html_escape(title, quote=False), 60)[:4])
            ids.append(f"post:{int(r['id'])}")
            labels.append(html_escape(_short(title, 28), quote=False))
            parents.append(f"platform:{platform}")
            # A floor so a near-zero score still gets a clickable sliver.
            values.append(max(float(r['trend_score'] or 0), 1.0))
            colors.append(color)
            hover.append(f"<b>{wrapped}</b><br>Platform: <b>{platform}</b>"
                         f"<br>Trend score: {float(r['trend_score'] or 0):.0f} / 100"
                         f"<br><i>Click to open in the inspector →</i>")

    fig = go.Figure(go.Sunburst(
        ids=ids, labels=labels, parents=parents, values=values,
        # 'remainder': containers carry no value of their own, so ring sizes come
        # purely from the posts and float rounding cannot hide a platform.
        branchvalues="remainder",
        marker=dict(colors=colors, line=dict(color="#ffffff", width=2)),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
        textinfo="label", insidetextorientation="radial",
        insidetextfont=dict(family="Inter, sans-serif"),
        sort=False,
    ))
    fig.update_layout(
        height=700, margin=dict(t=6, l=6, r=6, b=6),
        paper_bgcolor="#ffffff", uirevision="wheel",
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=LINE, align="left",
                        font=dict(color=TEXT_PRIMARY, size=13, family="Inter, sans-serif")),
    )
    return fig


# Plotly's sunburst reports clicks through `plotly_sunburstclick`, not through the
# selection events that st.plotly_chart(on_select=...) listens for -- so clicking a
# slice never reached Python and the inspector had to be driven by a dropdown. This
# tiny component attaches to the rendered wheel and forwards the clicked node's id.
_WHEEL_CLICK_JS = """
export default function(component) {
    const { setTriggerValue, data } = component;
    // The latest trigger is kept on window, so a handler bound by an earlier
    // mount always reports through the live component.
    window.__trendscopeWheelTrigger = setTriggerValue;

    const bind = () => {
        const gd = document.querySelector(data.selector + ' .js-plotly-plot');
        if (!gd || typeof gd.on !== 'function' || gd.__trendscopeBound) return;
        gd.__trendscopeBound = true;
        gd.on('plotly_sunburstclick', (event) => {
            const pt = event && event.points && event.points[0];
            if (!pt) return;
            const id = pt.id || (pt.data && pt.data.ids && pt.data.ids[pt.pointNumber]);
            if (!id) return;
            window.__trendscopeWheelTrigger('clicked', String(id));
            // A post is a leaf: select it, don't try to zoom into it.
            if (String(id).startsWith('post:')) return false;
        });
    };
    bind();
    // The chart is re-created on some reruns, so keep watching for a fresh one.
    const timer = setInterval(bind, 600);
    return () => clearInterval(timer);
}
"""

_wheel_click_listener = components_v2.component("trendscope_wheel_click",
                                                   js=_WHEEL_CLICK_JS)


def _apply_wheel_click(node_id, df_ids):
    """Turns a clicked wheel node into inspector state."""
    kind, _, value = str(node_id).partition(":")
    if kind == "post":
        try:
            post_id = int(value)
        except ValueError:
            return
        if post_id in df_ids:
            st.session_state["inspect_pick"] = post_id
    elif kind == "platform":
        # Clicking the platform again (it is now the centre) zooms back out.
        current = st.session_state.get("platform_filter")
        st.session_state["platform_filter"] = None if current == value else value
    elif kind == "root":
        st.session_state["platform_filter"] = None


def render_inspector(df, bridge_endpoints, graph_degrees):
    """The side panel: full details for the selected post."""
    with st.container(key="inspector"):
        st.markdown("<p class='card-title'>🔎 Trend Inspector</p>"
                    "<p class='card-sub'>Click any post on the wheel, or pick one "
                    "below.</p>", unsafe_allow_html=True)

        platform_filter = st.session_state.get("platform_filter")
        pool = df if not platform_filter else df[df['source_platform'] == platform_filter]
        options = [int(i) for i in pool['id']]
        titles = {int(r['id']): f"{r['source_platform']} · {_short(r['title'], 70)}"
                  for _, r in df.iterrows()}

        if not options:
            st.markdown("<div class='empty-inspector'><div class='big'>🌀</div>"
                        "No posts to show yet.</div>", unsafe_allow_html=True)
            return

        if platform_filter:
            c_note, c_clear = st.columns([3, 1], vertical_alignment="center")
            c_note.markdown(f"Showing {platform_pill(platform_filter)}",
                            unsafe_allow_html=True)
            if c_clear.button("Show all", key="clear_filter"):
                st.session_state["platform_filter"] = None
                st.rerun()

        # Widget state is set BEFORE the widget is created, which is the only point
        # Streamlit allows it -- this is what lets a wheel click drive the dropdown.
        if st.session_state.get("inspect_pick") not in options:
            st.session_state["inspect_pick"] = options[0]
        post_id = st.selectbox("Post", options, key="inspect_pick",
                               format_func=lambda i: titles.get(i, str(i)),
                               label_visibility="collapsed")

        row = df[df['id'] == post_id].iloc[0]
        platform = row['source_platform']
        color = PLATFORM_COLORS.get(platform, MUTED)
        score = float(row.get('trend_score') or 0)

        meta = []
        if row.get('author'):
            meta.append(f"by {html_escape(str(row['author']))}")
        if pd.notna(row.get('published_dt')):
            meta.append(f"{row['published_dt']:%d %b %Y}")

        st.markdown(
            f"{platform_pill(platform)}"
            f"<div class='post-title'>{html_escape(str(row['title']))}</div>"
            f"<div class='post-meta'>{' · '.join(meta)}</div>"
            f"<div class='score-row'><span>Trend score</span>"
            f"<b style='color:{INK}'>{score:.0f} / 100</b></div>"
            f"<div class='score-track'><div class='score-fill' "
            f"style='width:{max(min(score, 100), 2):.0f}%;background:{color}'></div></div>",
            unsafe_allow_html=True,
        )

        st.write("")
        url = safe_url(row.get('url'))
        if url:
            # The one filled control in the panel: reaching the original post is the
            # most common thing a reader wants from it.
            st.link_button(f"Open the original on {platform}  ↗", url,
                           type="primary", width="stretch")
        else:
            st.caption("No link was stored for this post.")

        st.markdown("<div class='section-label'>Why it's trending</div>"
                    f"<div class='reason'>{explain_relevance(row, df, bridge_endpoints, graph_degrees.get(row['id']))}</div>",
                    unsafe_allow_html=True)

        content = str(row.get('content') or '').strip()
        if content:
            # Strip the markdown emphasis READMEs and articles are full of; it would
            # otherwise show as literal asterisks in the preview.
            content = content.replace('**', '').replace('__', '')
            preview = _short(content, 420)
            st.markdown("<div class='section-label'>What it says</div>"
                        f"<div class='post-body'>{html_escape(preview)}</div>",
                        unsafe_allow_html=True)
            if len(content) > len(preview):
                with st.expander("Read the full text"):
                    st.markdown(f"<div class='post-body full'>{html_escape(content)}</div>",
                                unsafe_allow_html=True)

        try:
            keywords = json.loads(row.get('keywords') or '[]')
        except (json.JSONDecodeError, TypeError):
            keywords = []
        if keywords:
            chips = "".join(f"<span class='term-chip'>{html_escape(str(k))}</span>"
                            for k in keywords)
            st.markdown(f"<div class='section-label'>Key themes</div>{chips}",
                        unsafe_allow_html=True)


# ==========================================
# 🚀 CORE DASHBOARD INTERFACE
# ==========================================

def main():
    df = fetch_balanced_trends()
    profile = load_topic_profile() if not df.empty else None
    topic = (profile or {}).get('target_topic') or "your topic"

    render_hero()
    requested = render_search_bar(show_suggestions=df.empty)
    if requested is not None:
        # Validated BEFORE the scan starts, so bad input costs nothing.
        topic_ok, invalid = engine.validate_topic(requested)
        if invalid:
            st.warning(invalid, icon="⚠️")
        else:
            run_scan(topic_ok)

    notice = st.session_state.pop("scan_notice", None)
    if notice:
        found = ", ".join(f"{p} {n}" for p, n in notice['per_platform'].items())
        st.toast(f"Found {notice['fetched']} posts about “{notice['topic']}” ({found}).",
                 icon="✅")

    if df.empty:
        render_onboarding()
        return

    # --- What was this corpus collected for? ---
    last_collected = df['collected_at'].max() if 'collected_at' in df else None
    plan = ("AI-planned search" if (profile or {}).get('source') == 'gemini'
            else "Keyword search")
    st.markdown(
        f"<div class='topic-banner'><span class='label'>Trends for</span>"
        f"<span class='topic'>{html_escape(topic)}</span>"
        f"<span class='meta'>Updated {_relative_time(last_collected)}</span>"
        f"<span class='badge'>{plan}</span></div>",
        unsafe_allow_html=True,
    )

    # The bridge analysis is shared: the wheel cites it as a reason a post matters,
    # the KPI row counts it and the Connections tab lays out its evidence. Built once
    # so they cannot disagree.
    default_graph, default_metrics = build_semantic_graph(97.0, 25)
    bridges = default_metrics.get('emerging_bridges', [])
    bridge_endpoints = {nid for b in bridges for nid in (b['source'], b['target'])}
    graph_degrees = dict(default_graph.degree())

    # --- KPI cards: every tile is a measured quantity. ---
    distinct_keywords = len({
        k for raw in df['keywords'].dropna() for k in json.loads(raw or '[]')
    })
    top = df.iloc[0]
    kpis = [
        ("Posts analysed", f"{len(df)}", "the most talked-about right now"),
        ("Communities", f"{df['source_platform'].nunique()}", "GitHub · HN · Dev.to · Mastodon"),
        ("Key themes", f"{distinct_keywords}", "distinct ideas mentioned"),
        ("Surprising links", f"{len(bridges)}", "cross-community connections"),
        ("Hottest source", html_escape(top['source_platform']),
         html_escape(_short(top['title'], 44))),
    ]
    st.markdown(
        "<div class='kpi-grid'>" + "".join(
            f"<div class='kpi'><div class='k-label'>{label}</div>"
            f"<div class='k-value'>{value}</div><div class='k-sub'>{sub}</div></div>"
            for label, value, sub in kpis
        ) + "</div>",
        unsafe_allow_html=True,
    )

    tab_wheel, tab_hype, tab_concepts, tab_links = st.tabs(
        ["🌀 Ecosystem Wheel", "⚖️ Hype vs. Reality", "🏷️ Trending Concepts",
         "🔗 Connections"]
    )

    with tab_wheel:
        col_main, col_ins = st.columns([1.75, 1], gap="medium")

        with col_main:
            with st.container(key="wheel_card"):
                st.markdown(
                    "<p class='card-title'>Where the conversation is happening</p>"
                    "<p class='card-sub'>Inner ring: community · Outer ring: posts, sized "
                    "by how much attention they're getting. Hover for details, click to "
                    "inspect.</p>", unsafe_allow_html=True)
                st.plotly_chart(build_sunburst(df, topic), width='stretch',
                                key="ecosystem_wheel",
                                config={"displayModeBar": False})
            clicked = _wheel_click_listener(
                key="wheel_click", data={"selector": ".st-key-wheel_card"},
                on_clicked_change=lambda: None,
            )
            if clicked and clicked.get("clicked"):
                _apply_wheel_click(clicked["clicked"], {int(i) for i in df['id']})

        with col_ins:
            render_inspector(df, bridge_endpoints, graph_degrees)

    with tab_hype:
        render_hype_tab(df)

    with tab_concepts:
        render_concepts_tab(df, topic)

    with tab_links:
        render_connections_tab(bridges, default_metrics)


# ==========================================
# 🔗 CONNECTION EVIDENCE
# ==========================================
# Every connection is explained from the model's own measurements -- no language
# model call, so nothing here can hit a quota, fail, or say something the data
# does not support.

def _match_strength(similarity):
    """Plain-English band for a cosine similarity between two posts."""
    if similarity >= 0.70:
        return "Very strong"
    if similarity >= 0.55:
        return "Strong"
    return "Moderate"


def _evidence_tile(label, value, note):
    return (f"<div class='ev-tile'><div class='k-label'>{label}</div>"
            f"<div class='ev-value'>{value}</div><div class='k-sub'>{note}</div></div>")


def _keyword_chips(keywords, shared_terms):
    """A post's KeyBERT keywords, with the ones carrying a shared term emphasised."""
    shared = {t.lower() for t in shared_terms}
    chips = []
    for k in keywords:
        hit = any(tok in shared for tok in str(k).lower().split())
        cls = "term-chip shared" if hit else "term-chip"
        chips.append(f"<span class='{cls}'>{html_escape(str(k))}</span>")
    return "".join(chips) or "<span class='k-sub'>No keywords extracted</span>"


def render_connections_tab(bridges, metrics):
    st.markdown("### Surprising connections — and the evidence behind them")
    st.markdown(
        "<p class='card-sub'>Pairs of posts from <b>different communities</b> that are "
        "about the same idea, even though nothing else links them. That kind of quiet "
        "overlap is often the first sign an idea is spreading. Every number below comes "
        "straight from our model, so you can see exactly why each pair was flagged.</p>",
        unsafe_allow_html=True)

    if not bridges:
        st.info("No cross-community connections stood out in this scan. Try a broader "
                "topic, or scan again later as the conversation grows.", icon="🔍")
        return

    link_cutoff = metrics.get('thresholds', {}).get('cross', 0)

    for i, b in enumerate(bridges):
        with st.container(key=f"insight_{i}"):
            sim = b['similarity']
            st.markdown(
                f"<div class='pair-row'><p class='pair-head'>"
                f"{platform_pill(b['source_platform'])} &nbsp;+&nbsp; "
                f"{platform_pill(b['target_platform'])}</p>"
                f"<span class='badge'>{_match_strength(sim)} match</span></div>"
                f"<p class='pair-sub'>{html_escape(b['meaning'])}</p>",
                unsafe_allow_html=True)

            c1, c2 = st.columns(2, gap="medium")
            for col, side in ((c1, 'source'), (c2, 'target')):
                with col:
                    plat = b[f'{side}_platform']
                    st.markdown(
                        f"<div class='mini-post'>{platform_pill(plat)}"
                        f"<div class='t'>{html_escape(_short(b[f'{side}_title'], 120))}</div>"
                        f"<div class='section-label'>Extracted keywords</div>"
                        f"{_keyword_chips(b[f'{side}_keywords'], b['shared_terms'])}"
                        f"</div>", unsafe_allow_html=True)
                    url = safe_url(b[f'{side}_url'])
                    if url:
                        st.link_button(f"Open on {plat}  ↗", url, width='stretch')

            reg_a = REGISTER_LABEL.get(b['source_platform'], b['source_platform'])
            reg_b = REGISTER_LABEL.get(b['target_platform'], b['target_platform'])
            shared = "".join(f"<span class='term-chip shared'>{html_escape(str(t))}</span>"
                             for t in b['shared_terms'])
            tiles = "".join([
                _evidence_tile("Content similarity", f"{sim * 100:.0f}%",
                               f"{_match_strength(sim)} — a link needs "
                               f"{link_cutoff * 100:.0f}%+"),
                _evidence_tile("Shared neighbours", f"{b['neighbour_overlap'] * 100:.0f}%",
                               "0% means this pair is the only bridge between them"),
                _evidence_tile("Structural gap", f"{b['register_gap']:.2f} / 1.00",
                               f"{html_escape(reg_a)} ↔ {html_escape(reg_b)}"),
                _evidence_tile("Topic clusters",
                               "Different" if b['cross_community'] else "Same",
                               "separate corners of the conversation"
                               if b['cross_community'] else "same corner of the conversation"),
            ])
            bonus = 1.25 if b['cross_community'] else 1.0
            st.markdown(
                "<div class='section-label'>Connection evidence</div>"
                f"<div class='ev-grid'>{tiles}</div>"
                "<div class='section-label'>Shared keywords</div>"
                + (shared or "<span class='k-sub'>No distinctive keyword in common — "
                              "matched on overall meaning only.</span>")
                + f"<div class='formula'>Bridge score <b>{b['score']:.3f}</b> = similarity "
                  f"{sim:.2f} × (1 − shared neighbours {b['neighbour_overlap']:.2f}) × "
                  f"structural gap {b['register_gap']:.2f} × cluster bonus {bonus:.2f}</div>",
                unsafe_allow_html=True)


# ==========================================
# ⚖️ HYPE VS. REALITY
# ==========================================
# Code lives on GitHub; the other three communities are talk. The comparison uses
# each platform's ESTIMATED MATCHING VOLUME from the latest scan (scan_volume): the
# collectors store at most TARGET_POSTS_PER_PLATFORM posts each, so the stored rows
# alone would show a 25-vs-75 split for every popular topic. The estimate is the
# platform's own match count, corrected for overlap between search terms and for
# the share of checked posts that passed the relevance and quality gate.
CODE_PLATFORMS = ("GitHub",)
TALK_PLATFORMS = ("Hacker News", "Dev.to", "Mastodon")

# Below this many matching items across all four platforms, neither side is large
# enough for its share to mean anything.
EARLY_SIGNAL_FLOOR = 30


@st.cache_data(ttl=300)
def load_scan_volume():
    """Per-platform volume from the latest scan, or {} for data from older scans."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        return TrendManager().get_volume()
    except sqlite3.Error:
        return {}


def hype_vs_reality(df, volume):
    """
    The code-vs-talk comparison. Uses the measured volume when the latest scan
    recorded one, and otherwise falls back to stored post counts (flagged, since
    those are capped and cannot show the real ratio).
    """
    measured = bool(volume) and all(p in volume for p in CODE_PLATFORMS + TALK_PLATFORMS)
    if measured:
        counts = {p: int(volume[p]['estimated_matches']) for p in volume}
        capped = {p for p, v in volume.items() if v.get('capped')}
    else:
        counts = df['source_platform'].value_counts().to_dict()
        capped = set()

    n_code = int(sum(counts.get(p, 0) for p in CODE_PLATFORMS))
    talk_counts = {p: int(counts.get(p, 0)) for p in TALK_PLATFORMS}
    n_talk = sum(talk_counts.values())
    total = n_code + n_talk
    code_share = n_code / total if total else 0.5

    if total < EARLY_SIGNAL_FLOOR:
        verdict, icon = "Early Signal", "🌱"
        detail = ("There's very little activity anywhere yet — it's too early to tell "
                  "whether this is hype or real.")
    elif code_share >= 0.6:
        verdict, icon = "Backed by Code", "🛠️"
        detail = ("Far more working code than conversation: builders are ahead of the "
                  "buzz, so the attention may still be coming.")
    elif code_share <= 0.3:
        verdict, icon = "All Hype", "📣"
        detail = ("Lots of people are talking about it, but there's comparatively "
                  "little actively maintained code behind it.")
    else:
        verdict, icon = "Balanced", "⚖️"
        detail = ("Code and conversation are growing together — real building, with the "
                  "attention to match.")

    code_df = df[df['source_platform'].isin(CODE_PLATFORMS)]
    top_repo = code_df.sort_values('raw_score', ascending=False).head(1)
    return {
        'verdict': verdict, 'icon': icon, 'detail': detail, 'measured': measured,
        'n_code': n_code, 'n_talk': n_talk, 'talk_counts': talk_counts,
        'code_share': code_share, 'capped': capped, 'volume': volume if measured else {},
        'stars': float(code_df['raw_score'].fillna(0).sum()),
        'engagement': float(df[df['source_platform'].isin(TALK_PLATFORMS)]['raw_score']
                            .fillna(0).sum()),
        'top_repo': top_repo.iloc[0] if not top_repo.empty else None,
    }


def _compact(n):
    n = float(n or 0)
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def _approx(n, is_capped=False):
    """'≈3.4k', or '1.0k+' when the platform's count hit its reporting ceiling."""
    return f"{_compact(n)}+" if is_capped else f"≈{_compact(n)}"


def _volume_line(v):
    """How one platform's estimate was derived, in one short line."""
    return (f"{_compact(v['searched_total'])}{'+' if v.get('capped') else ''} found · "
            f"{v['overlap']:.0%} unique · {v['acceptance_rate']:.0%} passed our filter")


def render_hype_tab(df):
    h = hype_vs_reality(df, load_scan_volume())
    st.markdown("### Hype vs. Reality")
    st.markdown(
        "<p class='card-sub'>Is anyone actually <b>building</b> this, or just "
        "<b>talking</b> about it? We compare how much working code exists on GitHub with "
        "how much discussion there is on Hacker News, Dev.to and Mastodon.</p>",
        unsafe_allow_html=True)

    if not h['measured']:
        st.info("These results come from an older scan that didn't measure total volume, "
                "so the split below only counts the posts we stored (up to "
                f"{TARGET_POSTS_PER_PLATFORM} per community). **Scan this topic again** "
                "to see the real ratio.", icon="🔄")

    if h['n_talk']:
        per_ten = h['n_code'] / h['n_talk'] * 10
        ratio = (f"{per_ten:,.0f} active code project{'s' if round(per_ten) != 1 else ''} "
                 f"for every 10 discussions" if per_ten >= 1 else
                 f"1 active code project for every {h['n_talk'] / max(h['n_code'], 1):,.0f} "
                 f"discussions")
    else:
        ratio = "No discussion found yet — only code."

    # The split bar: code on the left in GitHub's hue, talk on the right split by
    # community, so each segment keeps the colour it has everywhere else.
    code_w = h['code_share'] * 100
    talk_total = h['n_talk'] or 1
    segments = [f"<div class='seg' title='GitHub · {h['n_code']:,}' "
                f"style='width:{code_w:.1f}%;background:{PLATFORM_COLORS['GitHub']}'></div>"]
    for p in TALK_PLATFORMS:
        w = (100 - code_w) * h['talk_counts'][p] / talk_total
        if w > 0:
            segments.append(f"<div class='seg' title='{p} · {h['talk_counts'][p]:,}' "
                            f"style='width:{w:.1f}%;background:{PLATFORM_COLORS[p]}'></div>")

    st.markdown(f"""
    <div class='verdict'>
        <div class='v-icon'>{h['icon']}</div>
        <div><div class='k-label'>Verdict</div>
             <div class='v-label'>{h['verdict']}</div>
             <div class='v-detail'>{h['detail']}</div></div>
    </div>
    <div class='split-bar'>{''.join(segments)}</div>
    <div class='split-legend'>
        <span>🛠️ <b>Code {code_w:.0f}%</b></span>
        <span class='k-sub'>{ratio}</span>
        <span>💬 <b>Talk {100 - code_w:.0f}%</b></span>
    </div>
    """, unsafe_allow_html=True)

    vol = h['volume']
    unit = "matching" if h['measured'] else "stored"
    c_code, c_talk = st.columns(2, gap="medium")
    with c_code:
        top = h['top_repo']
        top_line = (f"Most-starred: <b>{html_escape(_short(top['title'], 50))}</b> "
                    f"(⭐ {_compact(top['raw_score'])})" if top is not None else "—")
        how = (f"<div class='k-sub'>{_volume_line(vol['GitHub'])}</div>"
               if 'GitHub' in vol else "")
        count = (_approx(h['n_code'], 'GitHub' in h['capped']) if h['measured']
                 else f"{h['n_code']}")
        st.markdown(f"""
        <div class='side-card'>{platform_pill('GitHub')}
            <div class='side-title'>🛠️ Code</div>
            <div class='side-num'>{count} <span>active projects {unit}</span></div>
            {how}
            <div class='side-num' style='margin-top:10px'>⭐ {_compact(h['stars'])} <span>stars across the top {len(df[df['source_platform'] == 'GitHub'])}</span></div>
            <div class='k-sub'>{top_line}</div>
        </div>""", unsafe_allow_html=True)
    with c_talk:
        rows = []
        for p in TALK_PLATFORMS:
            n = h['talk_counts'][p]
            shown = _approx(n, p in h['capped']) if h['measured'] else f"{n}"
            detail = (f"<div class='k-sub'>{_volume_line(vol[p])}</div>" if p in vol else "")
            rows.append(f"<div class='talk-row'>{platform_pill(p)}<b>{shown}</b></div>{detail}")
        total = (_approx(h['n_talk'], bool(h['capped'] & set(TALK_PLATFORMS)))
                 if h['measured'] else f"{h['n_talk']}")
        st.markdown(f"""
        <div class='side-card'>
            <div class='side-title' style='margin-top:0'>💬 Talk</div>
            <div class='side-num'>{total} <span>discussions {unit}</span></div>
            {''.join(rows)}
            <div class='side-num' style='margin-top:10px'>{_compact(h['engagement'])} <span>points, reactions & replies on the top posts</span></div>
        </div>""", unsafe_allow_html=True)

    st.caption(
        "How it's measured: each platform tells us how many posts match the search — "
        "GitHub repositories updated in the last 14 days; Hacker News stories, Dev.to "
        "articles and Mastodon posts from the last 7 days. We remove double-counting "
        "between search terms, then keep only the share that passed our relevance and "
        f"quality filter on the posts we checked. We analyse the top "
        f"{TARGET_POSTS_PER_PLATFORM} from each community in depth. “+” means the "
        f"platform's count hit its reporting limit, so the true number is higher.")


# ==========================================
# 🏷️ TRENDING CONCEPTS
# ==========================================
# Built from the keywords KeyBERT extracted for each post. Whole keyphrases almost
# never repeat between posts (MMR diversification makes them document-specific), so
# they are split into single terms and counted once per post.
_CONCEPT_STOPWORDS = frozenset("""
a an the and or but if then than that this these those with without from into of to
in on at by for as is are was were be been being it its we you your our their they i
my not no so can will just what which who when where why how all any both each more
most other some new using use used make made get via about also one two first
based provides allows build building built way thing things lot need needs want like
""".split())


def trending_concepts(df, topic, top_n=15):
    """Top terms by number of posts mentioning them, excluding the search words."""
    topic_words = set()
    for w in re.findall(r'[a-z0-9]+', str(topic).lower()):
        topic_words |= {w, w.rstrip('s'), w + 's'}

    posts_by_term, platforms_by_term = {}, {}
    for _, row in df.iterrows():
        try:
            keywords = json.loads(row.get('keywords') or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        terms = set()
        for phrase in keywords:
            for tok in re.split(r'[^a-z0-9+#.]+', str(phrase).lower()):
                tok = tok.strip('.')
                if (len(tok) >= 2 and not tok.isdigit() and tok not in _CONCEPT_STOPWORDS
                        and tok not in topic_words):
                    terms.add(tok)
        for t in terms:
            posts_by_term.setdefault(t, set()).add(row['id'])
            platforms_by_term.setdefault(t, {}).setdefault(row['source_platform'], set()).add(row['id'])

    # Fold plurals into the singular ('agents' -> 'agent') when both occur.
    for t in [t for t in posts_by_term if t.endswith('s') and t[:-1] in posts_by_term]:
        posts_by_term[t[:-1]] |= posts_by_term.pop(t)
        for plat, ids in platforms_by_term.pop(t).items():
            platforms_by_term[t[:-1]].setdefault(plat, set()).update(ids)

    ranked = sorted(posts_by_term,
                    key=lambda t: (len(posts_by_term[t]), len(platforms_by_term[t]), t),
                    reverse=True)
    return [{'term': t, 'posts': len(posts_by_term[t]),
             'platforms': {p: len(ids) for p, ids in platforms_by_term[t].items()}}
            for t in ranked[:top_n] if len(posts_by_term[t]) >= 2]


def build_concepts_figure(concepts):
    """Horizontal bars, one neutral hue: a single series needs no legend."""
    concepts = list(reversed(concepts))   # largest at the top
    terms = [c['term'] for c in concepts]
    counts = [c['posts'] for c in concepts]
    # Leading spaces: 'outside' text is anchored at the bar end and otherwise
    # collides with the rounded corner.
    labels = [f"   {c['posts']} posts · {len(c['platforms'])} "
              f"communit{'y' if len(c['platforms']) == 1 else 'ies'}" for c in concepts]
    hover = ["<b>" + html_escape(c['term'], quote=False) + "</b><br>" + "<br>".join(
                 f"{p}: {n}" for p, n in sorted(c['platforms'].items(), key=lambda x: -x[1]))
             for c in concepts]
    fig = go.Figure(go.Bar(
        x=counts, y=terms, orientation='h', marker=dict(color="#334155"),
        text=labels, textposition='outside', cliponaxis=False,
        textfont=dict(color=TEXT_SECONDARY, size=12),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_layout(
        height=max(260, 34 * len(concepts) + 40), barcornerradius=4, bargap=0.35,
        margin=dict(t=10, l=10, r=150, b=10),
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        font=dict(family="Inter, sans-serif", color=TEXT_PRIMARY),
        xaxis=dict(visible=False, rangemode='tozero'),
        yaxis=dict(tickfont=dict(size=13, color=TEXT_PRIMARY), automargin=True),
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=LINE,
                        font=dict(color=TEXT_PRIMARY, size=12)),
    )
    return fig


def render_concepts_tab(df, topic):
    st.markdown("### Trending Concepts")
    st.markdown(
        "<p class='card-sub'>The ideas that come up most across everything we found, "
        "pulled from each post by our on-device keyword model. Your own search words "
        "are left out, so this shows what's <b>around</b> the topic.</p>",
        unsafe_allow_html=True)

    concepts = trending_concepts(df, topic)
    if not concepts:
        st.info("No concept came up in more than one post yet — the conversation is "
                "still very fragmented.", icon="🏷️")
        return

    with st.container(key="panel_card"):
        st.plotly_chart(build_concepts_figure(concepts), width='stretch',
                        key="concepts_chart", config={"displayModeBar": False})
        widest = max(concepts, key=lambda c: (len(c['platforms']), c['posts']))
        st.caption(f"Most widespread: **{widest['term']}**, mentioned in "
                   f"{len(widest['platforms'])} of 4 communities. Hover a bar for the "
                   f"per-community breakdown.")


if __name__ == "__main__":
    main()
