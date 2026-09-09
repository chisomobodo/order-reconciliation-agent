"""
Entry point / router for Arbiter -- an AI order-reconciliation agent
portfolio project.

Design concept: a shipping-manifest / dispatch-office aesthetic (see
theme.py) -- monospace order/SKU codes, stamped status badges, and a
conveyor-style staggered reveal for the tool-call chain, instead of a
generic analytics-dashboard skin.

This file owns everything that must run exactly once, at the top of
every script execution, before any page-specific content: page config,
global CSS injection, the DB-unreachable fallback, session-cookie
read + validation, the post-login hard-reload check, and the login/
sign-up screen shown whenever there's no valid session. Once a session
is valid, it computes the user's role (rbac.py) and hands off to
st.navigation() -- see the bottom of this file -- which routes to
dashboard_page.py (everyone) or admin_page.py (role == 'admin' only).

Shared helpers those page files also need (connection handling, backend
selection, the branded header) live in app_core.py, not here -- a page
file can't safely `import app` to reach anything in THIS file, since
Streamlit runs this script as __main__, and a later `import app` from
elsewhere loads it a SECOND time as a distinct module, re-running
st.set_page_config() and crashing. See app_core.py's own docstring.

Run with: streamlit run app.py
"""
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import extra_streamlit_components as stx
from extra_streamlit_components.CookieManager import _component_func as _cookie_component
import streamlit as st

import admin_page
import app_core
import auth
import auth_theme
import dashboard_page
import rbac
from theme import build_css

st.set_page_config(layout="wide", page_title="Arbiter", page_icon="assets/arbiter_favicon.png")

# --- Theme state ---
if "theme" not in st.session_state:
    st.session_state.theme = "dark"

# build_css() (palette, buttons, tabs, stamps -- applies everywhere) and
# auth_theme.build_auth_css() (fonts, icon sizing, hero/form layout,
# input restyle -- rules that don't already have an auth-only class are
# scoped internally to `body:has(.auth-hero)`, see that function's
# docstring) are concatenated into ONE st.markdown() call here rather
# than injected as two separate calls at two different points in the
# script. Streamlit ships each st.markdown() as its own WebSocket delta
# and the frontend paints deltas as they arrive, not after the whole
# script finishes -- with two separate calls (the old code had
# build_auth_css() called ~270 lines later, inside _render_auth_screen(),
# after the DB_INIT_ERROR check, the session-cookie read, and session
# validation), there was a real, confirmed (via a mid-rerun screenshot)
# window where build_css() had painted but build_auth_css() hadn't:
# dark background and styled buttons/tabs/stamps, but native fonts, an
# unconstrained SVG icon, and Streamlit's raw "Press Enter to submit
# form" hint text. One call means one delta, closing that window
# entirely rather than just shrinking it -- and this line runs
# unconditionally, before the DB_INIT_ERROR branch and everything else,
# so no rerun (including the involuntary ones on the code-entry screen)
# can skip it.
st.markdown(
    build_css(st.session_state.theme) + auth_theme.build_auth_css(st.session_state.theme),
    unsafe_allow_html=True,
)

# --- Auth gate ---
# Nothing below this block renders until there's a valid session. If the
# backend itself couldn't be reached (DB_INIT_ERROR), there's no usable
# connection for auth OR the app, so that error is shown instead of a
# login form that would just fail on every attempt.
if app_core.DB_INIT_ERROR:
    st.markdown(app_core.render_app_header(), unsafe_allow_html=True)
    # Clean, simple primary message for the end user -- the actual
    # exception and the env vars to check are real diagnostic info
    # (useful to whoever runs/deploys this), but they're internal
    # detail that doesn't belong in front of someone just trying to use
    # the app, so they're tucked behind an explicit expander instead.
    st.error("Couldn't connect to the database. Please try again shortly.")
    with st.expander("Technical details"):
        st.markdown(
            f"USE_AZURE_DB is set, but the app couldn't connect to Azure SQL: {app_core.DB_INIT_ERROR}\n\n"
            "Check that AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, and "
            "AZURE_SQL_PASSWORD are set correctly, then restart the app. Or set "
            "USE_AZURE_DB=false (or unset it) to fall back to the local SQLite backend."
        )
    st.stop()

