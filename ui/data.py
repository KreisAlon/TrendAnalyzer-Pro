"""Data access layer: memoised read-only loaders and the once-per-process background services."""

import os
import sqlite3
from contextlib import closing

import httpx
import pandas as pd
import streamlit as st

from graph_analyzer import GraphBuilder
import scheduler
from config import DB_PATH, TARGET_POSTS_PER_PLATFORM, DEVTO_ARTICLES_URL
from engine.topic_profile import TopicProfile
from database.manager import SQLITE_BUSY_TIMEOUT_S, TrendManager
from utils import safe_url


# Loaders are memoised for five minutes and cleared after each scan, so widget
# reruns never re-query the DB.

@st.cache_data(ttl=300)
def fetch_balanced_trends():
    """Top-N posts per platform by trend score, plus `published_dt` and `pub_date`."""
    # main.py deletes the database at launch, so a missing file is the normal first state.
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    try:
        # closing(): sqlite3's own context manager only commits, it never closes.
        with closing(sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_S)) as conn:
            # Partitioned ROW_NUMBER(): a global ORDER BY would let the platform with
            # the largest scores crowd out the others.
            df = pd.read_sql_query("""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER(PARTITION BY source_platform ORDER BY trend_score DESC) as rn
                    FROM unified_posts
                ) WHERE rn <= ? ORDER BY trend_score DESC
            """, conn, params=(TARGET_POSTS_PER_PLATFORM,))
    except (sqlite3.Error, pd.errors.DatabaseError):
        # The table does not exist until the first scan.
        return pd.DataFrame()

    # Collectors normalise to ISO 8601 UTC; a malformed stamp becomes NaT.
    df['published_dt'] = pd.to_datetime(df['published_at'], format='ISO8601',
                                        utc=True, errors='coerce')
    df['pub_date'] = df['published_dt'].dt.date
    return df


@st.cache_resource(show_spinner="Mapping how the posts connect…")
def build_semantic_graph(percentile, top_n):
    """
    (networkx.Graph, metrics) from GraphBuilder.build(), memoised per arguments.
    cache_resource avoids pickling the graph on every rerun; callers must not mutate it.
    """
    return GraphBuilder(percentile=percentile, top_n_per_platform=top_n).build()


@st.cache_data(ttl=300)
def load_topic_profile():
    """Persisted search plan of the stored corpus as a dict, or None before the first scan."""
    profile = TopicProfile.load()
    return profile.to_dict() if profile else None


@st.cache_data(ttl=300)
def load_scan_volume():
    """Latest scan's per-platform volume and gate tallies; {} if unavailable."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        # The manager also migrates older schemas.
        return TrendManager().get_volume()
    except sqlite3.Error:
        return {}


@st.cache_resource(show_spinner=False)
def start_background_services():
    """
    Runs once per process: creates the schema (first, so the worker's initial cycle
    never races the dashboard to create tables), then starts the APScheduler worker.

    Returns:
        dict: {"worker": BackgroundScheduler or None, "error": str}; never raises.
    """
    try:
        TrendManager()
        return {"worker": scheduler.start_scheduler(), "error": ""}
    except Exception as exc:                      # noqa: BLE001
        print(f"⚠️  Background scheduler did not start: {exc}")
        return {"worker": None, "error": str(exc)}


@st.cache_data(ttl=900, show_spinner=False)
def fetch_top_reads(count=3):
    """Today's top Dev.to articles as (title, url) pairs for the loading screen; [] on failure."""
    try:
        resp = httpx.get(DEVTO_ARTICLES_URL, params={"top": 1, "per_page": count},
                         timeout=3, headers={"User-Agent": "TrendAnalyzer/6.0"})
        resp.raise_for_status()
        articles = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    return [(a.get("title") or "Untitled", safe_url(a.get("url")))
            for a in articles if isinstance(a, dict) and safe_url(a.get("url"))][:count]
