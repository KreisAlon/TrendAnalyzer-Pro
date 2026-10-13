"""Hero header, search bar and scan feedback cards."""

from html import escape as html_escape

import streamlit as st

import engine
from graph_analyzer import PLATFORM_COLORS
from config import PLATFORMS as PLATFORM_ORDER
from data import load_topic_profile
from scan import lock_scan, scan_locked


def render_hero():
    """Renders the static page header with the four monitored platforms."""
    dots = "".join(
        f"<span><span class='dot' style='background:{PLATFORM_COLORS[p]}'></span>{p}</span>"
        for p in PLATFORM_ORDER
    )
    st.markdown(f"""
    <div class="hero">
        <div class="eyebrow">🧿 TrendAnalyzer Pro · Cross-platform trend analysis</div>
        <h1>Track how a topic moves across developer communities.</h1>
        <p>Four developer communities. One quality-gated view.</p>
        <div class="platform-row">{dots}</div>
    </div>
    """, unsafe_allow_html=True)


def render_search_bar():
    """
    Renders the search card.

    Returns:
        str | None: The topic to scan on this run (typed, or queued by a Deep Dive
        button), else None.
    """
    # Initialised once; afterwards Streamlit owns the value through the widget key.
    st.session_state.setdefault("scan_query", "")
    with st.container(key="search_card"):
        # A form batches input, so typing does not trigger a rerun per keystroke.
        with st.form("scan_form", border=False):
            c_input, c_button = st.columns([5, 1.25], vertical_alignment="center")
            # max_chars mirrors engine.MAX_TOPIC_CHARS so the UI and the validator agree.
            query = c_input.text_input(
                "Topic", key="scan_query", label_visibility="collapsed",
                placeholder="What do you want to track?  e.g. AI agents, Rust, "
                            "quantum computing…",
                max_chars=engine.MAX_TOPIC_CHARS, disabled=scan_locked(),
            )
            # Concurrency Control: disabled while a scan runs here or in any session.
            submitted = c_button.form_submit_button("🔍  Scan Trends", type="primary",
                                                    width="stretch",
                                                    on_click=lock_scan,
                                                    disabled=scan_locked())

    if submitted:
        return query
    return st.session_state.pop("pending_scan", None)


def render_scan_feedback():
    """Renders the one-shot feedback card (warning, invalid, insufficient, no results)."""
    feedback = st.session_state.pop("scan_feedback", None)
    if not feedback:
        return
    if feedback["kind"] == "warning":
        st.warning(feedback["message"], icon="⚠️")
        return
    # The engine leaves the previous corpus intact on an empty or rejected scan.
    previous = (load_topic_profile() or {}).get('target_topic')
    if feedback["kind"] == "invalid":
        keep = (f" Your previous results for **{previous}** are still shown below."
                if previous else "")
        st.info(f"{feedback['message']}{keep}", icon="💡")
        return
    if feedback["kind"] == "insufficient":
        found = ", ".join(f"{p} ({n})" for p, n in feedback.get("per_platform", {}).items()
                          if n) or "none"
        keep = (f" Your previous results for **{previous}** are still shown below."
                if previous else "")
        st.info(f"{feedback['message']} Communities with matching posts: {found}.{keep}",
                icon="💡")
        return
    keep = (f" Your previous results for <b>{html_escape(previous)}</b> are still "
            f"shown below." if previous else "")
    st.markdown(
        f"<div class='no-results'><div class='big'>🔍</div>"
        f"<div class='nr-title'>{html_escape(feedback['message'])}</div>"
        f"<div class='k-sub'>We couldn't find posts about "
        f"<b>{html_escape(feedback['topic'])}</b> on any of the four communities. "
        f"Check the spelling, or try a broader or more common term.{keep}</div></div>",
        unsafe_allow_html=True)