# Database schema setup (auth/session, inbox-tracking, outbound-email
# queue, hold-requests, rbac tables) no longer happens here. It used to
# run on every process's first page load (_ensure_*_schema(), gated
# behind @st.cache_resource) -- now it runs once at container startup,
# before Streamlit ever starts accepting traffic. See init_db.py and
# entrypoint.sh: the container's HTTP port doesn't open until schema
# init has already completed successfully, so no request from any user
# can race an uninitialized database.

# --- Session cookie: read side ---
# st.context.cookies is a native, read-only, synchronous snapshot of the
# cookies sent with the browser's initial request -- available
# immediately on the very first script execution. No custom component to
# mount, no iframe, no postMessage round-trip, no retry loop needed at
# all. This REPLACES an earlier approach (extra_streamlit_components'
# CookieManager, read via a raw _cookie_component(method="getAll", ...)
# call) that needed up to a 30-attempt/12s retry budget to work around
# that component's real, measured 1.7-9.7s mount+round-trip latency --
# and, worse, rendering the branded header on every one of those retry
# iterations produced a staged reveal on literally every single page
# load (logo appears immediately, then the page sits on it while the
# retries run, then the real content finally replaces it -- confirmed by
# video). Switching to this native API removes the wait it was built to
# survive, not just the visible symptom of it: confirmed directly (real
# Chrome via Playwright, a cookie set before navigation) that the value
# is correctly readable on the very first render, no polling required.
#
# Confirmed via the installed Streamlit version's own source
# (runtime/context.py): st.context.cookies wraps an immutable snapshot
# taken at the initial request, not a live-updating view -- so it will
# NOT reflect a cookie deleted by OUR OWN logout click within the same
# browser session (that deletion is a real, separate document.cookie
# write the ALREADY-ESTABLISHED session has no way to see until a whole
# new request/connection happens). just_logged_out below is what
# actually covers that gap, exactly as it did before this change.
#
# Writing (login) and deleting (logout) cookies still go through
# stx.CookieManager()/_cookie_component further down in this file --
# st.context.cookies is read-only, so those two call sites are
# unaffected by this change.
if "just_logged_out" not in st.session_state:
    st.session_state.just_logged_out = False

if st.session_state.get("session_token"):
    # This Python session already knows its own token (e.g. right after a
    # login in this very tab) -- no need to re-read the cookie.
    _session_token = st.session_state.session_token
elif st.session_state.just_logged_out:
    # We just tore this session down ourselves (see the Logout button) --
    # don't even check the cookie snapshot (see above for why it can't be
    # trusted here anyway), and don't run validate_session against a
    # token we know is already gone. This is what stops the logout click
    # from being able to briefly show a stale/invalid-session error.
    _session_token = None
else:
    _session_token = st.context.cookies.get("session_token")

# Validated on every single rerun, not just once -- this is what actually
# enforces the sliding 20-minute inactivity window (each call refreshes
# last_active_at) rather than trusting a stale login forever.
#
# Cached for app_core.SESSION_VALIDATION_TTL_S (@st.cache_data, same
# pattern as dashboard_page.get_db_snapshot): validate_session()'s own
# query is cheap, but the connection it needs is not -- opening a fresh
# one costs low single-digit ms on local SQLite (invisible) but several
# real seconds against Azure SQL (confirmed: a bare get_connection() call
# there took ~6.4s). Before this cache, that connection was opened fresh
# on EVERY rerun -- every reload, every button click, every widget
# interaction -- which blocked the entire page behind that multi-second
# wait right after the header, before the auth screen or dashboard could
# render. The sliding window still works, just refreshed at most once
# per TTL instead of on literally every rerun -- a few-second slack on a
# 20-minute inactivity timeout, not a meaningful security loosening.
#
# The cached function itself lives in app_core.py, not here -- see that
# module's docstring for why (dashboard_page.py's Logout button needs to
# reach the exact same cached function object to evict a token it just
# deleted, and it can't safely `import app` to get it).
if _session_token:
    # This call happens unconditionally on every page load, before
    # anything else renders -- the highest-frequency, highest-blast-
    # radius connection in the file. auth_get_connection() above already
    # retries transient failures, but if Azure SQL is down for longer
    # than that whole retry budget, the exception still propagates here
    # and must not reach the user as a raw traceback -- same clean
    # message + expandable technical-details pattern already used for
    # DB_INIT_ERROR above, rather than Streamlit's default error box.
    try:
        # A cache MISS here (first check of a given token, or the TTL
        # expired) makes Streamlit's cache machinery show its OWN default
        # spinner while the wrapped function actually runs, labeled with
        # the function's raw name/args, e.g. "Running
        # `_validate_session_cached(...)`." -- confirmed via a real
        # screenshot, raw internal code shown to a real user, worse now
        # that auth_get_connection() can retry for several real seconds
        # underneath it (see above), stretching how long that name stays
        # visible. Checked Streamlit's actual cache_utils.py source
        # rather than guessing: this spinner is controlled ONLY by the
        # show_spinner= argument on the @st.cache_data decorator itself
        # (set on app_core._validate_session_cached) -- wrapping the call
        # site in a manual st.spinner here would NOT suppress it (that
        # only happens when the call is nested inside ANOTHER cached
        # function, not inside a plain st.spinner block), it would just
        # add a second, redundant one.
        _session_check = app_core._validate_session_cached(_session_token)
    except Exception as e:
        st.markdown(app_core.render_app_header(), unsafe_allow_html=True)
        st.error("Couldn't connect to the database. Please try again shortly.")
        with st.expander("Technical details"):
            st.markdown(
                f"Could not verify your session against {app_core.DB_MODE} after "
                f"{app_core.CONNECT_MAX_ATTEMPTS} attempts: {e}\n\n"
                "This is usually a transient cold-start delay -- reloading the page in "
                "a few moments often resolves it."
            )
        st.stop()
