"""Homepage: the blocking cold-start screen and the live Intelligence Brief."""

import sqlite3
import time
from collections import Counter
from html import escape as html_escape

import pandas as pd
import streamlit as st

import engine
import scheduler
from engine import sentiment
from config import (PLATFORMS as PLATFORM_ORDER, HOT_MIN_PLATFORMS, HOT_MIN_TOTAL_POSTS,
                    SEED_TOPIC_COUNT, MANUAL_MAX_POSTS, BACKGROUND_MAX_POSTS)
from database.manager import TrendManager
from data import fetch_top_reads
from scan import queue_scan, scan_locked
from utils import TABLE_ROW_PX, md_link, relative_time, safe_url, shorten


# Cold-start screen: the feed needs this many distinct topics before it is shown.
FEED_SIZE = SEED_TOPIC_COUNT
COLD_START_POLL_S = 3
COLD_START_MESSAGE = (f"Initializing autonomous radar... Fetching and analyzing {FEED_SIZE} "
                      "top tech trends. This may take up to 3 minutes. Please wait.")


def _live_topic_count():
    """Distinct topics in live_trends; a read error counts as zero (keep waiting)."""
    try:
        return TrendManager().count_live_topics()
    except sqlite3.Error as exc:
        print(f"⚠️  Cold-start check failed, still waiting: {exc}")
        return 0


def is_cold_start():
    """True until the background worker has saved FEED_SIZE distinct topics."""
    return _live_topic_count() < FEED_SIZE


def render_cold_start(services):
    """
    Blocking loading screen. One stable spinner polls the database and reruns exactly
    once, when the feed is full, so the screen never flickers.
    """
    st.markdown("<div style='height: 22vh'></div>", unsafe_allow_html=True)
    _, centre, _ = st.columns([1, 2, 1])
    with centre:
        if services["worker"] is None:
            # Waiting would be endless with no worker, so say so instead.
            st.error(f"The background worker failed to start: {services['error']}. "
                     "Check the terminal and restart with `python main.py`.")
            st.stop()
        # Fetched once before the polling loop, so the links stay clickable meanwhile.
        reads = fetch_top_reads()
        if reads:
            st.markdown("##### 📚 While the radar initializes, check out today's top tech reads:")
            for title, url in reads:
                st.markdown(f"- {md_link(title, url)}")
        progress = st.empty()
        shown = None
        with st.spinner(COLD_START_MESSAGE):
            while True:
                ready = _live_topic_count()
                if ready >= FEED_SIZE:
                    break
                # Redraw only on change; the write also lets Streamlit stop a closed tab.
                if ready != shown:
                    progress.caption(f"{ready}/{FEED_SIZE} trends ready")
                    shown = ready
                time.sleep(COLD_START_POLL_S)
    st.rerun()


def _explore_live(topic):
    """Deep Dive callback: queues a scan and requests a full-app rerun (a fragment
    rerun would never reach the search bar that starts the scan)."""
    queue_scan(topic)
    st.session_state["explore_rerun"] = True


def _is_hot(trend):
    """Strong momentum (🔥): enough spread (communities) AND volume (valid posts)."""
    return trend['platforms'] >= HOT_MIN_PLATFORMS and trend['posts'] >= HOT_MIN_TOTAL_POSTS


def _affinity(trend):
    """The topic's community-affinity label (see sentiment.affinity)."""
    return sentiment.affinity(trend['sentiment'], trend['positive'], trend['negative'],
                              trend['posts'])


# Emoji per affinity label, so the mood reads at a glance next to the words.
AFFINITY_ICONS = {"Highly positive": "😊", "Positive": "🙂", "Neutral": "😐",
                  "Mixed": "🤔", "Negative": "😟"}


def _topic_list(topics):
    return ", ".join(f"<b>#{html_escape(t)}</b>" for t in topics)


