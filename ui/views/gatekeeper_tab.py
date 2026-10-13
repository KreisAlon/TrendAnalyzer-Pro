"""Gatekeeper's Filter tab: what the data-quality gate rejected, and why."""

from html import escape as html_escape

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from graph_analyzer import PLATFORM_COLORS, TEXT_PRIMARY
from config import PLATFORMS as PLATFORM_ORDER, TARGET_POSTS_PER_PLATFORM
from data import load_scan_volume
from utils import CHART_HOVERLABEL, GATE_BAR_COLOR, hbar_figure, kpi_grid, platform_pill


# Every inspected candidate ends accepted, rejected with a reason, or unreachable,
# so the funnel balances against what was stored.
# Raw rejection codes grouped into reader-facing categories: (label, description, codes).
REJECT_CATEGORIES = (
    ("Off-topic", "Matched none of the topic's strong terms and too few weak ones",
     ("not_topic_relevant",)),
    ("Too short or thin", "Below the character or word floor, leaving nothing to analyse",
     ("too_short", "too_few_words")),
    ("Wrong language", "Not confidently identified as English",
     ("not_english", "lang_undetectable")),
    ("Bot walls & junk pages", "CAPTCHA, paywall, 'enable JavaScript' and error pages",
     ("junk_page",)),
    ("Spam & malformed text", "Hashtag soup, repeated tokens, binary or markup residue",
     ("hashtag_soup", "repetitive", "low_alpha_ratio")),
    ("Duplicates & reposts", "Identical text already accepted, or a boosted repost",
     ("duplicate", "repost")),
    ("Replies & flagged posts", "Personal replies and content-warned posts",
     ("reply", "sensitive")),
    ("No extractable themes", "The keyword model found no confident key phrase",
     ("no_keywords",)),
    ("Unreachable", "Dead links, timeouts and malformed API records",
     ("unreachable", "processing_error")),
)

# Gate stages in execution order, cheapest first.
GATE_STAGES = (
    ("Structure", "Length, word count, letter ratio, hashtag density, repetition, bot walls"),
    ("Relevance", "Two-tier, word-boundary match against the topic's vocabulary"),
    ("Uniqueness", "SHA-256 fingerprint of the normalised text"),
    ("Language", "Statistical language identification, ≥ 85% English"),
    ("Themes", "KeyBERT must extract at least one confident key phrase"),
)

GATE_BLOCKED_COLOR = "#cbd5e1"  # De-emphasised blocked share of a meter


def gatekeeper_summary(volume):
    """
    Aggregates per-platform gate tallies into funnel and category figures.

    Returns:
        dict | None: {'platforms', 'totals', 'categories' (sorted by count)}, or None
        when the latest scan predates gate accounting.
    """
    rows = {p: v for p, v in (volume or {}).items() if v.get('gated') is not None}
    if not rows:
        return None

    code_to_category = {code: label for label, _, codes in REJECT_CATEGORIES for code in codes}
    platforms, categories = {}, {}
    for platform, v in rows.items():
        inspected = int(v.get('gated') or 0)
        passed = int(v.get('accepted') or 0)
        per_cat = {}
        # Codes unknown to this view (e.g. from a newer collector) fall into 'Other'.
        for code, n in (v.get('reject_reasons') or {}).items():
            label = code_to_category.get(code, "Other")
            per_cat[label] = per_cat.get(label, 0) + int(n or 0)
            categories.setdefault(label, {}).setdefault(platform, 0)
            categories[label][platform] += int(n or 0)
        platforms[platform] = {
            # Mastodon pages are not de-duplicated before inspection, so the retrieved
            # count is floored at the inspected count to keep the funnel monotonic.
            'retrieved': max(int(v.get('unique_candidates') or 0), inspected),
            'inspected': inspected,
            'passed': passed,
            'stored': int(v.get('stored') or 0),
            # Inspected but not passed: rejected with a reason or unreachable.
            'blocked': max(inspected - passed, 0),
            'categories': per_cat,
        }

    totals = {k: sum(p[k] for p in platforms.values())
              for k in ('retrieved', 'inspected', 'passed', 'stored', 'blocked')}
    ranked = sorted(((label, sum(by_plat.values()), by_plat)
                     for label, by_plat in categories.items() if sum(by_plat.values()) > 0),
                    key=lambda c: c[1], reverse=True)
    return {'platforms': platforms, 'totals': totals, 'categories': ranked}