else:
    _session_check = {"status": "Invalid"}

if _session_check.get("status") == "Valid":
    st.session_state.session_token = _session_token
    st.session_state.just_logged_out = False
else:
    st.session_state.pop("session_token", None)

# --- Post-login hard reload: gated behind a REAL cookie-readback check ---
# Root cause of the post-login FOUC (dashboard briefly rendering with no
# CSS applied): the verify_clicked handler below mounts a
# stx.CookieManager(key="set_cookie_manager") component to WRITE the new
# session cookie, and mounting ANY custom component for the first time
# in a browser tab makes it report a value back to Streamlit -- which
# triggers an involuntary rerun outside this script's control. Fighting
# to make one specific rerun win that race is fragile, so the fix is to
# force a REAL top-level browser reload (landing on a genuinely fresh
# document -- the same FOUC-free path already confirmed for every
# ordinary page load) the next time ANY rerun sees a valid session_token
# here, regardless of which rerun that turns out to be.
#
# THIS PART WAS WRONG in an earlier version, confirmed by real video
# evidence of it failing live: that version fired the reload after a
# blind `time.sleep(0.6)` following the cookie .set() call, assuming
# 0.6s of SERVER-side sleep was "enough time" for the cookie write to
# land in the BROWSER. It isn't a real synchronization primitive at
# all -- the server sleeping has no guaranteed relationship to when the
# browser-side iframe actually finishes running its
# `document.cookie = ...` write, particularly on a first mount, which
# has to load that iframe's own JS bundle first. When it wasn't enough
# time, the reload fired anyway, landed on a fresh page with no
# session_token cookie present yet, and the server correctly (from ITS
# perspective) rendered the logged-out screen -- reproducing exactly as
# "verify succeeds, reload fires, lands on a blank login form."
#
# Replaced with genuine confirmation: post_login_reload_phase drives a
# small state machine, re-entered on every rerun via this same check --
#   "confirm_cookie" -- poll the cookie component's OWN read side
#     (_cookie_component(method="getAll", ...), NOT st.context.cookies,
#     which is a frozen snapshot of the browser's ORIGINAL request and
#     will never reflect a cookie written mid-session) with a FRESH,
#     uniquely-keyed component instance each attempt, so each check is
#     a real new mount forcing a real new browser-side read rather than
#     a cached/stale value. Only once that readback genuinely contains
#     the expected session_token -- an actual confirmed fact, not a
#     guess -- does it advance to "reload". Bounded at
#     POST_LOGIN_COOKIE_MAX_ATTEMPTS attempts; if the cookie still
#     hasn't shown up after all of them (should be rare -- this is a
#     same-origin, no-network browser operation, not something that
#     should routinely take seconds), it still falls through to reload
#     rather than stranding the user on the code screen forever, but
#     logs loudly that it had to give up waiting, so a real recurrence
#     is actually diagnosable from container logs instead of silently
#     reproducing the old bug.
#   "reload" -- do the actual navigation.
# Every step below is logged (print(..., flush=True), visible in
# `az containerapp logs show` in production) with a timestamp, exactly
# per the user's request: log the verify success, log the cookie write
# being initiated (in the verify_clicked handler below), log every
# confirmation attempt and its result, log the moment reload actually
# fires.
#
# Still a <meta http-equiv="refresh"> tag, not a <script>-based reload:
# components.html() always renders into a sandboxed iframe lacking
# allow-top-navigation, so a script inside it can't navigate the parent
# page no matter what; st.markdown(unsafe_allow_html=True) avoids the
# iframe but can't run a <script> tag either -- elements inserted via
# innerHTML never execute embedded scripts, by DOM spec. A <meta
# refresh> tag is subject to neither restriction.
# These numbers are evidence-based, not guessed -- set from real
# az containerapp / Log Analytics data captured while reproducing this
# live on the actual deployed app (obtained via
# `az monitor log-analytics query` against ContainerAppConsoleLogs_CL,
# filtered on "login-reload", since per-replica `az containerapp logs
# show` only ever showed ONE of several replicas actually involved --
# see the "sticky sessions" note below). A first attempt at 8 attempts
# / 0.35s apart (~3s total) was measured live: most attempts confirmed
# cleanly within 2-4 tries, but several genuinely exhausted all 8
# attempts with the probe NEVER once reporting ready in that window --
# not "just barely too slow", the very first iframe mount+load hadn't
# finished at all yet under real cloud network/current-load conditions,
# something no local (loopback, single-instance) test ever surfaced.
# Widened to a real ~9s budget to actually cover that measured
# variance, not just the localhost-fast case.
POST_LOGIN_COOKIE_MAX_ATTEMPTS = 15
POST_LOGIN_COOKIE_RETRY_DELAY_S = 0.6

