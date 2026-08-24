"""
Interview demo dashboard for the Reconciliation Agent portfolio project.

Design concept: a shipping-manifest / dispatch-office aesthetic (see
theme.py) -- monospace order/SKU codes, stamped status badges, and a
conveyor-style staggered reveal for the tool-call chain, instead of a
generic analytics-dashboard skin.

Run with: streamlit run app.py
"""
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import extra_streamlit_components as stx
from extra_streamlit_components.CookieManager import _component_func as _cookie_component
import pandas as pd
import sqlite3
import streamlit as st

import auth
import auth_theme
import email_ingestion
import outbound_email
from theme import build_css, email_card_html, step_card_html, stamp_html

st.set_page_config(layout="wide", page_title="Reconciliation Agent", page_icon="📦")

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


# auth.py's users/login_codes/sessions tables live in the same database as
# the ERP tables, so auth switches backends together with USE_AZURE_DB via
# the same get_connection() pattern agent_engine[_azure].py already use.
# None when Azure was requested but couldn't be reached (DB_INIT_ERROR is
# set) -- that's handled before anything tries to call it, below.
auth_get_connection = get_azure_connection if USE_AZURE_DB else _sqlite_connection

# --- Theme state ---
if "theme" not in st.session_state:
    st.session_state.theme = "dark"

# --- Human-approval state ---
# pending_approval holds the proposed change (order_id, mapped_sku,
# requested_quantity) awaiting a manual decision; None when nothing is
# waiting. last_commit_result holds the outcome of the most recent
# commit/reject action, so it stays visible across reruns instead of
# flashing and disappearing.
if "pending_approval" not in st.session_state:
    st.session_state.pending_approval = None
if "last_commit_result" not in st.session_state:
    st.session_state.last_commit_result = None

# --- Inbox-ingestion state ---
# Fetched-but-unprocessed emails now live in the pending_emails DB table
# (see email_ingestion.sync_fetched_emails/get_pending_emails), not
# session state -- queried fresh on every render, so the pending list is
# correct regardless of which browser session fetched an email and
# survives a page refresh. Only the fetch-time error itself is transient
# session state (it needs to survive the st.rerun() right after a failed
# "Check Inbox" click, but has no reason to persist beyond that).
# pending_approval_queue is a LIST rather than the single pending_approval
# slot the manual-paste flow uses above -- several ingested emails can
# each independently need a human decision, and overwriting a single slot
# per new item would silently drop the earlier ones without ever letting
# a human review them. Rendered one at a time (same card/stamp styling as
# the single-item flow), popped as each is decided.
if "fetch_error" not in st.session_state:
    st.session_state.fetch_error = None
if "pending_approval_queue" not in st.session_state:
    st.session_state.pending_approval_queue = []

st.markdown(build_css(st.session_state.theme), unsafe_allow_html=True)

# --- Auth gate ---
# Nothing below this block renders until there's a valid session. If the
# backend itself couldn't be reached (DB_INIT_ERROR), there's no usable
# connection for auth OR the app, so that error is shown instead of a
# login form that would just fail on every attempt.
if DB_INIT_ERROR:
    st.markdown(
        '<div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>',
        unsafe_allow_html=True,
    )
    st.error(
        f"USE_AZURE_DB is set, but the app couldn't connect to Azure SQL: {DB_INIT_ERROR}\n\n"
        "Check that AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, and "
        "AZURE_SQL_PASSWORD are set correctly, then restart the app. Or set "
        "USE_AZURE_DB=false (or unset it) to fall back to the local SQLite backend."
    )
    st.stop()


@st.cache_resource
def _ensure_auth_schema():
    """Runs once for the life of the process (st.cache_resource, not
    cache_data -- this is setup, not data), alongside the ERP tables in
    the same database."""
    auth.init_auth_schema(auth_get_connection, is_azure=USE_AZURE_DB)
    return True


@st.cache_resource
def _ensure_email_tracking_schema():
    """Same pattern as _ensure_auth_schema -- pending_emails/
    processed_emails live in the same database, created once per process."""
    email_ingestion.init_email_tracking_schema(auth_get_connection, is_azure=USE_AZURE_DB)
    return True


@st.cache_resource
def _ensure_outbound_email_schema():
    """Same pattern again -- outbound_emails (the send-approval queue)
    lives in the same database, created once per process."""
    outbound_email.init_outbound_email_schema(auth_get_connection, is_azure=USE_AZURE_DB)
    return True


_ensure_auth_schema()
_ensure_email_tracking_schema()
_ensure_outbound_email_schema()

