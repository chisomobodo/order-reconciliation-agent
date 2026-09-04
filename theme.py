"""
Design system for the Arbiter dashboard.

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

    /* Spacing scale -- every section/card gap across every tab should
       come from one of these, not an ad-hoc pixel value, so the
       vertical rhythm reads the same regardless of which tab you're
       looking at. */
    --space-xs: 4px;
    --space-sm: 8px;
    --space-md: 16px;
    --space-lg: 24px;
    --space-xl: 32px;
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

/* Main header logo lockup: assets/arbiter_icon_header.svg (the mark
   alone -- transparent background, no <title>/<desc>, so no unwanted
   hover tooltip or background box) next to a real HTML wordmark, not
   text baked into the SVG. Left-aligned as a row (justify-content:
   flex-start), not centered -- sits toward the left of the header. */
.app-header-logo-row {{
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    animation: fadeSlideDown 0.5s ease-out;
}}
.app-header-icon {{
    display: inline-flex;
    width: 52px;
    flex-shrink: 0;
}}
.app-header-icon svg {{ display: block; width: 100%; height: auto; }}
.app-header-wordmark {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 1.9rem;
    letter-spacing: 0.02em;
    color: var(--text);
}}

/* Header row (logo, auto-approve toggle, theme toggle, user/logout) --
   st.columns' default behavior is to keep every column on one row and
   shrink-to-fit as the viewport narrows, with no minimum -- there's no
   "wrap onto a second row" until Streamlit's own much-narrower internal
   breakpoint stacks everything into a single column. At in-between
   widths (a tablet, or even a 1024px desktop once the sidebar eats a
   few hundred px of it) that shrinking squeezed short button/label text
   down to one character per line ("Lo" / "g" / "out") instead of ever
   wrapping normally -- confirmed via real screenshots at 768px and
   1024px. flex-wrap lets a column that no longer fits drop to the next
   line instead of continuing to shrink past its content's natural
   width; min-width keeps each column wide enough that ITS OWN content
   doesn't need to squeeze in the first place. Scoped to the header's
   own row (and the "Logged in as X" / "Log out" row nested inside it)
   via :has(.app-header-logo-row) so this never touches the many other
   st.columns() layouts elsewhere in the app. */
div[data-testid="stHorizontalBlock"]:has(.app-header-logo-row),
div[data-testid="stHorizontalBlock"]:has(.app-header-logo-row) div[data-testid="stHorizontalBlock"] {{
    flex-wrap: wrap !important;
    row-gap: var(--space-sm);
}}
div[data-testid="stHorizontalBlock"]:has(.app-header-logo-row) > div[data-testid="stColumn"] {{
    min-width: 200px !important;
}}
div[data-testid="stHorizontalBlock"]:has(.app-header-logo-row) .stButton button {{
    white-space: nowrap;
}}
/* When a column ends up alone on its own wrapped line (e.g. "Logged in
   as X" wrapping above the "Log out" button once the viewport is narrow
   enough that even the nested row's own min-width no longer fits both
   side by side), Streamlit's own height calculation for that column
   stayed sized for its ORIGINAL side-by-side flex-basis instead of its
   actual (padding-top-inclusive) content height -- confirmed via direct
   measurement: an 11px-tall column box containing a padding-top:8px
   text line. The text still rendered, just partly past its own box's
   bottom edge, visually overlapping the button wrapped below it.
   Forcing auto height here lets each wrapped column size to its real
   content instead of a stale pre-wrap value. */
div[data-testid="stHorizontalBlock"]:has(.app-header-logo-row) div[data-testid="stColumn"] {{
    height: auto !important;
}}

/* --- Section heading: the ONE style for every top-level named section
   inside a tab (e.g. "Agent Execution", "Pending Outbound Emails",
   "Awaiting Customer Reply", "Sent Emails", "Processed Emails") --
   replaces a previous mix of st.header/st.subheader/bold-markdown that
   each rendered with different font/size/weight depending on which tab
   they happened to be added in. Deliberately icon-free (an earlier pass
   had emoji on some section headings but not others -- inconsistent,
   and a status is already communicated by the .stamp badges, not by a
   decorative icon in the heading). */
.section-heading {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 1.25rem;
    letter-spacing: 0.02em;
    color: var(--text);
    margin: var(--space-sm) 0 var(--space-sm) 0;
    animation: fadeSlideDown 0.4s ease-out;
}}

/* --- Field label: the smaller, dimmer label for a sub-field WITHIN one
   section/result (e.g. "Tool Call Chain" / "Outcome" / "Reply to
   Customer" / "Database Write" are all sub-fields of one Agent
   Execution result, not independent sections of their own) -- reuses
   the same IBM Plex Mono uppercase treatment .step-card .step-label
   already established, instead of inventing a third label style. */
.field-label {{
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim);
    margin: var(--space-md) 0 var(--space-xs) 0;
}}

/* --- Empty state: a designed "nothing here" state, not a bare caption
   floating in blank space -- used everywhere a list/section can be
   empty (no pending emails, nothing on hold, etc.) so absence of
   content still reads as an intentional, finished part of the UI. */
.empty-state {{
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: var(--space-md);
    text-align: center;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.88rem;
    color: var(--text-dim);
    animation: fadeSlideUp 0.4s ease-out;
}}

/* --- Compact stamp variant: same badge, smaller/rotated for tight
   header contexts -- was a one-off inline style before, now a real
   class so it's defined in one place instead of duplicated per call
   site. */
.stamp-compact {{
    font-size: 0.7rem;
    padding: 4px 12px;
    transform: rotate(-2deg);
}}

/* --- Native Streamlit alert boxes (st.info/st.warning/st.error/
   st.success) and expanders -- restyled to match the manifest surface/
   border treatment instead of standing out as unstyled default
   Streamlit chrome next to the custom cards around them. The kind-
   specific tint (blue for info, red for error, etc.) actually lives on
   a NESTED stAlertContainer div with its own translucent background,
   not on the outer stAlert -- confirmed by inspecting the live DOM,
   since overriding only the outer container left that inner tint
   showing through as a mismatched color patch. Neutralized here so
   every alert reads as one plain surface-alt card regardless of kind;
   Streamlit's icon (info/warning/error glyph) is left alone, since
   that alone still carries the semantic signal. */
[data-testid="stAlert"] {{
    background: var(--surface-alt) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
}}
[data-testid="stAlert"] [data-testid="stAlertContainer"] {{
    background: transparent !important;
}}
[data-testid="stAlert"] p {{
    font-family: 'IBM Plex Sans', sans-serif !important;
    color: var(--text) !important;
}}
[data-testid="stExpander"] {{
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    background: var(--surface) !important;
}}

/* Links (e.g. a customer's email address auto-linkified inside a
   reply-card) default to the browser's blue -- recolored to the
   accent amber so they read as part of the theme, not a foreign
   element. */
.stApp a {{
    color: var(--accent) !important;
}}

/* Tab strip -- Streamlit's default active-tab color/underline is red
   (its own built-in primaryColor), clashing with the amber accent used
   everywhere else. Confirmed via the live DOM: the tab label itself is
   [data-testid="stTab"], and the moving underline is a sibling
   .react-aria-SelectionIndicator, not a border on the tab itself. */
[data-testid="stTab"] {{
    color: var(--text-dim) !important;
    transition: color 0.2s ease;
}}
[data-testid="stTab"][aria-selected="true"] {{
    color: var(--accent) !important;
}}
.react-aria-SelectionIndicator {{
    background: var(--accent) !important;
    transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1), width 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}}

/* Tab panel entrance -- confirmed via the frontend bundle that
   switching tabs never unmounts anything (all panels are
   shouldForceMount:true); the inactive ones just sit at display:none.
   Browsers restart an element's CSS animation whenever it re-enters
   the render tree from display:none, so giving the panel itself an
   animation is enough to make every tab switch (not just first page
   load) fade/slide the whole tab's content in -- on top of the
   step-card/stamp/reply-card/etc. animations already firing the same
   way for the same reason. */
[data-testid="stTabPanel"] {{
    animation: tabPanelIn 0.35s ease-out;
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

/* Primary button -> manifest "process" stamp button. A button inside
   st.form() (e.g. "Send login code", "Verify code", "Create account")
   renders with kind="primaryFormSubmit" instead of plain "primary" --
   confirmed via the live DOM -- so every form-submit button was falling
   through to Streamlit's unstyled default (red) instead of picking up
   this rule at all. Both kind values are matched here so any primary
   button reads the same regardless of whether it lives inside a form. */
.stButton button[kind="primary"], .stButton button[kind="primaryFormSubmit"] {{
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
.stButton button[kind="primary"]:hover, .stButton button[kind="primaryFormSubmit"]:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 10px rgba(0,0,0,0.25);
}}
.stButton button[kind="primary"]:active, .stButton button[kind="primaryFormSubmit"]:active {{
    transform: translateY(0px);
    box-shadow: 0 1px 0 rgba(0,0,0,0.25);
}}

/* Theme toggle button (secondary) -- same kind-name mismatch as above
   applies to secondary form-submit buttons (e.g. "Start over"). */
.stButton button[kind="secondary"], .stButton button[kind="secondaryFormSubmit"] {{
    border: 1px solid var(--border) !important;
    background: var(--surface) !important;
    color: var(--text) !important;
    border-radius: 20px !important;
    font-size: 0.85rem;
}}

/* Touch target minimum (~44px, the standard comfortable tap-target
   floor) for every button -- Streamlit's default button height is
   comfortably above this on desktop, but the compact secondary/toggle
   buttons could otherwise end up shorter once padding is squeezed by
   the header's responsive wrapping above. */
.stButton button {{
    min-height: 44px;
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
@keyframes tabPanelIn {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}

@media (prefers-reduced-motion: reduce) {{
    .step-card, .stamp, .reply-card, .app-header-logo-row,
    .section-heading, .empty-state, [data-testid="stTabPanel"] {{
        animation: none !important;
        opacity: 1 !important;
        transform: none !important;
    }}
    [data-testid="stTab"], .react-aria-SelectionIndicator {{
        transition: none !important;
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


def email_card_html(index: int, sender: str, subject: str, preview: str, delay_s: float) -> str:
    """Same .step-card visual styling as step_card_html, adapted for
    listing a fetched-but-not-yet-processed inbox email instead of a
    tool call. Callers must HTML-escape sender/subject/preview first --
    unlike step_card_html's fields, these come from real external email
    content, not from the agent's own structured output."""
    return f"""
    <div class="step-card" style="animation-delay: {delay_s}s;">
        <div class="step-label">Email {index} · From {sender}</div>
        <div class="step-tool">{subject}</div>
        <div class="step-detail">{preview}</div>
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
        "Awaiting Reply": "stamp-info",
        "Past Follow-Up": "stamp-danger",
        "Needs Manual Linking": "stamp-warn",
    }.get(status, "stamp-info")
    return f'<span class="stamp {variant}">{status}</span>'


def section_heading_html(text: str) -> str:
    """The one heading style for every top-level named section inside a
    tab (e.g. "Agent Execution", "Pending Outbound Emails", "Awaiting
    Customer Reply") -- use this instead of st.header/st.subheader/bold
    markdown so every section heading across every tab matches, rather
    than each rendering with whatever style happened to be reached for
    when that section was added."""
    return f'<div class="section-heading">{text}</div>'


def field_label_html(text: str) -> str:
    """The smaller, dimmer label for a sub-field within one section/
    result (e.g. "Tool Call Chain" / "Outcome" / "Reply to Customer" /
    "Database Write" are all sub-fields of one Agent Execution result,
    not independent sections of their own)."""
    return f'<div class="field-label">{text}</div>'


def empty_state_html(text: str) -> str:
    """A designed 'nothing here' state for any list/section that can be
    empty -- used in place of a bare st.caption() so absence of content
    still reads as an intentional, finished part of the UI rather than
    a blank gap."""
    return f'<div class="empty-state">{text}</div>'