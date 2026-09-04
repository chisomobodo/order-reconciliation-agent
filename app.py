"""
Interview demo dashboard for Arbiter -- an AI order-reconciliation
agent portfolio project.

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
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, make_msgid

import extra_streamlit_components as stx
from extra_streamlit_components.CookieManager import _component_func as _cookie_component
import pandas as pd
import sqlite3
import streamlit as st

import auth
import auth_theme
import email_ingestion
import hold_requests
import outbound_email
from theme import (
    build_css,
    email_card_html,
    empty_state_html,
    field_label_html,
    section_heading_html,
    step_card_html,
    stamp_html,
)

st.set_page_config(layout="wide", page_title="Arbiter", page_icon="assets/arbiter_favicon.png")

ARBITER_HEADER_ICON_SVG_PATH = "assets/arbiter_icon_header.svg"


def _load_svg(path: str) -> str:
    """Reads an SVG asset's raw markup for inline embedding via
    st.markdown(..., unsafe_allow_html=True). Returns an empty string
    if the file is missing, so a moved/renamed asset degrades to no
    logo rather than crashing the page."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _header_logo_html() -> str:
    """The one branded header lockup (icon + 'Arbiter' wordmark), used
    for the fully-loaded page AND every loading/waiting/error state
    that renders before it (DB_INIT_ERROR, the retry-exhausted DB
    connection handler, the cookie-probe "Checking session..." wait) --
    those states used to fall back to old plain-text/uppercase markup
    (.manifest-title) instead of this, causing a visible flash of
    unbranded content before the real header took over on every single
    page load. Defined here, near the top of the script, specifically
    so it's available to those early states -- they run before the
    header's own code further down. assets/arbiter_icon_header.svg is
    the mark alone -- no <title>/<desc> (which the browser renders as a
    hover tooltip on the earlier logo variant) and no background rect
    (which showed as an unwanted dark box around the icon). The
    wordmark is a real HTML element here, not baked into the SVG, so it
    can be styled/positioned independently."""
    icon_svg = _load_svg(ARBITER_HEADER_ICON_SVG_PATH)
    icon_html = f'<span class="app-header-icon">{icon_svg}</span>' if icon_svg else ""
    return f"""
    <div class="app-header-logo-row">
        {icon_html}
        <span class="app-header-wordmark">Arbiter</span>
    </div>
    """

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
    st.markdown(_header_logo_html(), unsafe_allow_html=True)
    # Clean, simple primary message for the end user -- the actual
    # exception and the env vars to check are real diagnostic info
    # (useful to whoever runs/deploys this), but they're internal
    # detail that doesn't belong in front of someone just trying to use
    # the app, so they're tucked behind an explicit expander instead.
    st.error("Couldn't connect to the database. Please try again shortly.")
    with st.expander("Technical details"):
        st.markdown(
            f"USE_AZURE_DB is set, but the app couldn't connect to Azure SQL: {DB_INIT_ERROR}\n\n"
            "Check that AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, and "
            "AZURE_SQL_PASSWORD are set correctly, then restart the app. Or set "
            "USE_AZURE_DB=false (or unset it) to fall back to the local SQLite backend."
        )
    st.stop()

# Database schema setup (auth/session, inbox-tracking, outbound-email
# queue, hold-requests tables) no longer happens here. It used to run on
# every process's first page load (_ensure_*_schema(), gated behind
# @st.cache_resource) -- now it runs once at container startup, before
# Streamlit ever starts accepting traffic. See init_db.py and
# entrypoint.sh: the container's HTTP port doesn't open until schema
# init has already completed successfully, so no request from any user
# can race an uninitialized database.

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
# Budget was originally 6 attempts * 0.35s (~2.1s worst case), sized
# around a fast local browser's component mount time. Measured directly
# (real Chrome, Playwright, repeated fresh/uncached page loads against
# this app): the cookie component's actual mount + document.cookie read +
# postMessage round-trip took 4.4s-9.7s on a cold/uncached load, and only
# ~1-1.7s on a warm reload in the same browser -- so almost every FIRST
# load of a session was blowing through the old ~2.1s budget and falling
# into the branch below that just waits, silently and indefinitely, for
# Streamlit's own automatic rerun-on-value-change. That branch still
# resolves correctly (nothing was ever actually stuck), but it has no
# visible retry/spinner cadence of its own (each active-loop attempt
# re-renders a spinner; the fallback is one static, unchanging message),
# which is what read as "slow, every single reload." 12s of active-retry
# budget comfortably covers the measured ~9.7s worst case, so the
# visibly-retrying loop now handles the realistic cold-mount case instead
# of silently falling through to that quieter fallback.
COOKIE_PROBE_MAX_ATTEMPTS = 30
COOKIE_PROBE_RETRY_DELAY_S = 0.4  # ~12s worst case across all retries