def _brief_insights(trends):
    """Section A: one-line findings computed from the live topics."""
    # get_live_trends() ranks by spread, then volume: the first topic leads.
    lead = trends[0]
    # (posts, matches) per community: capped samples often tie, so matches decide.
    by_platform = {}
    for t in trends:
        for platform, v in t['per_platform'].items():
            posts, matches = by_platform.get(platform, (0, 0))
            by_platform[platform] = (posts + v['posts'], matches + v['matches'])
    busiest, (busiest_posts, _) = max(by_platform.items(), key=lambda kv: kv[1])
    everywhere = [t['topic'] for t in trends if t['platforms'] == len(PLATFORM_ORDER)]
    strong = [t['topic'] for t in trends if _is_hot(t)]
    latest = max(t['scanned_at'] for t in trends)

    # Overall affinity, naming the warmest and most critical topics when they differ.
    total = sum(t['posts'] for t in trends)
    overall = sentiment.affinity(
        sum(t['sentiment'] * t['posts'] for t in trends) / total if total else 0.0,
        sum(t['positive'] for t in trends), sum(t['negative'] for t in trends), total)
    warmest = max(trends, key=lambda t: t['sentiment'])
    coolest = min(trends, key=lambda t: t['sentiment'])
    mood = (f"💬 <b>Community affinity:</b> the conversation is "
            f"<b>{overall.lower()}</b> overall {AFFINITY_ICONS[overall]}")
    if warmest is not coolest:
        mood += (f" — warmest around <b>#{html_escape(warmest['topic'])}</b> "
                 f"({_affinity(warmest).lower()}), most critical around "
                 f"<b>#{html_escape(coolest['topic'])}</b> ({_affinity(coolest).lower()}).")
    else:
        mood += "."

    matches = f" (≈{lead['matches']:,} matching posts platform-wide)" if lead['matches'] else ""
    bullets = [
        f"🥇 <b>#{html_escape(lead['topic'])}</b> leads this sweep: <b>{lead['posts']}</b> "
        f"posts across <b>{lead['platforms']}</b> communities{matches}.",
        f"📣 <b>{html_escape(busiest)}</b> is the most active community, contributing "
        f"<b>{busiest_posts}</b> posts across the tracked topics.",
        (f"🌐 Discussed on all four communities: {_topic_list(everywhere)}."
         if everywhere else "🌐 No topic reached all four communities in this sweep."),
        (f"🔥 Strong momentum: {_topic_list(strong)} ({HOT_MIN_PLATFORMS}+ communities "
         f"and {HOT_MIN_TOTAL_POSTS}+ posts)."
         if strong else
         f"🔥 No topic reached strong momentum this sweep. Last sweep "
         f"{relative_time(latest)}."),
        mood,
    ]
    return "".join(f"<li>{b}</li>" for b in bullets)


def _brief_buzzwords(trends, limit=14):
    """Section B: the KeyBERT phrases recurring most across the live topics' posts."""
    topics = {t['topic'].lower() for t in trends}
    counts = Counter(clean for t in trends for v in t['per_platform'].values()
                     for clean in map(engine.clean_buzzword, v.get('keywords', []))
                     if clean and clean not in topics)
    return [k for k, _ in counts.most_common(limit)]


# Platforms whose top post counts as an "article" (GitHub contributes repositories;
# Mastodon toots are conversation, not articles).
ARTICLE_PLATFORMS = ("Hacker News", "Dev.to")


def _top_links(posts):
    """A topic's 1-2 headline links: its best article and its best repository."""
    def best(platforms):
        pool = [p for p in posts
                if p['source_platform'] in platforms and safe_url(p['url'])]
        return max(pool, key=lambda p: p['trend_score'] or 0, default=None)
    return [(icon, kind, post) for icon, kind, post in
            (("📰", "Top article", best(ARTICLE_PLATFORMS)),
             ("💻", "Top repository", best(("GitHub",)))) if post]


def render_leaderboard(trends):
    """Top Market Trends: every live topic ranked by spread, then volume."""
    max_volume = BACKGROUND_MAX_POSTS * len(PLATFORM_ORDER)
    rows = []
    for rank, t in enumerate(trends, 1):
        label = _affinity(t)
        rows.append({
            "#": rank,
            "Topic": f"#{t['topic']}",
            "Volume": t['posts'],
            "Spread": f"{t['platforms']} / {len(PLATFORM_ORDER)}",
            "Matching posts": f"≈{t['matches']:,}" if t['matches'] else "—",
            "Sentiment": f"{AFFINITY_ICONS[label]} {label}",
            "Momentum": "🔥 Strong" if _is_hot(t) else "—",
        })
    st.dataframe(
        pd.DataFrame(rows), key="leaderboard", hide_index=True, width="stretch",
        height=TABLE_ROW_PX * (len(rows) + 1) + 3,
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "Volume": st.column_config.ProgressColumn(
                "Volume", min_value=0, max_value=max_volume, format="%d posts",
                help="Valid posts collected for the topic in the latest sweep"),
            "Spread": st.column_config.TextColumn(
                help="Communities where the topic has valid posts"),
            "Matching posts": st.column_config.TextColumn(
                help="Posts the platforms themselves report as matching the topic"),
            "Momentum": st.column_config.TextColumn(
                help=f"Strong: {HOT_MIN_PLATFORMS}+ communities and "
                     f"{HOT_MIN_TOTAL_POSTS}+ posts"),
        },
    )


