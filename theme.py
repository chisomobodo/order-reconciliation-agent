"""
Design system for the Reconciliation Agent dashboard.

Concept: a shipping-manifest / dispatch-office aesthetic, not a generic
analytics dashboard skin. Order IDs and SKUs read like manifest codes
(monospace), status outcomes render as stamped badges (like a rubber
stamp on a paper manifest), and the tool-call chain reveals like items
moving down a conveyor -- one step appearing after another.

Two token sets (dark/light) sharing the same accent hues so the identity
holds across both modes.
"""

FONTS_IMPORT = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
"""

DARK = {
    "bg": "#14181C",
    "surface": "#1D2329",
    "surface_alt": "#242B32",
    "border": "#333C44",
    "text": "#E8E6E1",
    "text_dim": "#93A0AC",
    "accent": "#E8A23D",       # manifest-stamp amber
    "accent_ink": "#4A2E0A",
    "success": "#4FAE8C",
    "success_ink": "#0E2A22",
    "warn": "#E8A23D",
    "warn_ink": "#4A2E0A",
    "danger": "#D9615A",
    "danger_ink": "#3D1210",
    "info": "#6FA8D9",
    "info_ink": "#12283D",
}

LIGHT = {
    "bg": "#F2EFE6",
    "surface": "#FFFFFF",
    "surface_alt": "#FBF8F1",
    "border": "#DCD5C4",
    "text": "#26221C",
    "text_dim": "#6B6255",
    "accent": "#B9791F",
    "accent_ink": "#FCEFD8",
    "success": "#2F8367",
    "success_ink": "#E3F3ED",
    "warn": "#B9791F",
    "warn_ink": "#FCEFD8",
    "danger": "#B84540",
    "danger_ink": "#FBE7E5",
    "info": "#3E76A8",
    "info_ink": "#E5F0F9",
}


def build_css(theme_name: str) -> str:
    t = DARK if theme_name == "dark" else LIGHT

    return f"""
{FONTS_IMPORT}
<style>
:root {{
    --bg: {t['bg']};
    --surface: {t['surface']};
    --surface-alt: {t['surface_alt']};
    --border: {t['border']};
    --text: {t['text']};
    --text-dim: {t['text_dim']};
    --accent: {t['accent']};
    --accent-ink: {t['accent_ink']};
    --success: {t['success']};
    --success-ink: {t['success_ink']};
    --warn: {t['warn']};
    --warn-ink: {t['warn_ink']};
    --danger: {t['danger']};
    --danger-ink: {t['danger_ink']};
    --info: {t['info']};
    --info-ink: {t['info_ink']};
}}

.stApp {{
    background: var(--bg);
    color: var(--text);
}}

/* Header band */
[data-testid="stHeader"] {{
    background: transparent;
}}

h1, h2, h3 {{
    font-family: 'Oswald', sans-serif;
    letter-spacing: 0.02em;
    color: var(--text) !important;
}}

.manifest-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 2.1rem;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--text);
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    margin-bottom: 0;
    animation: fadeSlideDown 0.5s ease-out;
}}
.manifest-title .accent-bar {{
    display: inline-block;
    width: 10px;
    height: 32px;
    background: var(--accent);
    border-radius: 2px;
    margin-right: 4px;
}}
.manifest-sub {{
    font-family: 'IBM Plex Mono', monospace;
    color: var(--text-dim);
    font-size: 0.85rem;
    letter-spacing: 0.04em;
    margin-top: 2px;
    animation: fadeSlideDown 0.6s ease-out;
}}

/* Sidebar as a ledger */
[data-testid="stSidebar"] {{
    background: var(--surface);
    border-right: 1px solid var(--border);
}}
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{
    font-family: 'Oswald', sans-serif;
    font-size: 1rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim) !important;
    border-bottom: 1px dashed var(--border);
    padding-bottom: 6px;
}}

/* Data tables -> ledger rows, monospace */
[data-testid="stDataFrame"] {{
    font-family: 'IBM Plex Mono', monospace;
}}

/* Text areas / inputs */
.stTextArea textarea, .stSelectbox div[data-baseweb="select"] {{
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    font-family: 'IBM Plex Mono', monospace !important;
    border-radius: 6px !important;
}}