if "cookie_probe_attempts" not in st.session_state:
    st.session_state.cookie_probe_attempts = 0
if "just_logged_out" not in st.session_state:
    st.session_state.just_logged_out = False
if "cookie_probe_exhausted" not in st.session_state:
    st.session_state.cookie_probe_exhausted = False

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
elif st.session_state.cookie_probe_exhausted:
    # Already gave up resolving the cookie once this browser session --
    # treat as "no session" rather than mounting the cookie-read
    # component again. Re-mounting it was the actual bug: the component
    # function was previously called unconditionally on every single
    # pass through this branch, including every run AFTER our own retry
    # budget below was exhausted and had stopped calling st.rerun()
    # itself. extra_streamlit_components' CookieManager component can
    # report its value again on its own (not only in direct response to
    # being re-rendered by us), and Streamlit automatically reruns the
    # script on ANY reported value from a mounted component -- confirmed
    # empirically (real browser, WebSocket frames captured) that this
    # kept the app rerunning indefinitely, with zero user interaction,
    # long after our own explicit st.rerun() calls had stopped. Never
    # calling the component again after giving up once is what actually
    # stops that -- a bounded, one-time retry budget below, followed by
    # a genuinely final answer, not another indefinite wait.
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
        st.markdown(_header_logo_html(), unsafe_allow_html=True)
        if st.session_state.cookie_probe_attempts < COOKIE_PROBE_MAX_ATTEMPTS:
            st.session_state.cookie_probe_attempts += 1
            with st.spinner("Checking session..."):
                time.sleep(COOKIE_PROBE_RETRY_DELAY_S)
            st.rerun()

        # Exhausted every retry (~12s of real waiting) and it's STILL not
        # back -- conclusively treat this as "no session" (proceed to the
        # real login screen) instead of parking in "Checking session..."
        # waiting indefinitely on more messages from the component. That
        # indefinite wait used to rely on Streamlit's automatic rerun-on-
        # value-change firing exactly once, when a genuinely slow mount
        # finally resolves -- but the cookie component doesn't only ever
        # report once: it can keep posting on its own, and each post
        # looked like a fresh, legitimate value change, so the "just wait
        # a bit longer" branch could in practice mean rerunning forever
        # with the tab sitting idle. A 12s active budget already
        # comfortably covers real measured mount times (see above), so
        # concluding here trades an already-rare, self-recoverable edge
        # case (a hard refresh landing on the login screen despite a
        # still-valid cookie, fixed by simply reloading again) for
        # actually going quiet when the script is done, which a
        # background/idle tab must do.
        st.session_state.cookie_probe_exhausted = True
        _session_token = None
    else:
        # _raw_cookies is a real dict now (possibly {}) -- definitive answer.
        st.session_state.cookie_probe_attempts = 0
        _session_token = _raw_cookies.get("session_token")

# Validated on every single rerun, not just once -- this is what actually
# enforces the sliding 20-minute inactivity window (each call refreshes
# last_active_at) rather than trusting a stale login forever.
#
# Cached for SESSION_VALIDATION_TTL_S (@st.cache_data, same pattern as
# get_db_snapshot below): validate_session()'s own query is cheap, but
# the connection it needs is not -- opening a fresh one costs low
# single-digit ms on local SQLite (invisible) but several real seconds
# against Azure SQL (confirmed: a bare get_connection() call there took
# ~6.4s). Before this cache, that connection was opened fresh on EVERY
# rerun -- every reload, every button click, every widget interaction
# -- which blocked the entire page behind that multi-second wait right
# after the header, before the auth screen or dashboard could render.
# The sliding window still works, just refreshed at most once per TTL
# instead of on literally every rerun -- a few-second slack on a
# 20-minute inactivity timeout, not a meaningful security loosening.
SESSION_VALIDATION_TTL_S = 20


@st.cache_data(ttl=SESSION_VALIDATION_TTL_S)
def _validate_session_cached(session_token: str) -> dict:
    with closing(auth_get_connection()) as conn:
        return auth.validate_session(conn, session_token)


if _session_token:
    _session_check = _validate_session_cached(_session_token)