# --- Session cookie: read side ---
# CookieManager.get()/get_all() hardcode default={} internally (confirmed
# by reading the installed library's source), so they can NEVER return
# None -- there is no way, through that public API, to tell "the browser
# round-trip for this cookie hasn't completed yet" (which should also
# look like {} on the very first pass) apart from "it completed, and
# there genuinely is no cookie" (also {}). That ambiguity was the actual
# bug: a hard refresh could see the not-yet-loaded default and wrongly
# conclude "no session".
#
# We read the SAME underlying component function CookieManager wraps,
# but with our own sentinel default (None) instead, so the two cases are
# distinguishable. We deliberately do NOT also construct a
# stx.CookieManager() here for reads -- its __init__ makes its own
# get_all() call as a side effect, which would mount a second, redundant
# cookie-reading component alongside this one on every page load, adding
# unnecessary async race surface during exactly the window we're trying
# to make reliable. CookieManager is only instantiated later, transiently,
# at the point .set() is actually needed (login success) -- see below.
COOKIE_PROBE_MAX_ATTEMPTS = 6
COOKIE_PROBE_RETRY_DELAY_S = 0.35  # ~2.1s worst case across all retries

if "cookie_probe_attempts" not in st.session_state:
    st.session_state.cookie_probe_attempts = 0
if "just_logged_out" not in st.session_state:
    st.session_state.just_logged_out = False

if st.session_state.get("session_token"):
    # This Python session already knows its own token (e.g. right after a
    # login in this very tab) -- no need to wait on the cookie round-trip.
    _session_token = st.session_state.session_token
elif st.session_state.just_logged_out:
    # We just tore this session down ourselves (see the Logout button) --
    # don't even ask the cookie, and don't run validate_session against a
    # token we know is already gone. This is what stops the logout click
    # from being able to briefly show a stale/invalid-session error.
    _session_token = None
else:
    _raw_cookies = _cookie_component(method="getAll", key="auth_cookie_manager", default=None)

    if _raw_cookies is None:
        # Not resolved yet on this pass. IMPORTANT: st.rerun() called
        # from inside the script is purely server-side -- it does NOT
        # wait on any round-trip to the browser, so calling it
        # immediately (as an earlier version of this fix did) gives the
        # component's iframe essentially zero real wall-clock time to
        # actually mount, read document.cookie, and report back before
        # we'd already moved on to the "give up" branch. That was why a
        # hard refresh could still show "logged out" even for a valid
        # session. The fix is a REAL delay before each retry -- the same
        # pattern already used for the Azure SQL retry logic below --
        # so the browser's independent, concurrently-running mount
        # actually gets time to finish.
        st.markdown(
            '<div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>',
            unsafe_allow_html=True,
        )
        if st.session_state.cookie_probe_attempts < COOKIE_PROBE_MAX_ATTEMPTS:
            st.session_state.cookie_probe_attempts += 1
            with st.spinner("Checking session..."):
                time.sleep(COOKIE_PROBE_RETRY_DELAY_S)
            st.rerun()

        # Exhausted every retry (~2s of real waiting) and it's STILL not
        # back -- stop guessing with a fixed budget and fall back to
        # Streamlit's own automatic rerun-on-value-change (built into
        # every custom component: once the frontend eventually reports a
        # value that differs from the None default we've seen so far,
        # Streamlit reruns the script on its own, however long that
        # takes). We keep showing "Checking session..." rather than ever
        # falling through to the login screen here, because doing that
        # on merely-slow-but-genuinely-valid session data is exactly the
        # bug being fixed -- an occasional longer wait is preferable to
        # an incorrect logout.
        st.info("Checking session...")
        st.stop()

    # _raw_cookies is a real dict now (possibly {}) -- definitive answer.
    st.session_state.cookie_probe_attempts = 0
    _session_token = _raw_cookies.get("session_token")

# Validated on every single rerun, not just once -- this is what actually
# enforces the sliding 20-minute inactivity window (each call refreshes
# last_active_at) rather than trusting a stale login forever.
_session_check = auth.validate_session(auth_get_connection, _session_token) if _session_token else {"status": "Invalid"}

if _session_check.get("status") == "Valid":
    st.session_state.session_token = _session_token
    st.session_state.just_logged_out = False
else:
    st.session_state.pop("session_token", None)


