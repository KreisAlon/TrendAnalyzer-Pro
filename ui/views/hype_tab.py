"""Hype vs. Reality tab: volume of code (GitHub) versus discussion (other platforms)."""

from html import escape as html_escape

import streamlit as st

from graph_analyzer import PLATFORM_COLORS
from config import TARGET_POSTS_PER_PLATFORM
from data import load_scan_volume
from utils import platform_pill, shorten


# Stored counts are capped per platform (always a fixed 1:3 split), so the comparison
# uses each platform's estimated matching volume from scan_volume instead.
CODE_PLATFORMS = ("GitHub",)
TALK_PLATFORMS = ("Hacker News", "Dev.to", "Mastodon")

# Below this many matching items in total, the verdict is "Early Signal".
EARLY_SIGNAL_FLOOR = 30


def hype_vs_reality(df, volume):
    """
    Computes the code-versus-discussion comparison and its verdict.

    Measured volume is used only when every platform reported one; otherwise stored
    counts are used and flagged `measured=False`.
    """
    measured = bool(volume) and all(p in volume for p in CODE_PLATFORMS + TALK_PLATFORMS)
    if measured:
        counts = {p: int(volume[p]['estimated_matches']) for p in volume}
        # Totals that hit the API reporting ceiling (true count is higher).
        capped = {p for p, v in volume.items() if v.get('capped')}
    else:
        counts = df['source_platform'].value_counts().to_dict()
        capped = set()

    n_code = int(sum(counts.get(p, 0) for p in CODE_PLATFORMS))
    talk_counts = {p: int(counts.get(p, 0)) for p in TALK_PLATFORMS}
    n_talk = sum(talk_counts.values())
    total = n_code + n_talk
    code_share = n_code / total if total else 0.5

    # Verdict bands: too little data, code-led (>= 60%), talk-led (<= 30%), balanced.
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
    """Formats a count compactly ('1.2M', '3.4k', '57')."""
    n = float(n or 0)
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def _approx(n, is_capped=False):
    """Formats an estimate: '≈3.4k', or '1.0k+' when the count hit its reporting ceiling."""
    return f"{_compact(n)}+" if is_capped else f"≈{_compact(n)}"


def _volume_line(v):
    """How one platform's estimate was derived: found, unique share, gate pass rate."""
    return (f"{_compact(v['searched_total'])}{'+' if v.get('capped') else ''} found · "
            f"{v['overlap']:.0%} unique · {v['acceptance_rate']:.0%} passed our filter")


def render_hype_tab(df):
    """Renders the Hype vs. Reality view: verdict, split bar, Code/Talk cards, method note."""
    h = hype_vs_reality(df, load_scan_volume())

    def shown(n, is_capped):
        """'≈N'/'N+' for measured estimates, a plain integer for stored counts."""
        return _approx(n, is_capped) if h['measured'] else f"{n}"
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

    if h['n_talk'] and h['n_code']:
        per_ten = h['n_code'] / h['n_talk'] * 10
        ratio = (f"{per_ten:,.0f} active code project{'s' if round(per_ten) != 1 else ''} "
                 f"for every 10 discussions" if per_ten >= 1 else
                 f"1 active code project for every {h['n_talk'] / h['n_code']:,.0f} "
                 f"discussions")
    elif h['n_talk']:
        ratio = "No active code found yet — only discussion."
    elif h['n_code']:
        ratio = "No discussion found yet — only code."
    else:
        ratio = "No code or discussion found yet."

    # Code in GitHub's hue, talk split by community, matching colours in other views.
    code_w = h['code_share'] * 100
    talk_total = h['n_talk'] or 1
    segments = [f"<div class='seg' title='GitHub · {h['n_code']:,}' "
                f"style='width:{code_w:.1f}%;background:{PLATFORM_COLORS['GitHub']}'></div>"]
    for p in TALK_PLATFORMS:
        w = (100 - code_w) * h['talk_counts'][p] / talk_total
        # Zero-width segments are omitted so the 2px gaps stay even.
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
        top_line = (f"Most-starred: <b>{html_escape(shorten(top['title'], 50))}</b> "
                    f"(⭐ {_compact(top['raw_score'])})" if top is not None else "—")
        how = (f"<div class='k-sub'>{_volume_line(vol['GitHub'])}</div>"
               if 'GitHub' in vol else "")
        count = shown(h['n_code'], 'GitHub' in h['capped'])
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
            detail = (f"<div class='k-sub'>{_volume_line(vol[p])}</div>" if p in vol else "")
            rows.append(f"<div class='talk-row'>{platform_pill(p)}"
                        f"<b>{shown(n, p in h['capped'])}</b></div>{detail}")
        # The aggregate is marked '+' if any constituent platform was capped.
        total = shown(h['n_talk'], bool(h['capped'] & set(TALK_PLATFORMS)))
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