else:
    _session_check = {"status": "Invalid"}

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
                    with closing(auth_get_connection()) as _conn:
                        result = auth.request_login_code(_conn, login_email.strip(), login_password)
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
                    with closing(auth_get_connection()) as _conn:
                        result = auth.verify_login_code(_conn, st.session_state.login_user_id, code, is_azure=USE_AZURE_DB)
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
                with closing(auth_get_connection()) as _conn:
                    result = auth.sign_up(_conn, signup_email.strip(), signup_name.strip(), signup_password)
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


def find_clarification_request(tool_log):
    """Scans a run's tool log for a request_clarification call, and
    returns its inputs (order_id, ambiguous_reference, candidate_skus,
    clarifying_question) -- order_id is only present if the customer
    actually gave one (it's an optional field in the tool schema).
    Returns None if no such call is present."""
    if not tool_log:
        return None
    for call in reversed(tool_log):
        if call["tool_called"] == "request_clarification":
            return call["inputs"]
    return None


TOOL_STEP_LABELS = {
    "get_order_details": "Checking order details...",
    "verify_order_modification": "Verifying feasibility against stock and dispatch status...",
    "request_clarification": "Product reference is ambiguous — preparing a clarifying question...",
}


def _make_status_step_callback(status):
    """Returns an on_step(tool_name) callback bound to an st.status()
    widget -- passed into run_agent() so each tool call, as it actually
    completes, appends a human-readable line to the status box instead
    of the user seeing one generic spinner for the whole multi-step
    loop. Unknown tool names (shouldn't happen -- there are only three)
    still get a sensible fallback line rather than silently doing
    nothing."""
    def on_step(tool_name):
        status.write(TOOL_STEP_LABELS.get(tool_name, f"Running {tool_name}..."))
    return on_step


def _build_combined_context(hold: dict, new_reply_body: str) -> str:
    """Builds the full context run_agent() needs when a fetched email is
    a reply to an existing Hold -- the customer's new reply alone (often
    just "the 24-pack" or similar) isn't enough context to act on, so
    the original inbound email and the clarifying question we sent are
    included alongside it."""
    return (
        f"--- Original customer request ---\n{hold['inbound_body']}\n\n"
        f"--- Clarifying question we sent ---\n{hold['clarifying_question_sent']}\n\n"
        f"--- Customer's reply ---\n{new_reply_body}"
    )


def _run_agent_and_dispatch(conn, agent_input: str, item: dict, sender_address: str, matched_hold: dict | None = None):
    """Core of processing one inbound email through the agent -- shared
    by a brand-new pending email (matched_hold=None), a reply
    automatically matched to an open Hold (Layers 1-3), and a reply a
    human manually linked to a Hold (Layer 4). agent_input is what's
    actually sent to run_agent() (either item["body"] as-is, or original
    request + clarifying question + new reply combined via
    _build_combined_context()) -- but item/record_processed_email always
    use the real, individual inbound email's own fields, so the
    processed-email log reflects what actually arrived, not the
    constructed context.

    Takes an already-open connection, used for every DB-touching call
    made while handling this one action (run_agent's internal tool
    calls, the Hold/outbound-draft writes, and the final processed-email
    move) -- caller owns its lifecycle. This is what keeps a single
    "Process" click down to one Azure SQL connection instead of five-plus.

    Updates st.session_state.last_result with the outcome, so the
    "Agent Execution" panel shows this result in place of whatever was
    there before (a single-result panel, not an accumulating list).

    On success, moves the email from pending_emails to processed_emails
    in the database (email_ingestion.record_processed_email) and marks
    it processed in Gmail. If run_agent() itself raises, the email is
    deliberately left wherever the caller found it (pending_emails or
    manual_linking_emails) and never marked processed in Gmail, so it's
    retried rather than silently lost to a transient failure (e.g. a
    dropped Claude API call)."""
    try:
        with st.status("Processing email...", expanded=True) as status:
            tool_log, final_result, reply = run_agent(conn, agent_input, on_step=_make_status_step_callback(status))
            status.update(label="Done", state="complete")
    except Exception as e:
        status.update(label="Done", state="error")
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
    clarification_request = find_clarification_request(tool_log) if tool_log else None

    if matched_hold:
        # This reply resolves an existing Hold -- never opens a new one,
        # even if the agent's new reply is itself still a clarification
        # question. A still-ambiguous follow-up isn't automatically
        # re-held in this version; it's sent back to the customer as a
        # normal reply, and the original Hold is marked resolved since
        # the conversation has moved forward (flagged as a known
        # limitation, not silently expanded here -- see README).
        hold_requests.resolve_hold(conn, matched_hold["id"])
        outbound_email.queue_draft(
            conn,
            to_email=sender_address,
            subject=f"Re: {item['subject']}",
            body=reply,
            context_note=f"Reply to HOLD-{matched_hold['id']} — from inbox ({item['sender']})",
        )
    elif clarification_request and clarification_request.get("order_id"):
        # A genuinely NEW clarification request -- place the order on
        # hold so it isn't silently lost while waiting on the customer's
        # reply (see hold_requests.py / CLAUDE.md "Hold state design").
        # The Message-ID is generated here, BEFORE the email is actually
        # sent (sending is gated behind human approval and may happen
        # much later) -- stored on the Hold now, and passed through to
        # queue_draft() so approve_and_send() can set it as the real
        # outgoing Message-ID header once a human approves it. The
        # HOLD-{id} tag in the subject/body is Layer 2's fallback in case
        # In-Reply-To/References ever gets stripped by a mail client.
        sent_message_id = make_msgid()
        hold_result = hold_requests.create_hold(
            conn,
            order_id=clarification_request["order_id"],
            customer_email=sender_address,
            inbound_sender=item["sender"],
            inbound_subject=item["subject"],
            inbound_body=item["body"],
            clarifying_question_sent=reply,
            sent_message_id=sent_message_id,
        )
        hold_id = hold_result["hold_id"]
        outbound_email.queue_draft(
            conn,
            to_email=sender_address,
            subject=f"Re: {item['subject']} [HOLD-{hold_id}]",
            body=(
                f"{reply}\n\n"
                f"To help us match your reply to the right request, please keep "
                f"the reference HOLD-{hold_id} somewhere in your reply."
            ),
            message_id=sent_message_id,
            context_note=f"Order {clarification_request['order_id']} — from inbox ({item['sender']})",
        )
    else:
        # Queue the drafted reply for human approval before it's ever
        # sent -- same check/commit-style gate already used for database
        # writes, now extended to outbound email. Every drafted reply is
        # queued here, regardless of outcome.
        outbound_email.queue_draft(
            conn,
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
            commit_result = commit_order_modification(conn, **verified_change)
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
        conn,
        uid=item["uid"],
        sender=item["sender"],
        subject=item["subject"],
        body=item["body"],
        tool_chain_summary=tool_chain_summary,
        final_status=final_status,
        reply_text=reply,
    )
    email_ingestion.mark_processed(item["uid"])