def _render_auth_screen():
    """Login / sign-up screen shown whenever there's no valid session.
    Real Streamlit widgets capture the input; auth_theme.py restyles them
    via CSS targeting Streamlit's own DOM (stHorizontalBlock/stColumn),
    not a hand-written wrapper div -- see auth_theme.build_auth_css.
    Each set of fields is inside an st.form so Enter submits the whole
    group at once instead of Streamlit's per-widget "Press Enter to
    Apply" behavior."""
    st.markdown(auth_theme.build_auth_css(st.session_state.theme), unsafe_allow_html=True)

    for key, default in (
        ("login_step", "credentials"),
        ("login_user_id", None),
        ("login_message", None),
        ("signup_message", None),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    hero_col, form_col = st.columns([1, 1])

    with hero_col:
        st.markdown(auth_theme.hero_panel_html(), unsafe_allow_html=True)

    with form_col:
        st.markdown(
            '<div class="auth-form-title">Account <span class="accent">Access</span></div>'
            '<div class="auth-form-caption">Sign in or create an account to continue.</div>',
            unsafe_allow_html=True,
        )

        login_tab, signup_tab = st.tabs(["Login", "Sign Up"])

        with login_tab:
            if st.session_state.login_message:
                text, kind = st.session_state.login_message
                st.markdown(auth_theme.auth_message_html(text, kind), unsafe_allow_html=True)

            if st.session_state.login_step == "credentials":
                with st.form("login_credentials_form"):
                    login_email = st.text_input("Email", key="login_email")
                    login_password = st.text_input("Password", type="password", key="login_password")
                    submitted = st.form_submit_button("Send login code", type="primary", use_container_width=True)
                if submitted:
                    result = auth.request_login_code(auth_get_connection, login_email.strip(), login_password)
                    if result.get("status") == "Success":
                        st.session_state.login_user_id = result["user_id"]
                        st.session_state.login_step = "code"
                        st.session_state.login_message = (result["message"], "success")
                    else:
                        st.session_state.login_message = (result.get("message", "Login failed."), "error")
                    st.rerun()
            else:
                st.caption("Enter the 6-digit code emailed to you.")
                with st.form("login_code_form"):
                    code = st.text_input("Verification code", key="login_code")
                    # Stacked, not two side-by-side columns: a nested
                    # st.columns() here used to create a second
                    # stHorizontalBlock that the hero/form split's own CSS
                    # (min-height, panel background) was matching too,
                    # producing a huge stretched empty box around these
                    # two buttons. Stacking avoids creating that nested
                    # row at all.
                    verify_clicked = st.form_submit_button("Verify code", type="primary", use_container_width=True)
                    restart_clicked = st.form_submit_button("Start over", type="secondary")

                if verify_clicked:
                    result = auth.verify_login_code(auth_get_connection, st.session_state.login_user_id, code)
                    if result.get("status") == "Success":
                        session_token = result["session_token"]
                        # Constructed here rather than at module scope --
                        # its __init__ makes its own get_all() call as a
                        # side effect, which we don't want competing with
                        # the sentinel-backed read above on every normal
                        # page load. Only needed for .set(), only at the
                        # moment a session is actually created.
                        stx.CookieManager(key="set_cookie_manager").set(
                            "session_token",
                            session_token,
                            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                            key="set_session_cookie",
                        )
                        # This is the FIRST time this specific component
                        # key has ever been mounted in this browser tab
                        # (unlike the read-side probe, which is warm from
                        # page load) -- its iframe needs real time to
                        # load and actually execute `document.cookie =
                        # ...`. The st.rerun() a few lines down tears
                        # this component out of the tree (login_step
                        # flips away from "code", so it won't be
                        # recreated), which can abort that in-flight
                        # write if it fires before the browser's had a
                        # chance to finish it -- this was why the cookie
                        # never actually showed up in the browser at all,
                        # not merely a slow-to-be-read one.
                        with st.spinner("Signing you in..."):
                            time.sleep(0.6)
                        st.session_state.session_token = session_token
                        st.session_state.just_logged_out = False
                        st.session_state.login_step = "credentials"
                        st.session_state.login_user_id = None
                        st.session_state.login_message = None
                    else:
                        st.session_state.login_message = (result.get("message", "Verification failed."), "error")
                    st.rerun()
                elif restart_clicked:
                    st.session_state.login_step = "credentials"
                    st.session_state.login_user_id = None
                    st.session_state.login_message = None
                    st.rerun()

        with signup_tab:
            if st.session_state.signup_message:
                text, kind = st.session_state.signup_message
                st.markdown(auth_theme.auth_message_html(text, kind), unsafe_allow_html=True)

            with st.form("signup_form"):
                signup_name = st.text_input("Full name", key="signup_name")
                signup_email = st.text_input("Email", key="signup_email")
                signup_password = st.text_input("Password", type="password", key="signup_password")
                submitted = st.form_submit_button("Create account", type="primary", use_container_width=True)
            if submitted:
                result = auth.sign_up(auth_get_connection, signup_email.strip(), signup_name.strip(), signup_password)
                kind = "success" if result.get("status") == "Success" else "error"
                st.session_state.signup_message = (result["message"], kind)
                st.rerun()


if _session_check.get("status") != "Valid":
    _render_auth_screen()
    st.stop()

CURRENT_USER = _session_check  # {"status": "Valid", "user_id", "email", "name"}


def find_verified_change(tool_log):
    """Scans a run's tool log for a verify_order_modification call that
    came back Success, and returns its inputs (order_id, mapped_sku,
    requested_quantity) -- the proposed change a human (or auto-approve)
    can now commit. Returns None if no such call is present."""
    if not tool_log:
        return None
    for call in reversed(tool_log):
        if call["tool_called"] == "verify_order_modification" and call["result"].get("status") == "Success":
            return call["inputs"]
    return None


def _process_pending_email(item):
    """Runs one pending inbox email (a dict from email_ingestion.
    get_pending_emails) through the exact same run_agent() pipeline as a
    manually pasted email -- same approval-gate handling (auto-commit, or
    queued for manual review). Updates st.session_state.last_result with
    the outcome, so the "Agent Execution" panel shows this result in
    place of whatever was there before (the same single-result pattern
    used before ingestion was added -- not an accumulating list, whether
    triggered by the per-email "Process" button or by "Process All"
    working through several in a row).

    On success, moves the email from pending_emails to processed_emails
    in the database (email_ingestion.record_processed_email) and marks
    it processed in Gmail. If run_agent() itself raises, the email is
    deliberately left in pending_emails and never marked processed in
    Gmail, so it's retried on the next inbox check rather than silently
    lost to a transient failure (e.g. a dropped Claude API call)."""
    try:
        tool_log, final_result, reply = run_agent(item["body"])
    except Exception as e:
        st.session_state.last_result = {"error": str(e)}
        return

    st.session_state.last_result = {
        "tool_log": tool_log,
        "final_result": final_result,
        "reply": reply,
    }
    # A freshly processed email supersedes whatever manual-paste approval
    # state was left over -- both sources share this one display panel.
    st.session_state.pending_approval = None
    st.session_state.last_commit_result = None

    verified_change = find_verified_change(tool_log) if tool_log else None

    # Queue the drafted reply for human approval before it's ever sent --
    # same check/commit-style gate already used for database writes, now
    # extended to outbound email. Every drafted reply is queued here,
    # regardless of outcome (a clarification question needs sending just
    # as much as an order confirmation does). item["sender"] is the raw
    # "From" header (can be "Display Name <addr>", not a bare address) --
    # parseaddr() pulls out just the address smtplib actually needs.
    sender_address = parseaddr(item["sender"])[1] or item["sender"]
    outbound_email.queue_draft(
        auth_get_connection,
        to_email=sender_address,
        subject=f"Re: {item['subject']}",
        body=reply,
        context_note=(
            f"Order {verified_change['order_id']} — from inbox ({item['sender']})"
            if verified_change
            else f"{final_result.get('status') if final_result else 'No Action'} — from inbox ({item['sender']})"
        ),
    )

    if final_result and final_result.get("status") == "Success" and verified_change:
        if st.session_state.auto_approve:
            commit_result = commit_order_modification(**verified_change)
            if commit_result.get("status") == "Success":
                st.cache_data.clear()
                st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
            else:
                st.toast(f"Auto-approve commit failed: {commit_result.get('message')}", icon="⚠️")
        else:
            # Queued, not auto-committed -- a human still needs to
            # review it. The email is still moved to processed_emails
            # below regardless: "processed" means the agent has
            # reached an outcome for it, not that a human has acted
            # on that outcome yet. Re-fetching the same email on
            # every future inbox check just because nobody's clicked
            # Approve yet would be worse, not safer.
            st.session_state.pending_approval_queue.append({
                **verified_change,
                "sender": item["sender"],
                "subject": item["subject"],
            })

    tool_chain_summary = " -> ".join(call["tool_called"] for call in tool_log) if tool_log else "NONE"
    final_status = final_result.get("status", "?") if final_result else "No Action"

    email_ingestion.record_processed_email(
        auth_get_connection,
        uid=item["uid"],
        sender=item["sender"],
        subject=item["subject"],
        body=item["body"],
        tool_chain_summary=tool_chain_summary,
        final_status=final_status,
        reply_text=reply,
    )
    email_ingestion.mark_processed(item["uid"])


def db_mode_badge_html(mode: str, has_error: bool) -> str:
    """Small stamp badge for the header showing which DB backend is live.
    Reuses the existing .stamp CSS classes from theme.py rather than
    introducing a new component."""
    variant = "stamp-danger" if has_error else "stamp-info"
    label = f"{mode} · CONNECTION ERROR" if has_error else mode
    return (
        f'<span class="stamp {variant}" '
        f'style="font-size:0.7rem; padding:4px 12px; transform: rotate(-2deg);">{label}</span>'
    )


DB_SNAPSHOT_MAX_ATTEMPTS = 5  # Azure SQL only -- see get_db_snapshot
DB_SNAPSHOT_RETRY_DELAY_S = 1.5


@st.cache_data(ttl=5)
def get_db_snapshot():
    """Returns (inventory_df, orders_df, error). error is None on success;
    when set, the sidebar shows it instead of crashing the app.

    Cached for 5s (@st.cache_data(ttl=5)) so the almost-every-rerun nature
    of Streamlit (button clicks, toggles, dropdown changes) doesn't re-hit
    the database when nothing has changed -- 5s is short enough that the
    sidebar is never meaningfully stale, long enough to absorb a burst of
    reruns from rapid clicking. This cache is only ever read for display;
    it's never consulted for feasibility checks (check_order_modification
    always queries live), so a cached snapshot can't cause a wrong
    approval decision -- only a few-second-old sidebar number. Callers
    that write to the database (commit_order_modification) must call
    st.cache_data.clear() right after a successful commit so the sidebar
    doesn't keep showing the pre-write snapshot for up to 5 more seconds.

    Azure SQL gets retried up to DB_SNAPSHOT_MAX_ATTEMPTS times: on a
    Container Apps cold start, the very first request can hit "Login
    timeout expired (SQLDriverConnect)" while the container's network path
    to Azure SQL (DNS, TLS session) is still warming up, even though the
    connection succeeds moments later. Local SQLite has no such cold-start
    path, so it stays a single attempt. This can block for a while in the
    worst case -- the caller is expected to wrap the call in a scoped
    loading indicator (e.g. st.spinner) rather than let it block silently.
    """
    if DB_INIT_ERROR:
        return None, None, DB_INIT_ERROR

    attempts = DB_SNAPSHOT_MAX_ATTEMPTS if USE_AZURE_DB else 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            conn = get_azure_connection() if USE_AZURE_DB else sqlite3.connect("mock_erp.db")
            inv_df = pd.read_sql_query("SELECT * FROM inventory", conn)
            ord_df = pd.read_sql_query("SELECT * FROM orders", conn)
            conn.close()
            return inv_df, ord_df, None
        except Exception as e:
            last_error = str(e)
            if attempt < attempts:
                time.sleep(DB_SNAPSHOT_RETRY_DELAY_S)

    return None, None, last_error


def load_sample_emails():
    try:
        with open("sample_emails.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# --- Header ---
# DB_INIT_ERROR can't be set here -- the auth gate above already st.stop()s
# on it before this point, so there's no error branch to show.
header_col, approve_toggle_col, theme_toggle_col, user_col = st.columns([3.2, 1.4, 1, 1.8])
with header_col:
    st.markdown(
        f"""
        <div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>
        <div class="manifest-sub">PORTFOLIO PROTOTYPE · CLAUDE SONNET 5 · FICTIONAL COMPANY, FICTIONAL DATA</div>
        <div style="margin-top: 8px;">{db_mode_badge_html(DB_MODE, bool(DB_INIT_ERROR))}</div>
        """,
        unsafe_allow_html=True,
    )
with approve_toggle_col:
    st.write("")
    st.toggle("Auto-approve changes", key="auto_approve", value=False)
with theme_toggle_col:
    st.write("")
    label = "☀️ Light mode" if st.session_state.theme == "dark" else "🌙 Dark mode"
    if st.button(label, type="secondary", use_container_width=True):
        st.session_state.theme = "light" if st.session_state.theme == "dark" else "dark"
        st.rerun()
with user_col:
    st.write("")
    # First name + Logout as one right-aligned group at the far right of
    # the header, rather than the full name stacked above a full-width
    # button.
    first_name = (CURRENT_USER.get("name") or "").split()
    first_name = first_name[0] if first_name else "User"
    name_col, logout_col = st.columns([1.3, 1], gap="small")
    with name_col:
        st.markdown(
            f"""
            <div style="text-align:right; font-family:'IBM Plex Mono',monospace;
                        font-size:0.75rem; color:var(--text-dim); padding-top:8px;">
                Logged in as <b style="color:var(--text);">{first_name}</b>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with logout_col:
        if st.button("Log out", type="secondary", use_container_width=True):
            auth.log_out(auth_get_connection, st.session_state.get("session_token"))
            # Not cookie_manager.delete() -- it does `del self.cookies[cookie]`
            # internally, which raises KeyError whenever that key isn't
            # already present in the manager's local dict (e.g. if the
            # cookie component's initial load returned {} before this
            # click). That uncaught exception was rendering as a brief
            # error before the page settled on the login screen. Calling
            # the same underlying component method directly does the
            # actual browser-side deletion without that fragile bookkeeping.
            _cookie_component(method="delete", cookie="session_token", key="delete_session_cookie", default=False)
            # Same reasoning as the .set() call on login: "delete_session_
            # cookie" is a brand new component key, first mounted right
            # here, and the st.rerun() below would otherwise tear it out
            # of the tree before its iframe has had real time to actually
            # run the browser-side deletion. Without this, the DB session
            # is correctly gone (auth.log_out() above) and this tab
            # behaves as logged out (just_logged_out=True below), but the
            # stale browser cookie could persist and resurface on a later
            # fresh load.
            with st.spinner("Logging out..."):
                time.sleep(0.6)
            st.session_state.pop("session_token", None)
            st.session_state.just_logged_out = True
            st.rerun()

st.write("")

# --- Main layout ---
col1, col2 = st.columns(2)

with col1:
    st.subheader("Inbound Email")

    st.caption(f"Real customer emails, pulled from Gmail's \"{email_ingestion.IMAP_LABEL}\" label.")
    check_inbox_clicked = st.button(
        "📥 Check Inbox for New Orders",
        type="secondary",
        use_container_width=True,
        disabled=bool(DB_INIT_ERROR),
    )

    if check_inbox_clicked:
        # Fetch, then sync into the pending_emails table (deduped by
        # uid against both pending_emails and processed_emails) -- never
        # runs anything through the agent or marks anything processed in
        # Gmail. That only happens once a human explicitly clicks
        # Process / Process All below, so they can see what was found
        # before anything acts on it. Safe to click repeatedly: syncing
        # can never duplicate an email still pending, or resurrect one
        # already processed.
        with st.spinner("Checking inbox..."):
            try:
                fetched = email_ingestion.fetch_new_order_emails()
            except Exception as e:
                st.session_state.fetch_error = str(e)
            else:
                st.session_state.fetch_error = None
                inserted = email_ingestion.sync_fetched_emails(auth_get_connection, fetched)
                st.toast(
                    f"Found {inserted} new email(s)." if inserted else "No new emails found.",
                    icon="📥",
                )
        st.rerun()

    if st.session_state.fetch_error:
        st.error(f"Could not check inbox: {st.session_state.fetch_error}")

    # Queried fresh from the database on every render, not session state
    # -- correct regardless of which browser session originally fetched
    # an email, and survives a page refresh.
    pending_emails = email_ingestion.get_pending_emails(auth_get_connection)

    if pending_emails:
        st.markdown(f"**{len(pending_emails)} email(s) pending processing:**")
        process_all_clicked = st.button(
            "▶️ Process All",
            type="primary",
            use_container_width=True,
            key="process_all_ingested",
        )

        process_this_uid = None
        for i, item in enumerate(pending_emails, 1):
            preview = item["body"][:300] + ("…" if len(item["body"]) > 300 else "")
            st.markdown(
                email_card_html(
                    i,
                    html.escape(item["sender"]),
                    html.escape(item["subject"]),
                    html.escape(preview),
                    delay_s=(i - 1) * 0.08,
                ),
                unsafe_allow_html=True,
            )
            if st.button("Process", key=f"process_ingested_{item['uid']}"):
                process_this_uid = item["uid"]

        # Only one of these can actually be true on any given run --
        # Streamlit only reports a click for the specific widget that was
        # clicked -- so there's no risk of double-processing here.
        if process_all_clicked:
            for item in pending_emails:
                _process_pending_email(item)
            st.rerun()
        elif process_this_uid:
            item = next(e for e in pending_emails if e["uid"] == process_this_uid)
            _process_pending_email(item)
            st.rerun()
    else:
        st.caption("No emails currently pending processing.")

    if st.session_state.pending_approval_queue:
        pending = st.session_state.pending_approval_queue[0]
        remaining = len(st.session_state.pending_approval_queue)
        label = "**Database Write — from inbox**"
        if remaining > 1:
            label += f" ({remaining} awaiting review)"
        st.markdown(label)
        st.markdown(stamp_html("Pending Approval"), unsafe_allow_html=True)
        st.markdown(
            f"""<div class="reply-card">
            From: <b>{html.escape(pending['sender'])}</b><br>
            Subject: <b>{html.escape(pending['subject'])}</b><br>
            Order: <b>{pending['order_id']}</b><br>
            SKU: <b>{pending['mapped_sku']}</b><br>
            New quantity: <b>{pending['requested_quantity']}</b>
            </div>""",
            unsafe_allow_html=True,
        )
        ingest_approve_col, ingest_reject_col = st.columns(2)
        with ingest_approve_col:
            if st.button("✅ Approve & Apply", type="primary", use_container_width=True, key="ingest_approve"):
                commit_result = commit_order_modification(
                    order_id=pending["order_id"],
                    mapped_sku=pending["mapped_sku"],
                    requested_quantity=pending["requested_quantity"],
                )
                st.session_state.pending_approval_queue.pop(0)
                if commit_result.get("status") == "Success":
                    st.cache_data.clear()
                    st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                else:
                    st.toast(f"Commit failed: {commit_result.get('message')}", icon="⚠️")
                st.rerun()
        with ingest_reject_col:
            if st.button("❌ Reject", use_container_width=True, key="ingest_reject"):
                st.session_state.pending_approval_queue.pop(0)
                st.toast("Change discarded.", icon="🚫")
                st.rerun()

    st.divider()

    sample_emails = load_sample_emails()

    if sample_emails:
        options = ["-- Write your own --"] + [
            f"[{e['category']}] {e['id']} — {e['subject']}" for e in sample_emails
        ]
        choice = st.selectbox("Pick a test case, or write your own:", options)

        if choice == "-- Write your own --":
            email_body = st.text_area("Email content:", height=200, placeholder="Paste or type a customer email here...")
            original_subject = "Your Order"
        else:
            idx = options.index(choice) - 1
            email_body = st.text_area("Email content:", value=sample_emails[idx]["body"], height=200)
            original_subject = sample_emails[idx]["subject"]
    else:
        st.info("No sample_emails.json found — run generate_test_emails.py first for pre-built test cases.")
        email_body = st.text_area("Email content:", height=200, placeholder="Paste or type a customer email here...")
        original_subject = "Your Order"

    # Manually pasted/typed emails have no "From" header to draw a reply
    # address from (unlike ingested emails, which do) -- required so the
    # drafted reply can actually be queued for approval below.
    sender_email = st.text_input(
        "Sender's email address:",
        placeholder="customer@example.com",
        key="manual_sender_email",
    )

    run_clicked = st.button(
        "Process with Agent",
        type="primary",
        disabled=not email_body.strip() or not sender_email.strip() or bool(DB_INIT_ERROR),
    )

with col2:
    st.subheader("Agent Execution")

    if run_clicked:
        with st.spinner("Agent reasoning and calling tools..."):
            try:
                tool_log, final_result, reply = run_agent(email_body)
                # Persist to session_state so this survives the st.rerun()
                # below (used to refresh the sidebar tables) instead of
                # vanishing the moment the script re-executes.
                st.session_state.last_result = {
                    "tool_log": tool_log,
                    "final_result": final_result,
                    "reply": reply,
                }
                # A fresh run supersedes whatever approval state was left
                # over from the previous email.
                st.session_state.pending_approval = None
                st.session_state.last_commit_result = None

                verified_change = find_verified_change(tool_log) if tool_log else None

                # Queue the drafted reply for human approval before it's
                # ever sent -- same check/commit-style gate already used
                # for database writes, now extended to outbound email.
                # This must run BEFORE the st.rerun() below (which only
                # fires for a verified change), so it happens for every
                # outcome -- including a clarification question, which
                # still needs to reach the customer.
                outbound_email.queue_draft(
                    auth_get_connection,
                    to_email=sender_email.strip(),
                    subject=f"Re: {original_subject}",
                    body=reply,
                    context_note=(
                        f"Order {verified_change['order_id']}" if verified_change
                        else (final_result.get("status") if final_result else "No Action")
                    ),
                )

                if final_result and final_result.get("status") == "Success" and verified_change:
                    if st.session_state.auto_approve:
                        commit_result = commit_order_modification(**verified_change)
                        st.session_state.last_commit_result = commit_result
                        if commit_result.get("status") == "Success":
                            st.cache_data.clear()  # sidebar shouldn't show the pre-write snapshot
                            st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                        else:
                            st.toast(f"Auto-approve commit failed: {commit_result.get('message')}", icon="⚠️")
                    else:
                        st.session_state.pending_approval = verified_change
                    st.rerun()
            except Exception as e:
                st.session_state.last_result = {"error": str(e)}

    if "last_result" in st.session_state:
        result = st.session_state.last_result

        if "error" in result:
            st.error(f"Agent error: {result['error']}")
        else:
            tool_log = result["tool_log"]
            final_result = result["final_result"]
            reply = result["reply"]

            st.markdown("**Tool Call Chain**")
            if not tool_log:
                st.markdown(stamp_html("No Action"), unsafe_allow_html=True)
                st.caption("Claude replied without taking any action.")
            else:
                for i, call in enumerate(tool_log, 1):
                    status = call["result"].get("status", "?")
                    detail_lines = [f"{k}: {v}" for k, v in call["inputs"].items()]
                    detail_lines.append(f"→ {status}")
                    detail = "\n".join(detail_lines)
                    st.markdown(
                        step_card_html(i, call["tool_called"], status, detail, delay_s=(i - 1) * 0.12),
                        unsafe_allow_html=True,
                    )

            final_status = final_result.get("status", "?") if final_result else "No Action"

            st.markdown("**Outcome**")
            st.markdown(stamp_html(final_status), unsafe_allow_html=True)

            st.markdown("**Reply to Customer**")
            st.markdown(f'<div class="reply-card">{reply}</div>', unsafe_allow_html=True)

            # --- Human approval gate for the database write ---
            if st.session_state.pending_approval:
                pending = st.session_state.pending_approval
                st.markdown("**Database Write**")
                st.markdown(stamp_html("Pending Approval"), unsafe_allow_html=True)
                st.markdown(
                    f"""<div class="reply-card">
                    Order: <b>{pending['order_id']}</b><br>
                    SKU: <b>{pending['mapped_sku']}</b><br>
                    New quantity: <b>{pending['requested_quantity']}</b>
                    </div>""",
                    unsafe_allow_html=True,
                )
                approve_col, reject_col = st.columns(2)
                with approve_col:
                    if st.button("✅ Approve & Apply", type="primary", use_container_width=True):
                        commit_result = commit_order_modification(**pending)
                        st.session_state.last_commit_result = commit_result
                        st.session_state.pending_approval = None
                        if commit_result.get("status") == "Success":
                            st.cache_data.clear()  # sidebar shouldn't show the pre-write snapshot
                            st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                        else:
                            st.toast(f"Commit failed: {commit_result.get('message')}", icon="⚠️")
                        st.rerun()
                with reject_col:
                    if st.button("❌ Reject", use_container_width=True):
                        st.session_state.pending_approval = None
                        st.session_state.last_commit_result = {
                            "status": "Rejected by Reviewer",
                            "message": "Change discarded by reviewer — no database write performed.",
                        }
                        st.rerun()
            elif st.session_state.last_commit_result:
                commit_result = st.session_state.last_commit_result
                st.markdown("**Database Write**")
                st.markdown(stamp_html(commit_result.get("status", "?")), unsafe_allow_html=True)
                st.caption(commit_result.get("message", ""))

    if not run_clicked and "last_result" not in st.session_state:
        st.info("Select or write an email on the left, then click **Process with Agent**.")

    st.divider()
    st.markdown("**📤 Pending Outbound Emails**")
    st.caption("Every drafted reply is queued here for approval before it's ever sent -- same gate as a database write.")
    pending_drafts = outbound_email.get_pending_drafts(auth_get_connection)
    if pending_drafts:
        for draft in pending_drafts:
            st.markdown(stamp_html("Pending Approval"), unsafe_allow_html=True)
            st.markdown(
                f"""<div class="reply-card">
                To: <b>{html.escape(draft['to_email'])}</b><br>
                Subject: <b>{html.escape(draft['subject'])}</b><br><br>
                {draft['body']}
                </div>""",
                unsafe_allow_html=True,
            )
            if draft.get("context_note"):
                st.caption(f"Context: {draft['context_note']}")
            approve_email_col, reject_email_col = st.columns(2)
            with approve_email_col:
                if st.button("✅ Approve & Send", type="primary", use_container_width=True, key=f"approve_email_{draft['id']}"):
                    send_result = outbound_email.approve_and_send(auth_get_connection, draft["id"])
                    if send_result.get("status") == "Sent":
                        st.toast(send_result.get("message", "Email sent."), icon="📧")
                    else:
                        st.toast(f"Send failed: {send_result.get('message')}", icon="⚠️")
                    st.rerun()
            with reject_email_col:
                if st.button("❌ Reject", use_container_width=True, key=f"reject_email_{draft['id']}"):
                    outbound_email.reject_draft(auth_get_connection, draft["id"])
                    st.toast("Draft discarded — not sent.", icon="🚫")
                    st.rerun()
            st.divider()
    else:
        st.caption("No outbound emails currently pending approval.")

# --- Sidebar: live ERP ledger ---
# Rendered last, after the header/CSS/toggles and the main email/agent
# panel are already fully sent to the browser. get_db_snapshot() can block
# for up to ~56s on an Azure SQL cold start (see its docstring); placing it
# here -- rather than before the main layout -- means that wait never holds
# up the page shell or the email input/process button, which render (and
# are usable) from the earlier part of this same script run. st.sidebar is
# a fixed screen region, so its position in the script doesn't change
# where it appears on screen, only when its content becomes available.
with st.sidebar:
    st.header("Live ERP State")
    with st.spinner(f"Connecting to {DB_MODE}..."):
        inv, ords, db_error = get_db_snapshot()
    if db_error:
        st.error(f"Could not load {DB_MODE} state:\n\n{db_error}")
    else:
        st.subheader("Inventory")
        st.dataframe(inv, use_container_width=True, hide_index=True)
        st.subheader("Orders")
        st.dataframe(ords, use_container_width=True, hide_index=True)
    st.caption(f"Backend: {DB_MODE}. Refreshes automatically after each successful reconciliation.")

    st.header("Processed Emails")
    st.caption("Reference log of every inbox email run through the agent -- viewable anytime, not just right after processing.")
    processed_emails = email_ingestion.get_processed_emails(auth_get_connection)
    if processed_emails:
        processed_df = pd.DataFrame(processed_emails)[["sender", "subject", "final_status", "processed_at"]]
        processed_df.columns = ["Sender", "Subject", "Status", "Processed At"]
        st.dataframe(processed_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No emails processed yet.")

    st.header("Sent Emails")
    st.caption("Permanent audit log of every outbound email actually sent -- nothing here was sent without an explicit Approve & Send click.")
    sent_log = outbound_email.get_sent_log(auth_get_connection)
    if sent_log:
        sent_df = pd.DataFrame(sent_log)[["to_email", "subject", "sent_at"]]
        sent_df.columns = ["Recipient", "Subject", "Sent At"]
        st.dataframe(sent_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No emails sent yet.")