def build_gate_funnel(totals):
    """Four-stage funnel (retrieved, inspected, passed, stored) in a single hue."""
    stages = ["Retrieved from APIs", "Inspected by the gate", "Passed every check",
              "Stored for analysis"]
    values = [totals['retrieved'], totals['inspected'], totals['passed'], totals['stored']]
    fig = go.Figure(go.Funnel(
        y=stages, x=values, textinfo="value+percent initial",
        textfont=dict(color="#ffffff", size=13, family="Inter, sans-serif"),
        marker=dict(color=GATE_BAR_COLOR, line=dict(color="#ffffff", width=2)),
        connector=dict(fillcolor="#eef1f5", line=dict(width=0)),
        hovertemplate="<b>%{y}</b><br>%{x:,} posts<br>%{percentInitial:.0%} of retrieved"
                      "<extra></extra>",
    ))
    fig.update_layout(
        height=300, margin=dict(t=6, l=6, r=6, b=6),
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
        font=dict(family="Inter, sans-serif", color=TEXT_PRIMARY),
        yaxis=dict(tickfont=dict(size=13, color=TEXT_PRIMARY)),
        hoverlabel=CHART_HOVERLABEL,
    )
    return fig


def build_rejection_figure(categories, blocked_total):
    """
    Horizontal bar chart of rejection categories, largest at the top.

    Args:
        categories (list): (label, count, per-platform counts), sorted descending.
        blocked_total (int): Denominator of each share label.
    """
    rows = list(reversed(categories))   # Plotly draws row 0 at the bottom
    # Falls back to the category sum if tallies are unbalanced (avoids division by zero).
    denom = blocked_total or sum(n for _, n, _ in rows) or 1
    labels = [f"   {n:,} · {n / denom:.0%}" for _, n, _ in rows]
    hover = ["<b>" + html_escape(label, quote=False) + "</b><br>" + "<br>".join(
                 f"{p}: {n:,}" for p, n in sorted(by_plat.items(), key=lambda x: -x[1]) if n)
             for label, _, by_plat in rows]
    return hbar_figure([n for _, n, _ in rows], [label for label, _, _ in rows],
                       labels, hover, height=max(220, 38 * len(rows) + 30),
                       margin=dict(t=6, l=6, r=110, b=6))


