"""
Shared bootstrap for Arbiter's multipage app -- backend selection, the
retry-wrapped connection helper, and the branded header -- everything
app.py (the router), dashboard_page.py, and admin_page.py all need in
common.

This is deliberately its OWN module, not something the page files
import from app.py directly. app.py is the script Streamlit actually
runs (`streamlit run app.py`), so it executes with __name__ ==
"__main__" -- a plain `import app` from another file would load it a
SECOND time as a distinct module object, re-running every top-level
statement in it, including st.set_page_config(), which raises
(`StreamlitAPIException: set_page_config() can only be called once per
app`) the moment it runs twice in the same script execution. Pulling
the shared, side-effect-free pieces out into this module instead means
app.py, dashboard_page.py, and admin_page.py can all import it safely,
with nothing in here ever executing more than once.

Nothing in this file calls st.set_page_config, injects CSS, or renders
the auth screen -- those stay in app.py itself, since they're real
page-level actions that must happen exactly once, in the actual entry
point.
"""
import os
import sqlite3
import time
from contextlib import closing

import streamlit as st

# --- Backend selection: local SQLite (fast iteration) vs Azure SQL (cloud
# demo). USE_AZURE_DB=true switches both the agent's tool backend and the
# sidebar's live ERP view. Defaults to local SQLite. If Azure is requested
# but agent_engine_azure can't be imported (missing env vars, bad driver,
# etc.), we don't crash -- DB_INIT_ERROR is surfaced in the UI instead, and
# run_agent/commit_order_modification are left as None so any accidental
# call fails loudly rather than silently doing nothing.
USE_AZURE_DB = os.environ.get("USE_AZURE_DB") == "true"
DB_INIT_ERROR = None
get_azure_connection = None

if USE_AZURE_DB:
    DB_MODE = "Azure SQL"
    try:
        from agent_engine_azure import (
            run_agent,
            commit_order_modification,
            get_connection as get_azure_connection,
        )
    except KeyError as e:
        DB_INIT_ERROR = f"Missing environment variable: {e.args[0]}"
        run_agent = commit_order_modification = None
    except Exception as e:
        DB_INIT_ERROR = str(e)
        run_agent = commit_order_modification = None
else:
    DB_MODE = "Local SQLite"
    from agent_engine import run_agent, commit_order_modification


def _sqlite_connection():
    return sqlite3.connect("mock_erp.db")


# auth.py's users/login_codes/sessions tables (and rbac.py's role_
# permissions/audit_log tables) live in the same database as the ERP
# tables, so everything in this app switches backends together via
# USE_AZURE_DB, through this one connection function.
_get_raw_connection = get_azure_connection if USE_AZURE_DB else _sqlite_connection

# Every connection anywhere in this app goes through auth_get_connection()
# -- retrying transient failures here means every call site gets that
# retry automatically, with no per-call-site opt-in required. Same retry
# shape (5 attempts, 1.5s apart) used throughout this project's Azure SQL
# startup/retry paths.
CONNECT_MAX_ATTEMPTS = 5
CONNECT_RETRY_DELAY_S = 1.5


def auth_get_connection():
    last_error = None
    for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
        try:
            return _get_raw_connection()
        except Exception as e:
            last_error = e
            if attempt < CONNECT_MAX_ATTEMPTS:
                time.sleep(CONNECT_RETRY_DELAY_S)
    raise last_error


# ---------------------------------------------------------------------
# Branded header -- single source of truth, shared by every page
# ---------------------------------------------------------------------

ARBITER_HEADER_ICON_SVG_PATH = "assets/arbiter_icon_header.svg"


def _load_svg(path: str) -> str:
    """Reads an SVG asset's raw markup for inline embedding via
    st.markdown(..., unsafe_allow_html=True). Returns an empty string
    if the file is missing, so a moved/renamed asset degrades to no
    logo rather than crashing the page -- but prints a loud warning
    first (a silent empty-string return was hard to distinguish from
    "this is just how the header looks" when the file genuinely failed
    to load, e.g. a case-sensitivity mismatch that only shows up on
    Linux, not on a Windows dev machine, since NTFS is case-
    insensitive)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"WARNING: could not load SVG asset at {path!r} (file not found) -- header will render without its icon.", flush=True)
        return ""


def render_app_header() -> str:
    """The SINGLE SOURCE OF TRUTH for the branded header lockup (icon +
    'Arbiter' wordmark) -- every page that shows this header (the auth
    screen's error states in app.py, the dashboard, the admin page)
    MUST call this function rather than writing its own markup. See
    app.py's module docstring history for why this consolidation
    matters -- a header written out separately per call site is exactly
    what let the same "wrong/missing header" bug resurface more than
    once before this existed."""
    icon_svg = _load_svg(ARBITER_HEADER_ICON_SVG_PATH)
    icon_html = f'<span class="app-header-icon">{icon_svg}</span>' if icon_svg else ""
    return f"""
    <div class="app-header-logo-row">
        {icon_html}
        <span class="app-header-wordmark">Arbiter</span>
    </div>
    """


# ---------------------------------------------------------------------
# Cached session validation -- shared so both app.py (which calls it on
# every page load, before routing) and dashboard_page.py (whose Logout
# button needs to evict a stale cached result for the token it just
# deleted) reference the SAME cached function object. Defined here, not
# in app.py, specifically so dashboard_page.py can reach it via a plain
# `import app_core` -- importing app.py itself from another module would
# re-execute it as a second, distinct module (Streamlit runs it as
# __main__, which a later `import app` does not find in sys.modules
# under that name), re-running st.set_page_config() a second time and
# crashing. See this module's own docstring.
SESSION_VALIDATION_TTL_S = 20


@st.cache_data(ttl=SESSION_VALIDATION_TTL_S, show_spinner="Loading...")
def _validate_session_cached(session_token: str) -> dict:
    import auth
    with closing(auth_get_connection()) as conn:
        return auth.validate_session(conn, session_token)


def db_mode_badge_html(mode: str, has_error: bool) -> str:
    """Small stamp badge for the header showing whether the backend is
    reachable. Deliberately doesn't name the actual backend (Azure SQL /
    local SQLite) -- that's an internal infrastructure detail, not
    something an end user needs to see; `mode` is still accepted so
    callers don't need to change, it's just no longer rendered into the
    visible label."""
    variant = "stamp-danger" if has_error else "stamp-info"
    label = "CONNECTION ERROR" if has_error else "CONNECTED"
    return f'<span class="stamp stamp-compact {variant}">{label}</span>'