/* Primary button -> manifest "process" stamp button */
.stButton button[kind="primary"] {{
    background: var(--accent) !important;
    color: var(--accent-ink) !important;
    border: none !important;
    font-family: 'Oswald', sans-serif;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    font-weight: 600;
    border-radius: 6px;
    padding: 0.6rem 1.4rem;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    box-shadow: 0 2px 0 rgba(0,0,0,0.25);
}}
.stButton button[kind="primary"]:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 10px rgba(0,0,0,0.25);
}}
.stButton button[kind="primary"]:active {{
    transform: translateY(0px);
    box-shadow: 0 1px 0 rgba(0,0,0,0.25);
}}

/* Theme toggle button (secondary) */
.stButton button[kind="secondary"] {{
    border: 1px solid var(--border) !important;
    background: var(--surface) !important;
    color: var(--text) !important;
    border-radius: 20px !important;
    font-size: 0.85rem;
}}

/* --- Step cards: the conveyor-belt reveal for tool calls --- */
.step-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 10px;
    opacity: 0;
    transform: translateX(-16px);
    animation: slideInStep 0.45s ease-out forwards;
}}
.step-card .step-label {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim);
}}
.step-card .step-tool {{
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 1rem;
    color: var(--text);
    margin: 2px 0 8px 0;
}}
.step-card .step-detail {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.82rem;
    color: var(--text-dim);
    white-space: pre-wrap;
    word-break: break-word;
}}

/* --- Stamp badge: the signature element --- */
.stamp {{
    display: inline-block;
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 0.95rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 6px 16px;
    border: 3px solid currentColor;
    border-radius: 4px;
    transform: rotate(-3deg);
    animation: stampIn 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
    opacity: 0;
}}
.stamp-success {{ color: var(--success); background: var(--success-ink); }}
.stamp-warn    {{ color: var(--warn);    background: var(--warn-ink); }}
.stamp-danger  {{ color: var(--danger);  background: var(--danger-ink); }}
.stamp-info    {{ color: var(--info);    background: var(--info-ink); }}

/* --- Reply card --- */
.reply-card {{
    background: var(--surface-alt);
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 16px;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.95rem;
    line-height: 1.55;
    color: var(--text);
    white-space: pre-wrap;
    animation: fadeSlideUp 0.5s ease-out 0.15s both;
}}

/* --- Animations --- */
@keyframes fadeSlideDown {{
    from {{ opacity: 0; transform: translateY(-8px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes fadeSlideUp {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes slideInStep {{
    from {{ opacity: 0; transform: translateX(-16px); }}
    to   {{ opacity: 1; transform: translateX(0); }}
}}
@keyframes stampIn {{
    0%   {{ opacity: 0; transform: scale(1.8) rotate(-3deg); }}
    60%  {{ opacity: 1; transform: scale(0.95) rotate(-3deg); }}
    100% {{ opacity: 1; transform: scale(1) rotate(-3deg); }}
}}

@media (prefers-reduced-motion: reduce) {{
    .step-card, .stamp, .reply-card, .manifest-title, .manifest-sub {{
        animation: none !important;
        opacity: 1 !important;
        transform: none !important;
    }}
}}
</style>
"""


def step_card_html(index: int, tool_name: str, status: str, detail: str, delay_s: float) -> str:
    return f"""
    <div class="step-card" style="animation-delay: {delay_s}s;">
        <div class="step-label">Step {index}</div>
        <div class="step-tool">{tool_name}</div>
        <div class="step-detail">{detail}</div>
    </div>
    """


def stamp_html(status: str) -> str:
    variant = {
        "Success": "stamp-success",
        "Found": "stamp-info",
        "Not Found": "stamp-warn",
        "Rejected": "stamp-warn",
        "Needs Clarification": "stamp-warn",
        "Insufficient Stock": "stamp-danger",
        "Error": "stamp-danger",
        "No Action": "stamp-info",
        "Pending Approval": "stamp-warn",
        "Rejected by Reviewer": "stamp-danger",
    }.get(status, "stamp-info")
    return f'<span class="stamp {variant}">{status}</span>'