"""
Scan control: on_click callbacks that lock and route the UI, the background ScanJob,
its live progress fragment and the collection of its result.
"""

import threading
import time

import streamlit as st

import engine
from config import MANUAL_MAX_POSTS
from data import (fetch_balanced_trends, load_topic_profile, build_semantic_graph,
                  load_scan_volume)


def queue_scan(topic):
    """
    Queues a scan of `topic` from a button callback (Deep Dive). Only callbacks may
    write widget-bound state; `pending_scan` is consumed by render_search_bar().
    """
    st.session_state["scan_query"] = topic
    st.session_state["pending_scan"] = topic
    lock_scan()


def lock_scan():
    """
    on_click callback of every scan trigger: locks the UI and routes to the search
    view. Done in a callback so controls render disabled on the run that starts the scan.
    """
    st.session_state["scan_running"] = True
    st.session_state["current_view"] = "search"


def go_home():
    """on_click callback of "Back to Home": clears the query and shows the homepage."""
    st.session_state["current_view"] = "home"
    st.session_state["scan_query"] = ""
    st.session_state.pop("pending_scan", None)


def cancel_search():
    """
    on_click callback of "Cancel Search": sets the scan's cancel event, checked by the
    engine every tick; finish_scan() routes home once the thread stops.
    """
    job = st.session_state.get("scan_job")
    if job is not None:
        job.cancel.set()


def scan_locked():
    """
    True while this session has a scan running or any session holds the engine's
    scan lock; the `disabled` state of every control except Cancel.
    """
    job = st.session_state.get("scan_job")
    return bool((job is not None and job.running) or st.session_state.get("scan_running")
                or engine.is_scanning())


def reset_view_state():
    """Clears per-corpus widget state, so a new topic never inherits stale selections."""
    for key in ("inspect_pick", "platform_filter"):
        st.session_state.pop(key, None)


class ScanJob:
    """
    One manual scan on its own thread, so reruns cannot interrupt it and the page
    renders fully while a fragment polls for progress. The worker never touches
    Streamlit: it only appends to `events` and sets `result`.
    """

    def __init__(self, topic):
        self.topic = topic
        self.cancel = threading.Event()
        self.events = []
        self.result = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, name=f"scan-{topic}",
                                       daemon=True)

    def _run(self):
        try:
            self.result = engine.scan(self.topic, self._progress,
                                      max_posts=MANUAL_MAX_POSTS, cancel=self.cancel)
        except BaseException as exc:              # noqa: BLE001
            self.result = engine.ScanResult(topic=self.topic, status="failed",
                                            error=f"The scan stopped unexpectedly: {exc}")

    def _progress(self, stage, info):
        if stage != "tick":
            self.events.append((stage, info))

    @property
    def running(self):
        return self.thread.is_alive()


def _progress_line(stage, info):
    """One status line for a scan progress event, or None for silent stages."""
    if stage == "loading":
        return "⚙️ Warming up the analysis engine…"
    if stage == "planning":
        return "🧭 Working out the best places and phrases to search…"
    if stage == "planned":
        return "✅ Search plan ready — using a direct search for your keywords."
    if stage == "collecting":
        return ("🚀 Reading GitHub, Hacker News, Dev.to and Mastodon in parallel. "
                "This usually takes a minute or two…")
    if stage == "platform_done":
        count = info['count']
        vol = info.get('volume') or {}
        matching = vol.get('estimated_matches', count)
        more = f" out of ≈{matching:,} matching" if matching > count else ""
        return (f"{'✅' if count else '⚪'} **{info['platform']}** — {count} "
                f"post{'s' if count != 1 else ''} analysed{more}")
    if stage == "scoring":
        return "📊 Ranking what's gaining momentum…"
    return None


def start_scan(topic):
    """Starts a manual scan of `topic` on a background thread (see ScanJob)."""
    job = ScanJob(topic)
    st.session_state["scan_job"] = job
    job.thread.start()


@st.fragment(run_every=1)
def render_scan_progress():
    """Live scan progress, refreshed every second; reruns the app when the scan ends."""
    job = st.session_state.get("scan_job")
    if job is None:
        return
    if not job.running:
        st.rerun(scope="app")
    elapsed = time.monotonic() - job.started
    label = ("Cancelling…" if job.cancel.is_set()
             else f"Scanning the web for **{job.topic}**… ⏱️ {elapsed:.0f}s")
    with st.status(label, expanded=True, state="running"):
        for stage, info in list(job.events):
            line = _progress_line(stage, info)
            if line:
                st.write(line)


def finish_scan(job):
    """Collects a finished scan: feedback, cache invalidation on success, routing on cancel."""
    st.session_state.pop("scan_job", None)
    st.session_state["scan_running"] = False
    result = job.result or engine.ScanResult(topic=job.topic, status="failed",
                                             error="The scan ended without a result.")
    topic = job.topic

    if result.status == "cancelled":
        st.session_state["current_view"] = "home"
        st.session_state["scan_query"] = ""
        st.toast("Search cancelled — nothing was saved.", icon="🛑")
        return
    # Anti-noise filter: too little relevant data for a generic term.
    if result.status == "no_results" and result.error == engine.LOW_VOLUME_MESSAGE:
        st.session_state["scan_feedback"] = {"kind": "warning", "message": result.error}
        return
    # Too few communities: a legitimate answer about the topic, shown as an info card.
    if result.status == "no_results" and result.error == engine.INSUFFICIENT_PLATFORMS_MESSAGE:
        st.session_state["scan_feedback"] = {"kind": "insufficient", "topic": topic,
                                             "message": result.error,
                                             "per_platform": result.per_platform}
        return
    if result.status == "no_results":
        st.session_state["scan_feedback"] = {"kind": "no_results", "topic": topic,
                                             "message": result.error}
        return
    # A rejected topic is an answer about the input, not a crash.
    if result.status == "invalid":
        st.session_state["scan_feedback"] = {"kind": "invalid", "message": result.error}
        return
    if result.error:
        st.session_state["scan_feedback"] = {"kind": "warning", "message": result.error}
        return

    # Every memoised loader holds the previous corpus.
    fetch_balanced_trends.clear()
    load_topic_profile.clear()
    build_semantic_graph.clear()
    load_scan_volume.clear()
    reset_view_state()
    found = ", ".join(f"{p} {n}" for p, n in result.per_platform.items())
    st.toast(f"Found {result.fetched} posts about “{topic}” ({found}) in "
             f"{result.duration_s:.0f}s.", icon="✅")