def render_gatekeeper_tab():
    """
    Renders the Gatekeeper's Filter view: KPIs, funnel, rejection reasons,
    per-community meters, gate stages and a table equivalent.
    """
    st.markdown("### The Gatekeeper's Filter")
    st.markdown(
        "<p class='card-sub'>Search APIs return far more than is worth analysing. Every "
        "candidate passes through a five-stage quality gate before it is stored: "
        "structural checks, a relevance test against a vocabulary derived from the "
        "search terms, "
        "de-duplication, language identification and keyword extraction. This tab shows "
        "what the gate blocked and why.</p>", unsafe_allow_html=True)

    summary = gatekeeper_summary(load_scan_volume())
    if summary is None:
        st.info("These results come from a scan that did not record gate statistics. "
                "**Scan the topic again** to see the filter breakdown.", icon="🔄")
        return
    totals = summary['totals']
    if totals['inspected'] == 0:
        st.info("No candidates reached the gate in this scan.", icon="🛡️")
        return

    blocked_share = totals['blocked'] / totals['inspected']
    top = summary['categories'][0] if summary['categories'] else None
    rejected = totals['blocked'] or sum(n for _, n, _ in summary['categories']) or 1
    kpis = [
        ("Inspected", f"{totals['inspected']:,}", f"of {totals['retrieved']:,} retrieved"),
        ("Noise blocked", f"{blocked_share:.0%}", f"{totals['blocked']:,} posts rejected"),
        ("Signal kept", f"{totals['stored']:,}",
         f"stored · up to {TARGET_POSTS_PER_PLATFORM} per community"),
        ("Main reason", html_escape(top[0]) if top else "—",
         f"{top[1] / rejected:.0%} of blocked posts" if top else "nothing blocked"),
    ]
    st.markdown(kpi_grid(kpis, "four"), unsafe_allow_html=True)

    c_funnel, c_reasons = st.columns([1, 1.15], gap="medium")
    with c_funnel:
        with st.container(key="insight_gate_funnel"):
            st.markdown("<p class='card-title'>From retrieval to storage</p>"
                        "<p class='card-sub'>Inspection stops once a community has enough "
                        "accepted posts, so not every retrieved candidate is "
                        "inspected.</p>", unsafe_allow_html=True)
            st.plotly_chart(build_gate_funnel(totals), key="gate_funnel",
                            config={"displayModeBar": False})
    with c_reasons:
        with st.container(key="insight_gate_reasons"):
            st.markdown("<p class='card-title'>Why posts were blocked</p>"
                        "<p class='card-sub'>Share of all rejections. Hover a bar for the "
                        "per-community split.</p>", unsafe_allow_html=True)
            if summary['categories']:
                st.plotly_chart(build_rejection_figure(summary['categories'], totals['blocked']),
                                key="gate_reasons",
                                config={"displayModeBar": False})
            else:
                st.caption("Nothing was blocked in this scan.")

    with st.container(key="insight_gate_platforms"):
        st.markdown("<p class='card-title'>Kept vs. blocked, per community</p>"
                    "<p class='card-sub'>Coloured: passed the gate · Grey: blocked.</p>",
                    unsafe_allow_html=True)
        rows = []
        for platform in PLATFORM_ORDER:
            p = summary['platforms'].get(platform)
            if not p:
                continue
            kept_w = p['passed'] / p['inspected'] * 100 if p['inspected'] else 0
            # Zero-width segments are omitted so the 2px gap stays even.
            segments = ""
            if kept_w > 0:
                segments += (f"<div class='seg' style='width:{kept_w:.1f}%;"
                             f"background:{PLATFORM_COLORS[platform]}'></div>")
            if kept_w < 100 and p['inspected']:
                segments += (f"<div class='seg' style='width:{100 - kept_w:.1f}%;"
                             f"background:{GATE_BLOCKED_COLOR}'></div>")
            # Exact counts beside the meter: identity never rests on colour alone.
            rows.append(
                f"<div class='gate-row'>{platform_pill(platform)}"
                f"<div class='meter'>{segments}</div>"
                f"<div class='nums'><b>{p['passed']:,}</b> kept · <b>{p['blocked']:,}</b> "
                f"blocked</div></div>")
        st.markdown("".join(rows), unsafe_allow_html=True)

    steps = "".join(
        f"<div class='gate-step'><div class='n'>Stage {i}</div><div class='t'>{name}</div>"
        f"<div class='d'>{desc}</div></div>"
        for i, (name, desc) in enumerate(GATE_STAGES, start=1))
    st.markdown("<div class='section-label'>How the gate works — cheapest check first</div>"
                f"<div class='gate-steps'>{steps}</div>", unsafe_allow_html=True)

    with st.expander("View the numbers as a table"):
        table = []
        for platform, p in summary['platforms'].items():
            row = {"Community": platform, "Retrieved": p['retrieved'],
                   "Inspected": p['inspected'], "Passed": p['passed'],
                   "Stored": p['stored'], "Blocked": p['blocked']}
            row.update({label: p['categories'].get(label, 0)
                        for label, _, _ in summary['categories']})
            table.append(row)
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
        st.caption("; ".join(f"**{label}**: {desc}" for label, desc, _ in REJECT_CATEGORIES))
