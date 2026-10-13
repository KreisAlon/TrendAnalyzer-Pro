"""
Ecosystem Wheel tab: the platform/post sunburst, its click listener, the Trend
Inspector side panel and the ranked table of every post in the search.
"""

import os
import textwrap
from html import escape as html_escape

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components_v1

from graph_analyzer import PLATFORM_COLORS, TEXT_PRIMARY
from config import PLATFORMS as PLATFORM_ORDER
from styles import LINE, MUTED
from utils import TREND_SCORE_HELP, display_title, platform_pill, safe_url, shorten


# Sunburst centre is a container, not a category, so it gets a neutral tone.
ROOT_COLOR = "#eef1f5"


def build_sunburst(df, topic):
    """
    Builds the sunburst: ring 1 = platform, ring 2 = post, sized by trend score.

    Stable node ids ("root", "platform:<name>", "post:<db id>") keep identical titles
    distinct and let a click resolve back to a database row.
    """
    ids, labels, parents, values, colors, hover = [], [], [], [], [], []
    platforms = [p for p in PLATFORM_ORDER if p in set(df['source_platform'])]
    platforms += sorted(set(df['source_platform']) - set(platforms))
    total = df['trend_score'].sum() or 1.0

    ids.append("root")
    labels.append(html_escape(shorten(topic, 22), quote=False))
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
            labels.append(html_escape(shorten(title, 28), quote=False))
            parents.append(f"platform:{platform}")
            # A floor of 1 keeps a near-zero score visible as a clickable sliver.
            values.append(max(float(r['trend_score'] or 0), 1.0))
            colors.append(color)
            hover.append(f"<b>{wrapped}</b><br>Platform: <b>{platform}</b>"
                         f"<br>Trend score: {float(r['trend_score'] or 0):.0f} / 100"
                         f"<br><i>Click to open in the inspector →</i>")

    fig = go.Figure(go.Sunburst(
        ids=ids, labels=labels, parents=parents, values=values,
        # 'remainder': containers carry no value of their own, so ring sizes derive
        # purely from the posts and rounding cannot hide a platform.
        branchvalues="remainder",
        marker=dict(colors=colors, line=dict(color="#ffffff", width=2)),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
        textinfo="label", insidetextorientation="radial",
        insidetextfont=dict(family="Inter, sans-serif"),
        sort=False,   # Canonical platform order, not size order
    ))
    # uirevision preserves the user's zoom level across Streamlit reruns.
    fig.update_layout(
        height=700, margin=dict(t=6, l=6, r=6, b=6),
        paper_bgcolor="#ffffff", uirevision="wheel",
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=LINE, align="left",
                        font=dict(color=TEXT_PRIMARY, size=13, family="Inter, sans-serif")),
    )
    return fig


# st.plotly_chart does not expose `plotly_sunburstclick`, so a custom component listens
# for it in the DOM. Declared once from a fixed path so its URL is stable across reruns.
_WHEEL_CLICK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "components", "wheel_click")
_wheel_click_listener = components_v1.declare_component("trend_wheel_click",
                                                        path=_WHEEL_CLICK_DIR)


def wheel_click():
    """The node id clicked since the previous run, else None (nonce-deduplicated,
    since a component value persists across reruns)."""
    value = _wheel_click_listener(key="wheel_click", default=None,
                                  data={"selector": ".st-key-wheel_card"})
    if not value or not value.get("clicked"):
        return None
    if value.get("nonce") == st.session_state.get("wheel_click_nonce"):
        return None
    st.session_state["wheel_click_nonce"] = value.get("nonce")
    return value["clicked"]


def _apply_wheel_click(node_id, df_ids):
    """Applies a clicked node id to inspector state; `df_ids` drops stale post clicks."""
    kind, _, value = str(node_id).partition(":")
    if kind == "post":
        try:
            post_id = int(value)
        except ValueError:
            return
        # A click may predate a new scan.
        if post_id in df_ids:
            st.session_state["inspect_pick"] = post_id
    elif kind == "platform":
        # Toggle: clicking the zoomed platform centre again clears the filter.
        current = st.session_state.get("platform_filter")
        st.session_state["platform_filter"] = None if current == value else value
    elif kind == "root":
        st.session_state["platform_filter"] = None


