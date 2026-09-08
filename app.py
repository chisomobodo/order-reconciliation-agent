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

st.markdown(build_css(st.session_state.theme), unsafe_allow_html=True)

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

# --- One-shot hard reload, right after login succeeds ---
# Root cause of the post-login FOUC (dashboard briefly rendering with no
# CSS applied -- huge unconstrained header icon, system fonts, no card
# borders -- confirmed reproducible on every login, never on a fresh
# load or manual refresh): the verify_clicked handler below mounts a
# stx.CookieManager(key="set_cookie_manager") component to WRITE the new
# session cookie. Mounting ANY custom component for the first time in a
# browser tab makes it report a value back to Streamlit -- and per the
# same mechanism already diagnosed once before in this file (the old
# cookie-read probe's spontaneous-rerun bug), that report triggers an
# involuntary rerun of the WHOLE script, on its own, outside anything
# this code controls.
#
# Confirmed directly: instrumented that handler with a debug file write
# placed right after its own deliberate st.rerun()-equivalent call --
# even though auth.verify_login_code() had already committed successfully
# (confirmed via a separate DB check), that debug write NEVER happened.
# The involuntary rerun above pre-empts the deliberate one before it
# finishes, every single time. Whatever code the login handler puts
# AFTER setting the cookie -- a plain st.rerun(), or an attempted real
# browser reload -- is dead code; the involuntary rerun always wins the
# race and is what actually lands the user on the dashboard. That
# explains the FOUC: this involuntary rerun does a normal in-place
# React reconciliation from the (small, shallow) login screen straight
# to the (much bigger) dashboard tree -- exactly the large structural
# DOM swap the FOUC has always tracked with, and exactly the one case
# no other rerun in the app ever has to do again afterward.
#
# Fighting to make one SPECIFIC rerun win that race is fragile -- it
# depends on exact timing of a component's own frontend lifecycle, not
# on anything this script can control. Instead: catch the transition
# here, at the one place EVERY rerun passes through right after a
# session is confirmed valid, regardless of which particular rerun (the
# deliberate one or the involuntary one) gets here first with a freshly
# populated session_token. needs_hard_reload_after_login is a plain
# session_state write made inside the login handler alongside its other
# post-verification state (session_token, login_step, etc.), and --
# this part matters -- all of it is written BEFORE that handler's own
# spinner/sleep, not after. Confirmed directly (per-line debug writes):
# Streamlit's rerun-cancellation can interrupt a running script even
# mid-time.sleep(), not just at the next st.* call, so anything written
# only after that sleep was silently lost whenever the involuntary rerun
# preempted it -- moving these writes earlier, to right after the DB
# call, is what makes them reliably land regardless of which rerun gets
# cut off. Whichever rerun is the first to see the flag true does a REAL
# top-level browser reload -- landing on a
# genuinely fresh HTML/CSS/JS document, the same FOUC-free path already
# confirmed for every ordinary page load and manual refresh -- then
# clears the flag immediately so the reload's own next script run (which
# also passes through this exact check) renders the dashboard normally
# instead of looping.
#
# A <meta http-equiv="refresh"> tag, not a <script>-based reload: an
# earlier attempt used components.html() to run
# `window.parent.location.reload()` from JS, which never worked --
# components.html() always renders into a sandboxed iframe lacking
# allow-top-navigation, so the browser silently blocks that frame from
# navigating its parent no matter what runs inside it. st.markdown(...,
# unsafe_allow_html=True) avoids the iframe entirely (inserted directly
# into the main page's own DOM) but can't run a <script> tag either --
# elements inserted via innerHTML never execute embedded scripts, by DOM
# spec. A <meta refresh> tag is subject to neither restriction: it's not
# a script, and it's not inside an iframe -- browsers act on it as soon
# as it's inserted anywhere in the document, not only at initial parse.
if st.session_state.get("needs_hard_reload_after_login"):
    st.session_state.needs_hard_reload_after_login = False
    st.markdown('<meta http-equiv="refresh" content="0">', unsafe_allow_html=True)
    st.stop()


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
                    with closing(app_core.auth_get_connection()) as _conn:
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
                        # Every session_state write for this login happens
                        # HERE, immediately, BEFORE the spinner/sleep below
                        # -- not after it, which is where this code used to
                        # put them. Mounting the CookieManager .set() call
                        # just above, the FIRST time this exact component
                        # key is used in this browser tab, makes it report
                        # a value back to Streamlit -- and per the same
                        # mechanism already diagnosed once before in this
                        # file (the old cookie-read probe's spontaneous-
                        # rerun bug), that report triggers an involuntary
                        # rerun of the whole script, on its own, racing
                        # this handler's own execution.
                        #
                        # Confirmed directly, with a debug file write
                        # placed at each successive line of this handler:
                        # execution reliably reaches the point right
                        # before the spinner/sleep below, but the write
                        # placed right AFTER that spinner/sleep block
                        # never happens -- Streamlit's rerun-cancellation
                        # interrupts the running script mid-block, even
                        # mid-time.sleep(), not just at the next st.*
                        # call. Anything that used to be written after the
                        # sleep (session_token, login_step, etc.) was
                        # therefore never actually committed by this run
                        # at all -- only the cookie survived, because that
                        # write happens client-side, inside the
                        # component's own iframe, decoupled from this
                        # Python thread's fate.
                        st.session_state.session_token = session_token
                        st.session_state.just_logged_out = False
                        st.session_state.login_step = "credentials"
                        st.session_state.login_user_id = None
                        st.session_state.login_message = None
                        # Tells the one-shot check near the top of this
                        # file (right after session validation, see
                        # "needs_hard_reload_after_login" there for the
                        # full explanation) to force a real browser
                        # reload instead of an in-place render, the next
                        # time ANY rerun sees a valid session_token here
                        # -- whichever rerun that turns out to be: this
                        # handler's own eventual st.rerun() below if it
                        # gets to run uninterrupted, or the involuntary
                        # one that may well fire first.
                        st.session_state.needs_hard_reload_after_login = True
                        # Purely cosmetic from here down: a moment showing
                        # "Signing you in..." gives the cookie write time
                        # to actually land in the browser before whichever
                        # rerun tears this component out of the tree
                        # (login_step already flipped away from "code"
                        # above, so it won't be recreated on the next
                        # run) -- this was why the cookie never showed up
                        # in the browser at all, not merely a slow-to-be-
                        # read one. Nothing after this point is load-
                        # bearing: even if THIS run gets cut off mid-sleep
                        # by the involuntary rerun, everything that
                        # matters has already committed above.
                        with st.spinner("Signing you in..."):
                            time.sleep(0.6)
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
