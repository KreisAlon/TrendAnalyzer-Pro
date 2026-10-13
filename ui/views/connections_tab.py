"""Connections tab: one evidence card per emerging cross-community bridge."""

from html import escape as html_escape

import streamlit as st

from graph_bridges import REGISTER_LABEL
from utils import platform_pill, safe_url, shorten


# Explained purely from graph measurements: no model call, quota or network failure.

def _match_strength(similarity):
    """Maps cosine similarity to a qualitative band."""
    if similarity >= 0.70:
        return "Very strong"
    if similarity >= 0.55:
        return "Strong"
    return "Moderate"


def _bridge_width(gap):
    """Maps the code-to-discussion distance between two communities to a plain label."""
    if gap >= 0.9:
        return "Very wide"
    if gap >= 0.6:
        return "Wide"
    return "Moderate"


def _evidence_tile(label, value, note):
    """HTML of one evidence tile: label, headline value and a one-sentence explanation."""
    return (f"<div class='ev-tile'><div class='k-label'>{label}</div>"
            f"<div class='ev-value'>{value}</div><div class='k-sub'>{note}</div></div>")


def _keyword_chips(keywords, shared_terms):
    """Key phrases as chips, highlighting any phrase containing a shared token."""
    shared = {t.lower() for t in shared_terms}
    chips = []
    for k in keywords:
        hit = any(tok in shared for tok in str(k).lower().split())
        cls = "term-chip shared" if hit else "term-chip"
        chips.append(f"<span class='{cls}'>{html_escape(str(k))}</span>")
    return "".join(chips) or "<span class='k-sub'>No keywords extracted</span>"


def render_connections_tab(bridges, metrics):
    """
    Renders one evidence card per bridge: both posts, the explanation, four evidence
    tiles and the shared vocabulary.

    Args:
        bridges (list[dict]): Ranked findings from detect_emerging_bridges().
        metrics (dict): Graph metrics; supplies the cross-platform edge threshold.
    """
    st.markdown("### Emerging connections — and the evidence behind them")
    st.markdown(
        "<p class='card-sub'>Pairs of posts from <b>different communities</b> that are "
        "about the same idea, even though nothing else links them. That kind of quiet "
        "overlap is often the first sign an idea is spreading. Every number below comes "
        "straight from our model, so you can see exactly why each pair was flagged.</p>",
        unsafe_allow_html=True)

    if not bridges:
        st.info("No emerging connections stood out in this scan. Try a broader "
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
                        f"<div class='t'>{html_escape(shorten(b[f'{side}_title'], 120))}</div>"
                        f"<div class='section-label'>Extracted keywords</div>"
                        f"{_keyword_chips(b[f'{side}_keywords'], b['shared_terms'])}"
                        f"</div>", unsafe_allow_html=True)
                    url = safe_url(b[f'{side}_url'])
                    if url:
                        st.link_button(f"Open on {plat}  ↗", url, width="stretch")

            reg_a = REGISTER_LABEL.get(b['source_platform'], b['source_platform'])
            reg_b = REGISTER_LABEL.get(b['target_platform'], b['target_platform'])
            shared = "".join(f"<span class='term-chip shared'>{html_escape(str(t))}</span>"
                             for t in b['shared_terms'])
            tiles = "".join([
                _evidence_tile("Topic Match", f"{sim * 100:.0f}%",
                               f"How closely the two posts discuss the same idea "
                               f"(we only link pairs at {link_cutoff * 100:.0f}% or higher)."),
                _evidence_tile("Link Uniqueness",
                               f"{(1 - b['neighbour_overlap']) * 100:.0f}%",
                               "How rare this link is: 100% means no other posts tie "
                               "these two together."),
                _evidence_tile("Community Bridge", _bridge_width(b['register_gap']),
                               f"Connects {html_escape(reg_a or '')} with "
                               f"{html_escape(reg_b or '')}; the further apart the "
                               f"communities, the more notable the link."),
                _evidence_tile("Conversation Circles",
                               "Different" if b['cross_community'] else "Same",
                               "Whether the posts sit in separate circles of the "
                               "conversation; different means the idea is crossing over."),
            ])
            st.markdown(
                "<div class='section-label'>Connection evidence</div>"
                f"<div class='ev-grid'>{tiles}</div>"
                "<div class='section-label'>Shared keywords</div>"
                + (shared or "<span class='k-sub'>No distinctive keyword in common — "
                             "matched on overall meaning only.</span>"),
                unsafe_allow_html=True)