def render_inspector(df):
    """
    Side panel for the selected post: title, metadata, trend score and link.

    The selectbox and wheel clicks share the `inspect_pick` key; `platform_filter`
    optionally restricts the pool.
    """
    with st.container(key="inspector"):
        st.markdown("<p class='card-title'>🔎 Trend Inspector</p>"
                    "<p class='card-sub'>Click any post on the wheel, or pick one "
                    "below.</p>", unsafe_allow_html=True)

        platform_filter = st.session_state.get("platform_filter")
        # A filter left over from a previous scan would hide every post (and the reset).
        if platform_filter and platform_filter not in set(df['source_platform']):
            platform_filter = st.session_state["platform_filter"] = None
        pool = df if not platform_filter else df[df['source_platform'] == platform_filter]
        # Native ints stay comparable with the value held in session state.
        options = [int(i) for i in pool['id']]
        titles = {int(r['id']): f"{r['source_platform']} · {shorten(r['title'], 70)}"
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

        # Widget state may only be written before the widget is created; doing it here
        # lets a wheel click drive the selectbox. Stale selections reset to the first option.
        if st.session_state.get("inspect_pick") not in options:
            st.session_state["inspect_pick"] = options[0]
        post_id = st.selectbox("Post", options, key="inspect_pick",
                               format_func=lambda i: titles.get(i, str(i)),
                               label_visibility="collapsed")

        row = df[df['id'] == post_id].iloc[0]
        platform = row['source_platform']
        color = PLATFORM_COLORS.get(platform, MUTED)
        score = float(row.get('trend_score') or 0)

        meta = [html_escape(platform)]
        if row.get('author'):
            meta.append(f"by {html_escape(str(row['author']))}")
        if pd.notna(row.get('published_dt')):
            meta.append(f"{row['published_dt']:%d %b %Y}")
        st.markdown(f"<div class='post-title'>{html_escape(display_title(row))}</div>"
                    f"<div class='post-meta'>{' · '.join(meta)}</div>",
                    unsafe_allow_html=True)

        st.markdown(f"**Trend score** · {score:.0f} / 100", help=TREND_SCORE_HELP)
        # Bar width clamped to 2-100% so a near-zero score stays visible.
        st.markdown(f"<div class='score-track'><div class='score-fill' "
                    f"style='width:{max(min(score, 100), 2):.0f}%;background:{color}'>"
                    f"</div></div>", unsafe_allow_html=True)

        st.write("")
        url = safe_url(row.get('url'))
        if url:
            st.link_button(f"Open the original on {platform}  ↗", url,
                           width="stretch")
        else:
            st.caption("No link was stored for this post.")


def render_ranked_posts(posts, key):
    """
    Ranked table of linkable posts, best first.

    Args:
        posts (iterable[dict]): Rows with title, source_platform, trend_score, url.
        key (str): Widget key.
    """
    ranked = sorted((p for p in posts if safe_url(p.get('url'))),
                    key=lambda p: -(p.get('trend_score') or 0))
    rows = []
    for n, p in enumerate(ranked, 1):
        rows.append({"#": n, "Post": display_title(p),
                     "Platform": p['source_platform'],
                     "Score": round(p.get('trend_score') or 0, 1),
                     "Link": safe_url(p.get('url'))})
    st.dataframe(
        pd.DataFrame(rows), key=key, hide_index=True, width="stretch",
        height=400,
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "Post": st.column_config.TextColumn(width="large"),
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%.0f",
                help=TREND_SCORE_HELP),
            "Link": st.column_config.LinkColumn("Link", display_text="Read post ↗"),
        },
    )


def render_wheel_tab(df, topic):
    """Renders the Ecosystem Wheel view: sunburst and inspector, then every post ranked."""
    col_main, col_ins = st.columns([1.75, 1], gap="medium")

    with col_main:
        with st.container(key="wheel_card"):
            st.markdown(
                "<p class='card-title'>Where the conversation is happening</p>"
                "<p class='card-sub'>Inner ring: community · Outer ring: posts, sized "
                "by how much attention they're getting. Hover for details, click to "
                "inspect.</p>", unsafe_allow_html=True)
            st.plotly_chart(build_sunburst(df, topic), key="ecosystem_wheel",
                            config={"displayModeBar": False})
        clicked = wheel_click()
        # Reported only on the run caused by the click.
        if clicked:
            _apply_wheel_click(clicked, {int(i) for i in df['id']})

    # Rendered after the click is applied, so it reflects it in the same run.
    with col_ins:
        render_inspector(df)

    # Every post of this search, best first, below the wheel and inspector.
    with st.container(key="ranked_card"):
        st.markdown(f"<p class='card-title'>🏆 All posts in this search, ranked "
                    f"({len(df)})</p>", unsafe_allow_html=True)
        render_ranked_posts(df.to_dict('records'), key="search_ranked_posts")