_post_login_phase = st.session_state.get("post_login_reload_phase")
if _post_login_phase == "confirm_cookie":
    _expected_token = st.session_state.get("post_login_expected_session_token")
    _attempt = st.session_state.get("post_login_cookie_attempts", 0) + 1
    st.session_state.post_login_cookie_attempts = _attempt
    _now = datetime.now(timezone.utc).isoformat()

    # A STABLE key across every attempt, deliberately NOT unique per
    # attempt -- confirmed by testing the other way first: a fresh key
    # each attempt forces a genuinely new component MOUNT each time,
    # meaning a brand new iframe that has to load its own JS bundle from
    # scratch before it can report anything -- with only
    # POST_LOGIN_COOKIE_RETRY_DELAY_S between attempts, none of them
    # ever finished loading in time to report back at all (confirmed via
    # real logs: probe_ready=False on every single one of 8 attempts).
    # A stable key mounts the iframe ONCE, on the first attempt; every
    # later attempt just asks that ALREADY-LOADED iframe to check
    # document.cookie again and report -- fast, since there's no bundle
    # to re-fetch -- which is what actually lets repeated polling work.
    _probe_result = _cookie_component(
        method="getAll",
        key="confirm_session_cookie_readback",
        default="__NOT_YET_REPORTED__",
    )
    _probe_is_ready = _probe_result != "__NOT_YET_REPORTED__"
    _cookie_seen = _probe_result.get("session_token") if _probe_is_ready and isinstance(_probe_result, dict) else None
    print(
        f"[login-reload] cookie confirm attempt {_attempt}/{POST_LOGIN_COOKIE_MAX_ATTEMPTS} at {_now}: "
        f"probe_ready={_probe_is_ready} session_token_seen={_cookie_seen!r} expected={_expected_token!r}",
        flush=True,
    )

    if _probe_is_ready and _cookie_seen == _expected_token:
        print(f"[login-reload] cookie CONFIRMED readable after {_attempt} attempt(s) at {_now} -- advancing to reload", flush=True)
        st.session_state.post_login_reload_phase = "reload"
        st.rerun()
    elif _attempt >= POST_LOGIN_COOKIE_MAX_ATTEMPTS:
        print(
            f"[login-reload] WARNING: gave up waiting for cookie confirmation after {_attempt} attempts at {_now} "
            f"-- reloading anyway (best effort) rather than stranding the user on the code screen. "
            f"If this recurs often, the cookie write itself is the problem, not just this check.",
            flush=True,
        )
        st.session_state.post_login_reload_phase = "reload"
        st.rerun()
    else:
        time.sleep(POST_LOGIN_COOKIE_RETRY_DELAY_S)
        st.rerun()
