"""
Visual design for the login/signup screen.

Layout borrows the reference template's structure (split hero panel +
form card, floating-label inputs, pill buttons, tabbed login/signup
toggle) but restyled entirely in the app's existing manifest/stamp
palette (see theme.py) rather than a different color scheme -- so the
auth screen feels like part of the same product, not a separate one.

Real form inputs (email, password, etc.) must stay actual Streamlit
widgets to capture values -- this module restyles them via CSS rather
than replacing them with plain HTML.
"""

ARBITER_ICON_SVG_PATH = "assets/arbiter_icon_only.svg"


def _load_svg(path: str) -> str:
    """Reads an SVG asset's raw markup for inline embedding via
    st.markdown(..., unsafe_allow_html=True). Returns an empty string
    if the file is missing, so a moved/renamed asset degrades to no
    logo rather than crashing the auth screen."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


AUTH_FONTS_IMPORT = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
"""


def build_auth_css(theme_name: str) -> str:
    """Reuses the same --bg/--surface/--accent CSS variables theme.py
    already defines (call build_css() first, this just adds auth-screen-
    specific rules on top)."""
    return f"""
{AUTH_FONTS_IMPORT}
<style>
/* Hide default Streamlit chrome on the auth screen for a cleaner look */
[data-testid="stSidebar"] {{ display: none; }}

/* --- Split hero/form layout ---
   Styled directly on Streamlit's OWN column containers (confirmed
   testids: stHorizontalBlock wraps the row, stColumn is each column)
   rather than a hand-written wrapper <div> opened/closed around them via
   separate st.markdown() calls -- that doesn't actually nest real
   Streamlit elements inside hand-written HTML, and was collapsing the
   whole screen.

   Scoped with :has(.auth-hero) rather than the bare testid: the auth
   screen's own code-entry step used to nest a second, smaller
   st.columns() inside the form for its two buttons, and the bare
   selector styled THAT row identically (min-height: 640px, the padded
   panel background, etc.) -- producing a huge stretched empty box with
   the second button shoved into it. :has(.auth-hero) only matches the
   ONE outer row that actually contains the hero panel, so any column
   layout nested inside the form (now removed, but this also guards
   against a future one) is left alone. */
div[data-testid="stHorizontalBlock"]:has(.auth-hero) {{
    border-radius: 16px;
    overflow: hidden;
    border: 1px solid var(--border);
    box-shadow: 0 20px 60px rgba(0,0,0,0.35);
    animation: authFadeIn 0.6s ease-out;
    align-items: stretch;
}}
div[data-testid="stHorizontalBlock"]:has(.auth-hero) > div[data-testid="stColumn"] {{
    min-height: 640px;
}}
div[data-testid="stHorizontalBlock"]:has(.auth-hero) > div[data-testid="stColumn"]:nth-of-type(2) {{
    background: var(--bg);
    padding: 48px 44px;
    display: flex;
    flex-direction: column;
    justify-content: center;
}}

/* --- Left hero panel --- */
.auth-hero {{
    flex: 1;
    height: 100%;
    background:
        radial-gradient(circle at 20% 20%, rgba(232,162,61,0.18), transparent 45%),
        radial-gradient(circle at 80% 80%, rgba(79,174,140,0.14), transparent 45%),
        var(--surface);
    padding: 48px 40px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    position: relative;
    overflow: hidden;
}}
.auth-hero::before {{
    content: "";
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(var(--border) 1px, transparent 1px),
        linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 34px 34px;
    opacity: 0.25;
    animation: gridDrift 40s linear infinite;
}}
.auth-hero-logo {{
    width: 44px;
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 16px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.25);
    z-index: 1;
    animation: authSlideUp 0.7s ease-out 0s both;
}}
.auth-hero-logo svg {{ display: block; }}
.auth-hero-tag {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--accent);
    z-index: 1;
    animation: authSlideUp 0.7s ease-out 0.1s both;
}}
.auth-hero-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 2.3rem;
    line-height: 1.15;
    color: var(--text);
    margin-top: 14px;
    z-index: 1;
    animation: authSlideUp 0.7s ease-out 0.22s both;
}}
.auth-hero-title .accent {{ color: var(--accent); }}
.auth-hero-sub {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.92rem;
    color: var(--text-dim);
    margin-top: 14px;
    max-width: 320px;
    line-height: 1.55;
    z-index: 1;
    animation: authSlideUp 0.7s ease-out 0.34s both;
}}
.auth-hero-stamp {{
    align-self: flex-start;
    z-index: 1;
    animation: authStampIn 0.6s cubic-bezier(0.34,1.56,0.64,1) 0.55s both;
}}

/* --- Right form panel --- */
.auth-form-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 1.6rem;
    color: var(--text);
    margin-bottom: 4px;
    animation: authSlideUp 0.5s ease-out 0.15s both;
}}
.auth-form-title .accent {{ color: var(--accent); }}
.auth-form-caption {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.85rem;
    color: var(--text-dim);
    margin-bottom: 22px;
    animation: authSlideUp 0.5s ease-out 0.22s both;
}}

/* --- Tabbed Login / Sign up toggle --- */
.auth-tabs {{
    display: flex;
    background: var(--surface-alt);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 4px;
    margin-bottom: 22px;
    animation: authSlideUp 0.5s ease-out 0.28s both;
}}
.auth-tabs button {{
    flex: 1;
    border: none;
    border-radius: 999px;
    padding: 8px 0;
    font-family: 'Oswald', sans-serif;
    font-size: 0.82rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    cursor: pointer;
    transition: all 0.25s ease;
}}

/* --- Restyle real Streamlit inputs into floating-label pill fields ---
   Streamlit's text input is a BaseWeb component: the visible bordered
   box is [data-testid="stTextInputRootElement"] (a wrapper around the
   real <input>, styled by BaseWeb with its own default straight-edged
   border), NOT the <input> itself. Putting the rounded border/focus
   ring only on the inner <input> (as before) left that default BaseWeb
   border showing through underneath, which read as a stray line/seam
   cutting through the rounded field. The border now lives on the root
   element instead, and the inner <input> is made borderless/
   transparent so there's only ever one visible edge. */
div[data-testid="stTextInput"] {{
    animation: authSlideUp 0.5s ease-out both;
}}
div[data-testid="stTextInput"] label {{
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.68rem !important;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim) !important;
}}
div[data-testid="stTextInputRootElement"] {{
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}}
div[data-testid="stTextInputRootElement"]:focus-within {{
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px rgba(232,162,61,0.15) !important;
}}
div[data-testid="stTextInput"] input {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: var(--text) !important;
    padding: 12px 16px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
}}

/* Streamlit's built-in "Press Enter to submit form" / "Press Enter to
   apply" hint -- unavoidable through the public API, so hidden via CSS
   instead. Confirmed testid from the installed frontend build. */
[data-testid="InputInstructions"] {{
    display: none;
}}

/* Primary pill button (reuse existing .stButton primary styling from
   theme.py, this just adds the pill radius + a subtle press animation) */
.stButton button[kind="primary"] {{
    border-radius: 999px !important;
    animation: authSlideUp 0.5s ease-out 0.4s both;
}}

/* Inline auth error / success messages */
.auth-message {{
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.85rem;
    padding: 10px 14px;
    border-radius: 8px;
    margin-bottom: 14px;
    animation: authShake 0.4s ease-out;
}}
.auth-message-error {{
    background: var(--danger-ink);
    color: var(--danger);
    border: 1px solid var(--danger);
}}
.auth-message-success {{
    background: var(--success-ink);
    color: var(--success);
    border: 1px solid var(--success);
}}

/* --- Animations --- */
@keyframes authFadeIn {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes authSlideUp {{
    from {{ opacity: 0; transform: translateY(14px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes authStampIn {{
    0%   {{ opacity: 0; transform: scale(1.6) rotate(-6deg); }}
    60%  {{ opacity: 1; transform: scale(0.96) rotate(-3deg); }}
    100% {{ opacity: 1; transform: scale(1) rotate(-3deg); }}
}}
@keyframes authShake {{
    0%, 100% {{ transform: translateX(0); }}
    25% {{ transform: translateX(-4px); }}
    75% {{ transform: translateX(4px); }}
}}
@keyframes gridDrift {{
    from {{ background-position: 0 0; }}
    to   {{ background-position: 34px 34px; }}
}}

@media (prefers-reduced-motion: reduce) {{
    div[data-testid="stHorizontalBlock"]:has(.auth-hero), .auth-hero-logo, .auth-hero-tag,
    .auth-hero-title, .auth-hero-sub, .auth-hero-stamp, .auth-form-title,
    .auth-form-caption, .auth-tabs, div[data-testid="stTextInput"],
    .stButton button[kind="primary"], .auth-message {{
        animation: none !important;
        opacity: 1 !important;
        transform: none !important;
    }}
    .auth-hero::before {{ animation: none !important; }}
}}

@media (max-width: 820px) {{
    div[data-testid="stHorizontalBlock"]:has(.auth-hero) {{ flex-direction: column; }}
    .auth-hero {{ padding: 32px 28px; }}
    div[data-testid="stHorizontalBlock"]:has(.auth-hero) > div[data-testid="stColumn"]:nth-of-type(2) {{
        padding: 32px 28px;
    }}
}}
</style>
"""


def hero_panel_html() -> str:
    """The decorative left panel. Pure HTML/CSS, no interactive elements."""
    icon_svg = _load_svg(ARBITER_ICON_SVG_PATH)
    logo_html = f'<div class="auth-hero-logo">{icon_svg}</div>' if icon_svg else ""
    return f"""
    <div class="auth-hero">
        <div>
            {logo_html}
            <div class="auth-hero-tag">Arbiter · Access</div>
            <div class="auth-hero-title">Every order,<br/><span class="accent">accounted for.</span></div>
            <div class="auth-hero-sub">
                Sign in to process live order-change requests, review pending
                approvals, and keep inventory reconciled — with an AI agent
                that asks before it guesses.
            </div>
        </div>
        <div class="auth-hero-stamp">
            <span class="stamp stamp-success">Secure Access</span>
        </div>
    </div>
    """


def auth_message_html(text: str, kind: str = "error") -> str:
    css_class = "auth-message-success" if kind == "success" else "auth-message-error"
    return f'<div class="auth-message {css_class}">{text}</div>'