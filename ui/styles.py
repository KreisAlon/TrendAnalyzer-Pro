"""Visual theme of the dashboard: the slate palette and the global stylesheet."""

import streamlit as st

# Monochrome chrome: every hue is reserved for the platform colours.
INK = "#0f172a"
INK_SOFT = "#1e293b"
MUTED = "#64748b"
LINE = "#e5e7eb"


# Containers are styled via Streamlit's generated `st-key-<key>` classes.
# Braces are doubled for the f-string.
_STYLESHEET = f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    .stApp {{ background: #f6f7fb; color: {INK};
             font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .block-container {{ padding-top: 3.2rem; padding-bottom: 4rem; max-width: 1360px; }}
    h1, h2, h3, h4, h5 {{ font-family: 'Inter', sans-serif; letter-spacing: -0.02em; color: {INK}; }}

    /* Hide the developer chrome: hamburger menu, deploy button, footer, top bar. */
    #MainMenu, footer, [data-testid="stDecoration"], [data-testid="stToolbarActions"],
    [data-testid="stMainMenu"], [data-testid="stAppDeployButton"] {{ display: none !important; }}
    header[data-testid="stHeader"] {{ background: transparent; }}


    /* --- Hero ---------------------------------------------------------- */
    .hero {{
        background: radial-gradient(1200px 400px at 85% -20%, #334155 0%, transparent 60%),
                    linear-gradient(135deg, {INK} 0%, {INK_SOFT} 100%);
        border-radius: 22px; padding: 38px 44px 40px; color: #fff; position: relative;
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
        padding: 14px 16px; margin: 0; position: relative; z-index: 2;
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
    .st-key-wheel_card, .st-key-inspector, [class*="st-key-insight_"], .st-key-panel_card,
    .st-key-ranked_card {{
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
    .section-label {{ font-size: 0.76rem; font-weight: 700; letter-spacing: 0.07em;
                      text-transform: uppercase; color: {MUTED}; margin: 18px 0 8px; }}
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

    /* --- Gatekeeper's filter ----------------------------------------- */
    .kpi-grid.four {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    @media (max-width: 1000px) {{ .kpi-grid.four {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    .gate-row {{ display: grid; grid-template-columns: 150px 1fr 200px; gap: 14px;
                 align-items: center; margin: 10px 0; }}
    .gate-row .meter {{ display: flex; height: 14px; border-radius: 999px; overflow: hidden;
                        gap: 2px; background: #fff; }}
    .gate-row .meter .seg {{ height: 100%; }}
    .gate-row .nums {{ font-size: 0.86rem; color: {MUTED}; text-align: right; }}
    .gate-row .nums b {{ color: {INK}; }}
    .gate-steps {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px;
                   margin-bottom: 16px; }}
    @media (max-width: 1000px) {{ .gate-steps {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    .gate-step {{ background: #f8fafc; border: 1px solid #eef2f7; border-radius: 14px;
                  padding: 12px 14px; }}
    .gate-step .n {{ font-size: 0.74rem; font-weight: 700; letter-spacing: 0.07em;
                     text-transform: uppercase; color: {MUTED}; }}
    .gate-step .t {{ font-weight: 700; margin: 4px 0 2px; color: {INK}; }}
    .gate-step .d {{ font-size: 0.84rem; color: #475569; line-height: 1.45; }}

    /* --- Live trends (background worker) -------------------------------- */
    .live-head {{ display: flex; align-items: center; gap: 10px; margin: 22px 2px 10px; }}
    .live-head .title {{ font-size: 1.1rem; font-weight: 800; }}
    .live-chip {{ font-size: 0.74rem; font-weight: 600; color: #475569; background: #eef2f7;
                  border-radius: 999px; padding: 3px 10px; }}
    .live-chip.on {{ color: #047857; background: #d1fae5; }}
    /* Intelligence Brief sections (bordered containers keyed brief_*). */
    div[class*="st-key-brief_"] {{ background: #fff; border-radius: 14px;
                                   box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06); }}
    .brief-title {{ font-size: 1.02rem; font-weight: 800; margin: 0 0 6px; }}
    .brief-list {{ margin: 0; padding-left: 1.1rem; line-height: 1.75; font-size: 0.95rem; }}

    /* --- No results ---------------------------------------------------- */
    .no-results {{ background: #fff; border: 1px solid {LINE}; border-radius: 18px;
                   padding: 30px; text-align: center; margin: 8px 0 10px; }}
    .no-results .big {{ font-size: 2.2rem; }}
    .no-results .nr-title {{ font-size: 1.3rem; font-weight: 800; margin: 6px 0 6px; }}

</style>
"""


def inject_styles():
    """Injects the global stylesheet; called on every run, right after set_page_config."""
    st.markdown(_STYLESHEET, unsafe_allow_html=True)