elif _post_login_phase == "reload":
    print(f"[login-reload] triggering hard reload at {datetime.now(timezone.utc).isoformat()}", flush=True)
    st.session_state.post_login_reload_phase = None
    st.session_state.post_login_expected_session_token = None
    st.session_state.post_login_cookie_attempts = 0
    st.markdown('<meta http-equiv="refresh" content="0">', unsafe_allow_html=True)
    st.stop()

# How long after a genuinely-sent login code to block a resend from the
# SAME browser session -- see the "Send login code" handler in
# _render_auth_screen() below for the two-layer duplicate-send fix this
# guards. 45s: long enough to absorb rapid double-clicks and Streamlit's
# rerun-cancellation race (see that handler's own comment), short enough
# that a user who genuinely didn't get their first code isn't stuck
# waiting.
LOGIN_CODE_COOLDOWN_S = 45


def _render_auth_screen():
    """Login / sign-up screen shown whenever there's no valid session.
    Real Streamlit widgets capture the input; auth_theme.py restyles them
    via CSS targeting Streamlit's own DOM (stHorizontalBlock/stColumn),
    not a hand-written wrapper div -- see auth_theme.build_auth_css.
    Each set of fields is inside an st.form so Enter submits the whole
    group at once instead of Streamlit's per-widget "Press Enter to
    Apply" behavior.

    build_auth_css() is NOT injected here any more -- it's bundled into
    the single unconditional st.markdown() call at the very top of the
    script (alongside build_css()) so both arrive as one atomic WebSocket
    delta on every rerun, closing the paint-order race this used to have.
    See that call site's comment."""

    for key, default in (
        ("login_step", "credentials"),
        ("login_user_id", None),
        ("login_message", None),
        ("signup_message", None),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    # Recover from a lost mid-login session. Confirmed reproducible:
    # sitting idle on the code-entry screen (code already requested, not
    # yet entered), the app would spontaneously bounce back to the
    # credentials screen with zero interaction -- st.session_state.
    # login_step/login_user_id are the ONLY record of "this browser is
    # mid-login," and they live purely server-side, tied to this one
    # script session. If that session is ever lost for reasons entirely
    # outside this code's control -- the frontend's WebSocket reconnects
    # onto a fresh server-side session, a load-balanced redeploy lands a
    # later request on a different backend replica, the session gets
    # garbage-collected -- login_step simply isn't in the new, empty
    # session_state at all, and the loop above correctly (from ITS
    # narrow perspective) defaults it back to "credentials", silently
    # discarding the user's progress with no error shown.
    #
    # Direct testing (idle wait, a forced WebSocket close/reopen, and a
    # blocked-reconnect simulation) never reproduced a spontaneous
    # session loss under this app's own control -- no component mounts
    # on this screen while idle, and every code path that touches
    # login_step is gated behind an explicit button click (confirmed by
    # grepping every reference to it). That means the trigger is
    # environment-level, not a bug in the click handlers themselves --
    # consistent with it being worse on mobile/slower connections, which
    # is exactly the situation that gives an environment-level session
    # loss more time to land mid-attempt. Rather than chase an exact
    # trigger further, this makes the app resilient to losing that
    # session for ANY such reason, the same way it already handles
    # losing session_state for a fully-logged-in user: a cookie.
    #
    # pending_login_token is set (see the "Send login code" handler
    # below) the moment a code is successfully requested, and matched
    # back to a user_id via login_codes.browser_token in the database --
    # NOT the raw user_id itself, which would let anyone override this
    # cookie to attempt guessing a different user's code without ever
    # knowing their password. It naturally stops being valid the moment
    # the code is used or expires (see restore_pending_login()), so
    # there's no separate cleanup needed for the normal case -- only the
    # explicit "Start over" click below needs to actively delete it, so
    # that choice doesn't get silently overridden by this same recovery
    # on the very next rerun.
    if st.session_state.login_step == "credentials" and st.session_state.login_user_id is None:
        _pending_login_token = st.context.cookies.get("pending_login_token")
        if _pending_login_token:
            with closing(app_core.auth_get_connection()) as _conn:
                _pending_result = auth.restore_pending_login(_conn, _pending_login_token, is_azure=app_core.USE_AZURE_DB)
            if _pending_result.get("status") == "Success":
                st.session_state.login_step = "code"
                st.session_state.login_user_id = _pending_result["user_id"]

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
                    # Layer 2 first, deliberately, even though it reads
                    # like the "backend" half and layer 1 (the spinner
                    # below) reads like the "frontend" half -- this check
                    # has to run and take effect BEFORE the slow part
                    # (auth_get_connection() + request_login_code()'s own
                    # SMTP round-trip) even starts, or it isn't a real
                    # guard at all, just a check that happens to usually
                    # win a race.
                    #
                    # That race is real, not theoretical, in this exact
                    # app: Streamlit's rerun-cancellation can interrupt an
                    # already-running script mid-statement, even mid-
                    # blocking-call (confirmed directly during the post-
                    # login FOUC investigation -- see the "Post-login hard
                    # reload" comment below). A second rapid
                    # click doesn't wait for the first click's script run
                    # to finish; it can cancel it outright, part-way
                    # through auth.request_login_code()'s SMTP call --
                    # possibly AFTER Gmail's server has already accepted
                    # the message, which no client-side cancellation can
                    # unsend. If the cooldown timestamp were only written
                    # once request_login_code() returns Success, an
                    # interrupted first click would never get to write it
                    # -- and a second click landing in that gap would see
                    # no cooldown at all and send a genuine duplicate.
                    #
                    # So the timestamp is written OPTIMISTICALLY, right
                    # here, before the slow call -- the instant this
                    # script run has decided to proceed -- and only
                    # rolled back if request_login_code() comes back with
                    # a definite, fast failure (wrong password / no such
                    # account, which returns before any email risk exists
                    # at all) that genuinely means no email went out. If
                    # THIS run itself gets interrupted mid-flight before
                    # reaching that rollback, the timestamp is left set --
                    # deliberately erring toward "assume a code may have
                    # been sent, keep the cooldown" rather than risking a
                    # real duplicate. A false-positive cooldown resolves
                    # itself in well under a minute; a duplicate code
                    # email is a real, visible bug the user already hit.
                    _login_code_now = time.monotonic()
                    _login_code_last_sent = st.session_state.get("login_code_last_sent_at")
                    _cooldown_remaining = (
                        LOGIN_CODE_COOLDOWN_S - (_login_code_now - _login_code_last_sent)
                        if _login_code_last_sent is not None else 0
                    )
                    if _cooldown_remaining > 0:
                        st.session_state.login_message = (
                            f"A code was already sent — please check your email, or wait "
                            f"{int(_cooldown_remaining) + 1}s before requesting another.",
                            "error",
                        )
                    else:
                        st.session_state.login_code_last_sent_at = _login_code_now
                        # Layer 1: immediate visual feedback -- the SMTP
                        # round-trip inside request_login_code() (worst
                        # case, a cold Azure SQL connection AND a slow
                        # Gmail handshake) can take several real seconds,
                        # long enough that a user with no feedback at all
                        # is exactly the person who clicks again.
                        with st.spinner("Sending code..."):
                            with closing(app_core.auth_get_connection()) as _conn:
                                result = auth.request_login_code(_conn, login_email.strip(), login_password)
                            if result.get("status") == "Success":
                                # Sets the "pending login" cookie -- see
                                # "Recover from a lost mid-login session"
                                # above for the full explanation of what
                                # this is for. Session_state writes happen
                                # BEFORE the sleep just below, not after --
                                # same reasoning already proven out at the
                                # verify-code cookie write further down:
                                # Streamlit's rerun-cancellation can
                                # interrupt this run mid-sleep, and
                                # anything written only after it risks
                                # never actually committing.
                                stx.CookieManager(key="set_pending_login_cookie_manager").set(
                                    "pending_login_token",
                                    result["browser_token"],
                                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=auth.CODE_VALID_MINUTES),
                                    key="set_pending_login_token",
                                )
                                st.session_state.login_user_id = result["user_id"]
                                st.session_state.login_step = "code"
                                st.session_state.login_message = (result["message"], "success")
                                # Gives the newly-mounted cookie component's
                                # iframe real time to actually execute
                                # `document.cookie = ...` before whatever
                                # rerun comes next tears it back out of the
                                # tree -- same pattern as every other
                                # cookie write in this file.
                                time.sleep(0.6)
                        if result.get("status") != "Success":
                            # Confirmed fast failure, before any email was
                            # ever at risk of being sent -- don't punish a
                            # mistyped password with a cooldown that was
                            # only ever meant to prevent duplicate emails.
                            st.session_state.login_code_last_sent_at = _login_code_last_sent
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
                    with closing(app_core.auth_get_connection()) as _conn:
                        result = auth.verify_login_code(_conn, st.session_state.login_user_id, code, is_azure=app_core.USE_AZURE_DB)
                        if result.get("status") == "Success":
                            # RBAC admin bootstrap: if this user's email is
                            # listed in ADMIN_EMAILS, force their role to
                            # 'admin' right now, on this same connection --
                            # this is how the first admin(s) ever get
                            # created, without a chicken-and-egg "an admin
                            # must promote you" UI flow for the very first
                            # one. Harmless no-op for everyone else (see
                            # rbac.ensure_admin_role's own docstring). Looks
                            # the email up by id rather than threading it
                            # through session_state from the credentials
                            # step above -- one extra query on an already-
                            # open connection, simpler than adding a new
                            # piece of state that only exists for this.
                            _cursor = _conn.cursor()
                            _cursor.execute("SELECT email FROM users WHERE id = ?", (st.session_state.login_user_id,))
                            _row = _cursor.fetchone()
                            if _row:
                                rbac.ensure_admin_role(_conn, st.session_state.login_user_id, _row[0])
                    if result.get("status") == "Success":
                        session_token = result["session_token"]
                        _logged_in_user_id = st.session_state.login_user_id  # captured before it's cleared below
                        print(
                            f"[login-reload] verify_login_code SUCCESS for user_id={_logged_in_user_id} "
                            f"at {datetime.now(timezone.utc).isoformat()}",
                            flush=True,
                        )
                        # Every session_state write for this login happens
                        # HERE, before the cookie mount below -- not after
                        # -- because mounting stx.CookieManager(), the
                        # FIRST time this exact component key is used in
                        # this browser tab, makes it report a value back
                        # to Streamlit, which triggers an involuntary
                        # rerun outside this script's control. Confirmed
                        # directly (per-line debug writes, in an earlier
                        # investigation): Streamlit's rerun-cancellation
                        # can interrupt a running script even mid-
                        # time.sleep(), not just at the next st.* call, so
                        # anything written only after a mount+sleep risked
                        # never actually committing if the involuntary
                        # rerun preempted it first.
                        st.session_state.session_token = session_token
                        st.session_state.just_logged_out = False
                        st.session_state.login_step = "credentials"
                        st.session_state.login_user_id = None
                        st.session_state.login_message = None
                        # Arms the confirm-then-reload state machine at
                        # the top of this file -- see its own long
                        # comment for the full explanation, including
                        # why this replaced a blind sleep that a real
                        # video-recorded reproduction proved unreliable.
                        st.session_state.post_login_reload_phase = "confirm_cookie"
                        st.session_state.post_login_expected_session_token = session_token
                        st.session_state.post_login_cookie_attempts = 0
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
                        print(
                            f"[login-reload] cookie SET call issued for user_id={_logged_in_user_id} "
                            f"at {datetime.now(timezone.utc).isoformat()}",
                            flush=True,
                        )
                        # Tidiness, not strictly required for correctness:
                        # restore_pending_login() already stops matching
                        # this browser_token the instant verify_login_code()
                        # marks the code used, so the cookie is already
                        # harmless from here -- but there's no reason to
                        # leave it sitting in the browser once the real
                        # login is complete.
                        _cookie_component(method="delete", cookie="pending_login_token", key="delete_pending_login_cookie_on_verify", default=False)
                        # A brief settle delay, same established need as
                        # every other cookie-mount-then-rerun spot in this
                        # file (Logout, Start Over): this is the FIRST
                        # time this exact component key mounts in this
                        # browser tab, and its iframe needs real wall-
                        # clock time to load and actually execute
                        # `document.cookie = ...` before the st.rerun()
                        # just below tears it back out of the tree.
                        # Confirmed necessary by testing THIS fix without
                        # it: removing this delay entirely (relying only
                        # on the confirm_cookie polling below to sort
                        # things out) resulted in the cookie never landing
                        # in the browser at all, 8/8 poll attempts unable
                        # to see it. This sleep is NOT what guarantees the
                        # reload waits for the real cookie, though --
                        # that's still entirely the job of the
                        # confirm_cookie phase at the top of this file.
                        # This is just giving the write itself a fair
                        # chance to happen at all.
                        with st.spinner("Signing you in..."):
                            time.sleep(0.6)
                    else:
                        st.session_state.login_message = (result.get("message", "Verification failed."), "error")
                    st.rerun()
                elif restart_clicked:
                    st.session_state.login_step = "credentials"
                    st.session_state.login_user_id = None
                    st.session_state.login_message = None
                    # Required here, not just tidiness (unlike the delete
                    # on the verify-success path above): without this, the
                    # pending-login recovery check near the top of this
                    # function would find this still-valid, still-unused
                    # code's browser_token again on the very next rerun
                    # and silently snap login_step right back to "code" --
                    # overriding the user's explicit choice to start over.
                    _cookie_component(method="delete", cookie="pending_login_token", key="delete_pending_login_cookie_on_restart", default=False)
                    with st.spinner("Starting over..."):
                        time.sleep(0.6)
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
                # One connection for the whole action -- the allowlist
                # check now needs one too (it consults the DB-backed
                # allowlist an admin manages via the Admin page's
                # Signup Access section, not just the env var), so it's
                # opened first and reused for sign_up()/ensure_admin_role()
                # below rather than opening a second one.
                with closing(app_core.auth_get_connection()) as _conn:
                    # Signup allowlist: checked BEFORE auth.sign_up() is
                    # ever called, so a not-allowed email never even
                    # reaches the "does this email already exist" query --
                    # rejected purely on the allowlist itself. See
                    # rbac.is_email_allowed_to_signup()'s own docstring for
                    # the DB-table / env-var / open-by-default precedence.
                    if not rbac.is_email_allowed_to_signup(_conn, signup_email.strip()):
                        st.session_state.signup_message = (
                            "This email isn't authorized to create an account — contact an administrator.",
                            "error",
                        )
                    else:
                        result = auth.sign_up(_conn, signup_email.strip(), signup_name.strip(), signup_password)
                        # RBAC admin bootstrap, same as the login-verify
                        # handler above -- sign_up() doesn't log the user
                        # in (they still go through the normal password +
                        # emailed-code flow separately), but there's no
                        # reason to make the very first admin wait for a
                        # second round-trip just to get their role set;
                        # doing it here means it's already correct by the
                        # time they do log in.
                        if result.get("status") == "Success" and result.get("user_id"):
                            rbac.ensure_admin_role(_conn, result["user_id"], signup_email.strip())
                        kind = "success" if result.get("status") == "Success" else "error"
                        st.session_state.signup_message = (result["message"], kind)
                st.rerun()