def _process_pending_email(conn, item):
    """Entry point for a normal pending-inbox email (the "Process" /
    "Process All" buttons). Before treating it as a new request, checks
    it against open Holds via hold_requests.match_reply_to_hold() -- see
    that function's docstring for the four-layer matching order. An
    "ambiguous" result (Layer 3 found more than one open Hold for this
    sender) is never guessed at: the email is moved into the
    manual-linking queue for a human to resolve instead of being
    processed here.

    Takes an already-open connection -- caller owns its lifecycle. When
    called in a loop ("Process All"), the SAME connection is reused
    across every email in the batch rather than one per email."""
    sender_address = parseaddr(item["sender"])[1] or item["sender"]

    match_result = hold_requests.match_reply_to_hold(
        conn,
        sender_email=sender_address,
        subject=item["subject"],
        body=item["body"],
        in_reply_to=item.get("in_reply_to"),
        references=item.get("references"),
    )

    if match_result["status"] == "ambiguous":
        hold_requests.queue_for_manual_linking(conn, item)
        email_ingestion.remove_pending_email(conn, item["uid"])
        return

    matched_hold = match_result.get("hold")
    agent_input = _build_combined_context(matched_hold, item["body"]) if matched_hold else item["body"]
    _run_agent_and_dispatch(conn, agent_input, item, sender_address, matched_hold=matched_hold)


