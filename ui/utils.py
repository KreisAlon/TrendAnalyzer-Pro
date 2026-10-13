"""Shared presentation helpers: HTML snippets, bar chart, safe URLs and text formatting."""

import textwrap
from datetime import datetime
# All third-party text is escaped before being embedded in raw HTML.
from html import escape as html_escape

import pandas as pd
import plotly.graph_objects as go

from graph_analyzer import PLATFORM_COLORS, TEXT_PRIMARY, TEXT_SECONDARY
from styles import LINE, MUTED


def platform_pill(platform):
    """HTML badge for a platform; the name is always shown, so identity never relies on colour."""
    color = PLATFORM_COLORS.get(platform, MUTED)
    return (f"<span class='pill'><span class='dot' style='background:{color}'></span>"
            f"{html_escape(str(platform))}</span>")


def kpi_grid(kpis, variant=""):
    """HTML grid of KPI tiles from (label, value, caption) triples; values must be pre-escaped."""
    tiles = "".join(f"<div class='kpi'><div class='k-label'>{label}</div>"
                    f"<div class='k-value'>{value}</div><div class='k-sub'>{sub}</div></div>"
                    for label, value, sub in kpis)
    return f"<div class='kpi-grid{' ' + variant if variant else ''}'>{tiles}</div>"


# Single neutral series hue and hover style shared by the bar and funnel charts.
GATE_BAR_COLOR = "#334155"
CHART_HOVERLABEL = dict(bgcolor="#ffffff", bordercolor=LINE,
                        font=dict(color=TEXT_PRIMARY, size=12))


def hbar_figure(x, y, text, hover, height, margin):
    """Directly-labelled horizontal bar chart in one neutral hue (no legend needed)."""
    fig = go.Figure(go.Bar(
        x=x, y=y, orientation='h', marker=dict(color=GATE_BAR_COLOR),
        text=text, textposition='outside', cliponaxis=False,  # Labels may enter the margin
        textfont=dict(color=TEXT_SECONDARY, size=12),
        hovertext=hover, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_layout(
        height=height, barcornerradius=4, bargap=0.35, margin=margin,
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        font=dict(family="Inter, sans-serif", color=TEXT_PRIMARY),
        xaxis=dict(visible=False, rangemode='tozero'),
        yaxis=dict(tickfont=dict(size=13, color=TEXT_PRIMARY), automargin=True),
        hoverlabel=CHART_HOVERLABEL,
    )
    return fig


def safe_url(val):
    """The URL if it is an absolute http(s) link, else '' (blocks NaN and `javascript:`)."""
    # Duplicate column labels make pandas return a Series.
    if isinstance(val, pd.Series):
        val = val.iloc[0]
    text = str(val) if val is not None and pd.notna(val) else ''
    return text if text.startswith(('http://', 'https://')) else ''


def relative_time(iso_text):
    """Coarse relative label ('just now', '12 min ago', '3 h ago', '02 Oct'); '—' if unparseable."""
    try:
        then = datetime.fromisoformat(str(iso_text))
    except (TypeError, ValueError):
        return "—"
    # Same tzinfo as the stamp, so naive and aware datetimes are never mixed.
    now = datetime.now(then.tzinfo)
    minutes = (now - then).total_seconds() / 60
    if minutes < 1.5:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    if minutes < 24 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{then:%d %b}"


def shorten(text, width):
    """Shortens text to `width` chars on a word boundary; empty text becomes 'Untitled'."""
    return textwrap.shorten(str(text or ''), width=width, placeholder="…") or "Untitled"


# Plain-language explanation of the score, shown as a tooltip beside it.
TREND_SCORE_HELP = ("We score posts based on how much engagement (upvotes, comments, "
                    "shares) they get compared to the average post on that specific "
                    "platform. A high score means the post is exceptionally popular "
                    "right now.")


def md_link(text, url):
    """A Markdown link whose text and target cannot break the link syntax."""
    text = str(text).replace("[", "(").replace("]", ")")
    target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{text}]({target})"


# Pixel height of one st.dataframe row (and of its header), for fit-to-content tables.
TABLE_ROW_PX = 35


def display_title(post):
    """Title shown for a post; Mastodon posts have no real title, so their full text is used."""
    content, title = post.get('content'), post.get('title')
    # NULL columns arrive as NaN in DataFrame rows, and NaN is truthy.
    if post.get('source_platform') == "Mastodon" and isinstance(content, str) and content:
        return content
    return title if isinstance(title, str) and title else "Untitled"