def _render_topic(trend, posts, index):
    """Section C card: header and Deep Dive, the topic's metrics, its 1-2 top links."""
    label = _affinity(trend)
    flame = " 🔥" if _is_hot(trend) else ""
    with st.container(border=True, key=f"brief_topic_{index}"):
        c_head, c_action = st.columns([5, 1], vertical_alignment="center")
        c_head.markdown(f"#### {index + 1}. #{trend['topic']}{flame}")
        c_action.button("🔍 Deep Dive", key=f"btn_{trend['topic']}", width="stretch",
                        on_click=_explore_live, args=(trend['topic'],),
                        help=f"Run a full {MANUAL_MAX_POSTS}-post search and open the "
                             f"5-tab analysis for #{trend['topic']}",
                        disabled=scan_locked())
        m_volume, m_spread, m_mood = st.columns(3)
        m_volume.metric("Volume", f"{trend['posts']} posts",
                        help="Valid posts collected in the latest sweep")
        m_spread.metric("Spread", f"{trend['platforms']} / {len(PLATFORM_ORDER)} communities")
        m_mood.metric("Sentiment", f"{AFFINITY_ICONS[label]} {label}",
                      help=f"{trend['positive']} positive and {trend['negative']} "
                           f"negative posts (VADER)")
        links = _top_links(posts)
        for icon, kind, post in links:
            st.markdown(f"{icon} **{kind}** · {post['source_platform']} · score "
                        f"{post['trend_score'] or 0:.0f} — "
                        f"{md_link(shorten(post['title'], 100), post['url'])}")
        if not links:
            st.caption("No linkable posts were stored for this topic.")


@st.fragment(run_every=60)
def render_intelligence_brief(worker):
    """Live homepage dashboard: leaderboard, insights, buzzwords and one card per
    topic. A fragment, so it refreshes every minute on its own."""
    # Set by a Deep Dive click: escalate this fragment rerun to a full-app rerun.
    if st.session_state.pop("explore_rerun", False):
        st.rerun(scope="app")

    try:
        manager = TrendManager()
        trends = manager.get_live_trends(limit=FEED_SIZE)
        all_posts = manager.get_top_live_posts()
    except sqlite3.Error as exc:
        print(f"⚠️  Intelligence brief unavailable: {exc}")
        return
    if not trends:
        return

    scanning = engine.background_topic()
    upcoming = scheduler.next_run_time(worker) if worker is not None else None
    if scanning:
        chip = f"<span class='live-chip on'>● Scanning #{html_escape(scanning)}</span>"
    elif worker is None:
        chip = "<span class='live-chip'>Worker offline</span>"
    else:
        chip = (f"<span class='live-chip'>⏱ Next sync {upcoming:%H:%M}</span>"
                if upcoming else "")
    st.markdown(f"<div class='live-head'><span class='title'>🏁 Top Market Trends</span>"
                f"{chip}</div>", unsafe_allow_html=True)
    render_leaderboard(trends)

    col_a, col_b = st.columns([1.5, 1], gap="medium")
    with col_a, st.container(border=True, key="brief_insights"):
        st.markdown(f"<p class='brief-title'>🧠 Key insights</p>"
                    f"<ul class='brief-list'>{_brief_insights(trends)}</ul>",
                    unsafe_allow_html=True)
    with col_b, st.container(border=True, key="brief_buzz"):
        chips = "".join(f"<span class='term-chip'>{html_escape(k)}</span>"
                        for k in _brief_buzzwords(trends))
        empty = "<span class='k-sub'>No recurring phrases yet.</span>"
        st.markdown(f"<p class='brief-title'>🗣️ Keywords driving the conversation</p>"
                    f"{chips or empty}", unsafe_allow_html=True)

    posts_by_topic = {}
    for post in all_posts:
        posts_by_topic.setdefault(post['topic'], []).append(post)
    st.markdown("<p class='brief-title'>📌 Topic highlights</p>", unsafe_allow_html=True)
    for i, trend in enumerate(trends):
        _render_topic(trend, posts_by_topic.get(trend['topic'], []), i)
    st.caption("Post scores are relative to each topic's own sample (per-platform "
               "Z-score), so they rank momentum within a topic's community.")


def render_footer():
    """Project credit at the foot of the homepage (the app has no sidebar)."""
    st.caption("🧿 TrendAnalyzer Pro · " + " · ".join(PLATFORM_ORDER)
               + " · 🎓 Academic capstone by Alon Kreisberger")