def db_mode_badge_html(mode: str, has_error: bool) -> str:
    """Small stamp badge for the header showing whether the backend is
    reachable. Deliberately doesn't name the actual backend (Azure SQL /
    local SQLite) -- that's an internal infrastructure detail, not
    something an end user of the app needs to see; `mode` is still
    accepted so callers don't need to change, it's just no longer
    rendered into the visible label. Reuses the existing .stamp CSS
    classes from theme.py rather than introducing a new component."""
    variant = "stamp-danger" if has_error else "stamp-info"
    label = "CONNECTION ERROR" if has_error else "CONNECTED"
    return f'<span class="stamp stamp-compact {variant}">{label}</span>'


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
        _header_logo_html() + f'<div style="margin-top: 8px;">{db_mode_badge_html(DB_MODE, bool(DB_INIT_ERROR))}</div>',
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
                        font-size:0.75rem; color:var(--text-dim); padding-top:8px;
                        padding-bottom:16px;">
                Logged in as <b style="color:var(--text);">{first_name}</b>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with logout_col:
        if st.button("Log out", type="secondary", use_container_width=True):
            with closing(auth_get_connection()) as _conn:
                auth.log_out(_conn, st.session_state.get("session_token"))
            # Evicts just this token's cached _validate_session_cached()
            # result. Without this, another tab/session still holding this
            # same now-deleted token in ITS OWN session_state (so it never
            # touches the cookie/rerun-gate above at all -- see the
            # `if st.session_state.get("session_token")` short-circuit near
            # the top of the file) would keep reading a stale "Valid"
            # result out of the cache for up to SESSION_VALIDATION_TTL_S
            # more seconds after this logout, instead of finding out on its
            # very next rerun the way it did before that cache existed.
            _validate_session_cached.clear(st.session_state.get("session_token"))
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
# Reorganized into tabs (pure layout change -- same widgets, same keys,
# same session_state, same function calls as before; only the physical
# grouping changed, from two side-by-side columns holding everything to
# five topic tabs). The header row above and the sidebar (Live ERP
# State) stay exactly where they were -- always visible regardless of
# which tab is active.
tab_process, tab_inbox, tab_outbound, tab_hold, tab_history = st.tabs(
    ["Process Email", "Inbox", "Outbound Queue", "On Hold", "History"]
)