if _session_check.get("status") != "Valid":
    _render_auth_screen()
    st.stop()

CURRENT_USER = _session_check  # {"status": "Valid", "user_id", "email", "name"}

# --- Role + page routing ---
# Read live, every render, never cached -- same principle this app
# already applies to check_order_modification and every permission check
# in dashboard_page.py ("a stale cache can never be the reason an
# approval decision is wrong"). A role change made in the Admin page
# takes effect on this user's very next rerun, not up to
# SESSION_VALIDATION_TTL_S later.
with closing(app_core.auth_get_connection()) as _role_conn:
    CURRENT_USER["role"] = rbac.get_user_role(_role_conn, CURRENT_USER["user_id"])

# The Admin page object is only ever constructed -- let alone included in
# the list st.navigation() is given -- when the CURRENT role is 'admin'.
# This is what actually keeps it out of a non-admin's reach: st.navigation
# falls back to the default page for any URL that doesn't match a page in
# THIS run's list, so a non-admin who guesses/bookmarks the admin page's
# URL still never reaches admin_page.render_admin_page() -- there is no
# server-side route to it at all for their session, not just a hidden nav
# entry. Wrapped in lambdas (not passed as bare function references) so
# each page function receives this run's CURRENT_USER via closure --
# st.navigation calls a function-based page with no arguments.
_pages = [
    st.Page(lambda: dashboard_page.render_dashboard_page(CURRENT_USER), title="Dashboard", icon="📋", url_path="dashboard", default=True),
]
if CURRENT_USER["role"] == "admin":
    _pages.append(
        st.Page(lambda: admin_page.render_admin_page(CURRENT_USER), title="Admin", icon="🛡️", url_path="admin"),
    )

_current_page = st.navigation(_pages)
_current_page.run()