with tab_process:
    pe_col1, pe_col2 = st.columns(2)

    with pe_col1:
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
            st.info("No sample test cases available — write your own email below.")
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
            use_container_width=True,
            disabled=not email_body.strip() or not sender_email.strip() or bool(DB_INIT_ERROR),
        )

    with pe_col2:
        st.markdown(section_heading_html("Agent Execution"), unsafe_allow_html=True)

        if run_clicked:
            with st.status("Processing email...", expanded=True) as status:
                try:
                    # One connection for this whole action -- run_agent's
                    # internal tool calls (get_order_details, verify_order_
                    # modification), the Hold/outbound-draft writes, and
                    # the optional auto-approve commit all share it,
                    # instead of each opening (and paying the Azure SQL
                    # handshake cost for) its own.
                    with closing(auth_get_connection()) as conn:
                        tool_log, final_result, reply = run_agent(
                            conn, email_body, on_step=_make_status_step_callback(status)
                        )
                        status.update(label="Done", state="complete")
                        # Persist to session_state so this survives the
                        # st.rerun() below (used to refresh the sidebar
                        # tables) instead of vanishing the moment the
                        # script re-executes.
                        st.session_state.last_result = {
                            "tool_log": tool_log,
                            "final_result": final_result,
                            "reply": reply,
                        }
                        # A fresh run supersedes whatever approval state
                        # was left over from the previous email.
                        st.session_state.pending_approval = None
                        st.session_state.last_commit_result = None

                        verified_change = find_verified_change(tool_log) if tool_log else None
                        clarification_request = find_clarification_request(tool_log) if tool_log else None

                        # Queue the drafted reply for human approval
                        # before it's ever sent -- same check/commit-style
                        # gate already used for database writes, now
                        # extended to outbound email. This must run
                        # BEFORE the st.rerun() below (which only fires
                        # for a verified change), so it happens for every
                        # outcome -- including a clarification question,
                        # which still needs to reach the customer.
                        #
                        # If this was a clarification request AND an
                        # order ID was actually given, place that order
                        # on hold so it isn't silently lost while waiting
                        # on the customer's reply -- see hold_requests.py
                        # / CLAUDE.md "Hold state design". The Message-ID
                        # is generated here, BEFORE the email is actually
                        # sent (sending is gated behind human approval),
                        # stored on the Hold now, and passed through to
                        # queue_draft() so approve_and_send() can set it
                        # as the real outgoing Message-ID header once
                        # approved -- this is what lets a real reply to
                        # this question (ingested later via IMAP) be
                        # matched back automatically. The HOLD-{id} tag in
                        # the subject/body is a fallback in case
                        # In-Reply-To/References ever gets stripped. No
                        # order ID -> nothing to hold; just queue the
                        # clarification email normally.
                        if clarification_request and clarification_request.get("order_id"):
                            sent_message_id = make_msgid()
                            hold_result = hold_requests.create_hold(
                                conn,
                                order_id=clarification_request["order_id"],
                                customer_email=sender_email.strip(),
                                inbound_sender=sender_email.strip(),
                                inbound_subject=original_subject,
                                inbound_body=email_body,
                                clarifying_question_sent=reply,
                                sent_message_id=sent_message_id,
                            )
                            hold_id = hold_result["hold_id"]
                            outbound_email.queue_draft(
                                conn,
                                to_email=sender_email.strip(),
                                subject=f"Re: {original_subject} [HOLD-{hold_id}]",
                                body=(
                                    f"{reply}\n\n"
                                    f"To help us match your reply to the right request, please keep "
                                    f"the reference HOLD-{hold_id} somewhere in your reply."
                                ),
                                message_id=sent_message_id,
                                context_note=f"Order {clarification_request['order_id']}",
                            )
                        else:
                            outbound_email.queue_draft(
                                conn,
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
                                commit_result = commit_order_modification(conn, **verified_change)
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
                    status.update(label="Done", state="error")
                    st.session_state.last_result = {"error": str(e)}

        if "last_result" in st.session_state:
            result = st.session_state.last_result

            if "error" in result:
                st.error(f"Agent error: {result['error']}")
            else:
                tool_log = result["tool_log"]
                final_result = result["final_result"]
                reply = result["reply"]

                st.markdown(field_label_html("Tool Call Chain"), unsafe_allow_html=True)
                if not tool_log:
                    st.markdown(stamp_html("No Action"), unsafe_allow_html=True)
                    st.caption("Claude replied without taking any action.")
                else:
                    for i, call in enumerate(tool_log, 1):
                        # Named tool_status, not status -- this used to
                        # shadow the st.status() widget object bound
                        # above (`with st.status(...) as status:`).
                        # Harmless today (that `with` block has already
                        # exited by the time this loop runs), but fragile
                        # -- a future edit calling status.update() after
                        # this loop would silently call .update() on a
                        # plain string instead.
                        tool_status = call["result"].get("status", "?")
                        detail_lines = [f"{k}: {v}" for k, v in call["inputs"].items()]
                        detail_lines.append(f"→ {tool_status}")
                        detail = "\n".join(detail_lines)
                        st.markdown(
                            step_card_html(i, call["tool_called"], tool_status, detail, delay_s=(i - 1) * 0.12),
                            unsafe_allow_html=True,
                        )

                final_status = final_result.get("status", "?") if final_result else "No Action"

                st.markdown(field_label_html("Outcome"), unsafe_allow_html=True)
                st.markdown(stamp_html(final_status), unsafe_allow_html=True)

                st.markdown(field_label_html("Reply to Customer"), unsafe_allow_html=True)
                st.markdown(f'<div class="reply-card">{reply}</div>', unsafe_allow_html=True)

                # --- Human approval gate for the database write ---
                if st.session_state.pending_approval:
                    pending = st.session_state.pending_approval
                    st.markdown(field_label_html("Database Write"), unsafe_allow_html=True)
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
                            with closing(auth_get_connection()) as conn:
                                commit_result = commit_order_modification(conn, **pending)
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
                    st.markdown(field_label_html("Database Write"), unsafe_allow_html=True)
                    st.markdown(stamp_html(commit_result.get("status", "?")), unsafe_allow_html=True)
                    st.caption(commit_result.get("message", ""))

        if not run_clicked and "last_result" not in st.session_state:
            st.info("Select or write an email on the left, then click **Process with Agent**.")

with tab_inbox:
    st.markdown(section_heading_html("Inbound Email"), unsafe_allow_html=True)

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
                with closing(auth_get_connection()) as conn:
                    inserted = email_ingestion.sync_fetched_emails(conn, fetched)
                st.toast(
                    f"Found {inserted} new email(s)." if inserted else "No new emails found.",
                    icon="📥",
                )
        st.rerun()

    if st.session_state.fetch_error:
        st.error(f"Could not check inbox: {st.session_state.fetch_error}")

    # Queried fresh from the database on every render, not session state
    # -- correct regardless of which browser session originally fetched
    # an email, and survives a page refresh. Reuses one connection for
    # both the read and (if Process/Process All was clicked) the whole
    # processing pass below -- a "Process All" batch of N emails used to
    # open a fresh connection for every one of N x several DB calls each;
    # now it's one connection for the entire click.
    with closing(auth_get_connection()) as conn:
        pending_emails = email_ingestion.get_pending_emails(conn)

        if pending_emails:
            st.markdown(field_label_html(f"{len(pending_emails)} email(s) pending processing"), unsafe_allow_html=True)
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
                if st.button("Process", key=f"process_ingested_{item['uid']}", use_container_width=True):
                    process_this_uid = item["uid"]

            # Only one of these can actually be true on any given run --
            # Streamlit only reports a click for the specific widget that
            # was clicked -- so there's no risk of double-processing here.
            if process_all_clicked:
                for item in pending_emails:
                    _process_pending_email(conn, item)
                st.rerun()
            elif process_this_uid:
                item = next(e for e in pending_emails if e["uid"] == process_this_uid)
                _process_pending_email(conn, item)
                st.rerun()
        else:
            st.markdown(empty_state_html("No emails currently pending processing."), unsafe_allow_html=True)

    if st.session_state.pending_approval_queue:
        st.divider()
        pending = st.session_state.pending_approval_queue[0]
        remaining = len(st.session_state.pending_approval_queue)
        label = "Database Write — from inbox"
        if remaining > 1:
            label += f" ({remaining} awaiting review)"
        st.markdown(field_label_html(label), unsafe_allow_html=True)
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
                with closing(auth_get_connection()) as conn:
                    commit_result = commit_order_modification(
                        conn,
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

with tab_outbound:
    st.markdown(section_heading_html("Pending Outbound Emails"), unsafe_allow_html=True)
    # One connection for both read-only queries that render this tab
    # (the pending queue and the sent-log audit trail below), instead of
    # a separate connection for each.
    with closing(auth_get_connection()) as conn:
        pending_drafts = outbound_email.get_pending_drafts(conn)
        sent_log = outbound_email.get_sent_log(conn)
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
                    with closing(auth_get_connection()) as conn:
                        send_result = outbound_email.approve_and_send(conn, draft["id"])
                    if send_result.get("status") == "Sent":
                        st.toast(send_result.get("message", "Email sent."), icon="📧")
                    else:
                        st.toast(f"Send failed: {send_result.get('message')}", icon="⚠️")
                    st.rerun()
            with reject_email_col:
                if st.button("❌ Reject", use_container_width=True, key=f"reject_email_{draft['id']}"):
                    with closing(auth_get_connection()) as conn:
                        outbound_email.reject_draft(conn, draft["id"])
                    st.toast("Draft discarded — not sent.", icon="🚫")
                    st.rerun()
            st.divider()
    else:
        st.markdown(empty_state_html("No outbound emails currently pending approval."), unsafe_allow_html=True)

    st.divider()
    st.markdown(section_heading_html("Sent Emails"), unsafe_allow_html=True)
    if sent_log:
        sent_df = pd.DataFrame(sent_log)[["to_email", "subject", "sent_at"]]
        sent_df.columns = ["Recipient", "Subject", "Sent At"]
        st.dataframe(sent_df, use_container_width=True, hide_index=True)
    else:
        st.markdown(empty_state_html("No emails sent yet."), unsafe_allow_html=True)

with tab_hold:
    st.markdown(section_heading_html("Awaiting Customer Reply"), unsafe_allow_html=True)
    # One connection for every read-only query that renders this tab
    # (all three sections below, including the per-candidate lookup
    # inside the Needs Manual Linking loop) -- previously each of these
    # opened its own connection, and the per-candidate lookup alone could
    # multiply that by however many ambiguous emails were queued.
    with closing(auth_get_connection()) as read_conn:
        awaiting_holds = hold_requests.get_awaiting_reply(read_conn)
        past_follow_up = hold_requests.get_past_follow_up(read_conn)
        manual_linking_items = hold_requests.get_manual_linking_emails(read_conn)
        manual_linking_candidates = {
            ml_item["uid"]: hold_requests.get_open_holds_for_sender(
                read_conn, parseaddr(ml_item["sender"])[1] or ml_item["sender"]
            )
            for ml_item in manual_linking_items
        }

    if awaiting_holds:
        for hold in awaiting_holds:
            st.markdown(stamp_html("Awaiting Reply"), unsafe_allow_html=True)
            st.markdown(
                f"**Order:** {html.escape(hold['order_id'] or '?')} — "
                f"**From:** {html.escape(hold['customer_email'])}"
            )
            st.markdown(
                f"""<div class="reply-card">
                <b>Customer wrote →</b><br>
                <i>Subject: {html.escape(hold['inbound_subject'])}</i><br><br>
                {html.escape(hold['inbound_body'])}
                </div>""",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"""<div class="reply-card">
                <b>Agent asked →</b><br><br>
                {hold['clarifying_question_sent']}
                </div>""",
                unsafe_allow_html=True,
            )
            st.divider()
    else:
        st.markdown(empty_state_html("No orders currently on hold."), unsafe_allow_html=True)

    st.divider()
    st.markdown(section_heading_html("Needs Attention"), unsafe_allow_html=True)
    if past_follow_up:
        for hold in past_follow_up:
            st.markdown(stamp_html("Past Follow-Up"), unsafe_allow_html=True)
            st.markdown(
                f"**Order:** {html.escape(hold['order_id'] or '?')} — "
                f"**From:** {html.escape(hold['customer_email'])}"
            )
            st.markdown(
                f"""<div class="reply-card">
                <b>Customer wrote →</b><br>
                <i>Subject: {html.escape(hold['inbound_subject'])}</i><br><br>
                {html.escape(hold['inbound_body'])}
                </div>""",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"""<div class="reply-card">
                <b>Agent asked →</b><br><br>
                {hold['clarifying_question_sent']}
                </div>""",
                unsafe_allow_html=True,
            )
            if hold.get("follow_up_body"):
                st.markdown(
                    f"""<div class="reply-card">
                    <b>Follow-up sent →</b><br><br>
                    {hold['follow_up_body']}
                    </div>""",
                    unsafe_allow_html=True,
                )
            if st.button("Mark Resolved", key=f"resolve_hold_{hold['id']}", use_container_width=True):
                with closing(auth_get_connection()) as conn:
                    hold_requests.resolve_hold(conn, hold["id"])
                st.toast("Hold marked resolved.", icon="✅")
                st.rerun()
            st.divider()
    else:
        st.markdown(empty_state_html("Nothing needs attention right now."), unsafe_allow_html=True)

    st.divider()
    st.markdown(section_heading_html("Needs Manual Linking"), unsafe_allow_html=True)
    if manual_linking_items:
        for ml_item in manual_linking_items:
            ml_sender_address = parseaddr(ml_item["sender"])[1] or ml_item["sender"]
            candidates = manual_linking_candidates[ml_item["uid"]]

            st.markdown(stamp_html("Needs Manual Linking"), unsafe_allow_html=True)
            st.markdown(
                f"**From:** {html.escape(ml_item['sender'])} — "
                f"**Subject:** {html.escape(ml_item['subject'])}"
            )
            st.markdown(
                f"""<div class="reply-card">{html.escape(ml_item['body'])}</div>""",
                unsafe_allow_html=True,
            )

            if candidates:
                st.caption(f"{len(candidates)} open hold(s) from this sender:")
                for candidate in candidates:
                    cand_col, link_col = st.columns([4, 1.3])
                    with cand_col:
                        st.markdown(
                            f"HOLD-{candidate['id']} — Order **{html.escape(candidate['order_id'] or '?')}** — "
                            f"*{html.escape(candidate['inbound_subject'])}*"
                        )
                    with link_col:
                        if st.button(
                            "Link", key=f"link_hold_{ml_item['uid']}_{candidate['id']}", use_container_width=True
                        ):
                            agent_input = _build_combined_context(candidate, ml_item["body"])
                            with closing(auth_get_connection()) as conn:
                                _run_agent_and_dispatch(conn, agent_input, ml_item, ml_sender_address, matched_hold=candidate)
                                hold_requests.remove_from_manual_linking(conn, ml_item["uid"])
                            st.rerun()
            else:
                # The candidate holds that made this ambiguous may have
                # since been resolved by the time a human looks at it --
                # nothing left to link to.
                st.markdown(empty_state_html("No open holds from this sender remain."), unsafe_allow_html=True)

            if st.button(
                "Not a reply — treat as new request",
                key=f"treat_as_new_{ml_item['uid']}",
                use_container_width=True,
            ):
                with closing(auth_get_connection()) as conn:
                    _run_agent_and_dispatch(conn, ml_item["body"], ml_item, ml_sender_address, matched_hold=None)
                    hold_requests.remove_from_manual_linking(conn, ml_item["uid"])
                st.rerun()
            st.divider()
    else:
        st.markdown(empty_state_html("Nothing needs manual linking right now."), unsafe_allow_html=True)

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
    with st.spinner("Connecting..."):
        inv, ords, db_error = get_db_snapshot()
    if db_error:
        st.error(f"Could not load live data:\n\n{db_error}")
    else:
        st.subheader("Inventory")
        st.dataframe(inv, use_container_width=True, hide_index=True)
        st.subheader("Orders")
        st.dataframe(ords, use_container_width=True, hide_index=True)

with tab_history:
    st.markdown(section_heading_html("Processed Emails"), unsafe_allow_html=True)
    with closing(auth_get_connection()) as conn:
        processed_emails = email_ingestion.get_processed_emails(conn)
    if processed_emails:
        processed_df = pd.DataFrame(processed_emails)[["sender", "subject", "final_status", "processed_at"]]
        processed_df.columns = ["Sender", "Subject", "Status", "Processed At"]
        st.dataframe(processed_df, use_container_width=True, hide_index=True)
    else:
        st.markdown(empty_state_html("No emails processed yet."), unsafe_allow_html=True)
