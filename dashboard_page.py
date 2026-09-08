"""
Arbiter's main dashboard page -- the five tabs (Process Email, Inbox,
Outbound Queue, On Hold, History) plus the header and the "Live ERP
State" sidebar. Unchanged in behavior from when this all lived directly
in app.py; moved into its own page/function so app.py's st.navigation()
can route to it as a genuinely separate page, alongside admin_page.py.

Permission gating (new, via rbac.py): three of this page's actions are
checked against the CURRENT user's role/permissions, computed ONCE per
render (one connection, one round-trip) rather than per button -- see
_load_permissions() below. A denied action disables its button and
shows a short caption explaining why, rather than doing nothing on
click:
  - can_process_emails    -- "Process with Agent", "Check Inbox",
                              "Process"/"Process All", and the Hold
                              tab's "Link"/"treat as new request"
                              buttons (all of these run an email through
                              the agent, the same sensitive action
                              wherever it's triggered from)
  - can_approve_order_changes -- every "Approve & Apply" button, AND
                              (an extension past the literal spec, see
                              its own comment below) the auto-approve
                              toggle itself, since leaving that ungated
                              would let anyone bypass the Approve button
                              entirely
  - can_approve_outbound_emails -- "Approve & Send" AND "Reject" for
                              outbound email drafts (paired, since the
                              rbac spec explicitly audits both)

Every successful order-change approval and outbound-email approve/
reject is recorded via rbac.log_audit() -- see the three call sites
marked below. Order-change REJECTIONS are deliberately not gated or
audit-logged: discarding a proposed change touches neither the ERP nor
a customer inbox, unlike every action above.
"""
import html
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parseaddr, make_msgid

import extra_streamlit_components as stx
from extra_streamlit_components.CookieManager import _component_func as _cookie_component
import pandas as pd
import sqlite3
import streamlit as st

import app_core
import auth
import email_ingestion
import hold_requests
import outbound_email
import rbac
from theme import (
    email_card_html,
    empty_state_html,
    field_label_html,
    section_heading_html,
    step_card_html,
    stamp_html,
)


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


def _run_agent_and_dispatch(conn, agent_input: str, item: dict, sender_address: str,
                             current_user: dict, can_approve_order_changes: bool,
                             matched_hold: dict | None = None):
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

    can_approve_order_changes gates the auto-approve commit path below,
    the same permission the manual "Approve & Apply" button checks --
    without this, a user without approval rights could bypass that gate
    entirely just by having the auto-approve toggle left on (the toggle
    itself is also disabled for them, see render_dashboard_page(), but
    this is checked again here so a stale/tampered toggle state can't
    silently commit a write this user isn't allowed to approve).

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
            tool_log, final_result, reply = app_core.run_agent(conn, agent_input, on_step=_make_status_step_callback(status))
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
        if st.session_state.auto_approve and can_approve_order_changes:
            commit_result = app_core.commit_order_modification(conn, **verified_change)
            if commit_result.get("status") == "Success":
                st.cache_data.clear()
                st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                rbac.log_audit(
                    conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                    action="order_change_approved", target_type="order", target_id=str(verified_change["order_id"]),
                    details=f"auto-approved: SKU {verified_change['mapped_sku']} -> qty {verified_change['requested_quantity']}",
                )
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


def _process_pending_email(conn, item, current_user, can_approve_order_changes):
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
    _run_agent_and_dispatch(conn, agent_input, item, sender_address, current_user, can_approve_order_changes, matched_hold=matched_hold)


DB_SNAPSHOT_MAX_ATTEMPTS = 5  # Azure SQL only -- see get_db_snapshot
DB_SNAPSHOT_RETRY_DELAY_S = 1.5


@st.cache_data(ttl=5, show_spinner=False)
def get_db_snapshot():
    """Returns (inventory_df, orders_df, error). error is None on success;
    when set, the sidebar shows it instead of crashing the app.

    show_spinner=False: the caller already wraps this call in its own
    st.spinner("Connecting...") -- confirmed via Streamlit's actual
    cache_utils.py source that a cache miss's own default spinner
    ("Running `get_db_snapshot()`.") is NOT suppressed just because the
    call site happens to already be inside a manual st.spinner block
    (only nesting inside ANOTHER cached function does that), so without
    this, both spinners would render, one showing the raw function name.
    This was the same class of leak _validate_session_cached() had.

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
    if app_core.DB_INIT_ERROR:
        return None, None, app_core.DB_INIT_ERROR

    attempts = DB_SNAPSHOT_MAX_ATTEMPTS if app_core.USE_AZURE_DB else 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            conn = app_core.get_azure_connection() if app_core.USE_AZURE_DB else sqlite3.connect("mock_erp.db")
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


def _load_permissions(current_user: dict) -> dict:
    """One connection, one pass -- reads every permission this page
    gates, up front, rather than opening a fresh connection at each of
    the many individual button sites below. Never cached: permission
    checks are read live on every render, the same principle this app
    already applies to check_order_modification ("a stale cache can
    never be the reason an approval decision is wrong")."""
    with closing(app_core.auth_get_connection()) as conn:
        return {
            "can_process_emails": rbac.has_permission(conn, current_user["user_id"], "can_process_emails"),
            "can_approve_order_changes": rbac.has_permission(conn, current_user["user_id"], "can_approve_order_changes"),
            "can_approve_outbound_emails": rbac.has_permission(conn, current_user["user_id"], "can_approve_outbound_emails"),
        }


def _permission_caption(label: str):
    st.caption(f"🔒 You don't have permission to {label}. Contact an administrator.")


def render_dashboard_page(current_user: dict):
    """The main dashboard: header, five tabs, and the Live ERP State
    sidebar. current_user is the validated session dict from app.py
    ({"status": "Valid", "user_id", "email", "name", "role"})."""
    permissions = _load_permissions(current_user)
    can_process_emails = permissions["can_process_emails"]
    can_approve_order_changes = permissions["can_approve_order_changes"]
    can_approve_outbound_emails = permissions["can_approve_outbound_emails"]

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

    # --- Header ---
    # Computed here (rather than down inside `with user_col:` where it used
    # to live) because the mobile avatar button below also needs it, and
    # that button renders inside header_col, before user_col exists.
    first_name = (current_user.get("name") or "").split()
    first_name = first_name[0] if first_name else "User"

    # Two top-level columns, not four -- header_col (logo/badge/avatar) and
    # ONE menu_col holding every secondary control (auto-approve toggle,
    # theme toggle, "Logged in as X", Log out) via its own nested row. This
    # is what lets the mobile dropdown below work at all: CSS can hide/show
    # a single stColumn as one floating panel, but hiding/showing several
    # independent top-level columns the same way makes them each their own
    # absolutely-positioned box, stacking on top of each other instead of
    # flowing as a list. Wrapping them in one shared column first means
    # there's only ever one box to reposition.
    header_col, menu_col = st.columns([3.2, 4.2])
    with header_col:
        st.markdown(
            app_core.render_app_header() + f'<div style="margin-top: 8px;">{app_core.db_mode_badge_html(app_core.DB_MODE, bool(app_core.DB_INIT_ERROR))}</div>'
            # Mobile-only compact menu trigger -- a circular avatar button
            # showing the user's first initial, which reveals the auto-
            # approve toggle, theme toggle, "Logged in as X", and Log out as
            # a dropdown panel when tapped, instead of each rendering as its
            # own full-width stacked row (the old mobile behavior, which
            # pushed actual app content below a wall of secondary controls).
            # Pure CSS checkbox-hack: the checkbox has no visuals of its own
            # (display:none) and only drives :checked state; the label is
            # the visible tap target. Hidden entirely above the mobile
            # breakpoint -- see theme.py's @media (max-width: 640px) block,
            # which is also what makes menu_col hide by default and reveal
            # as the dropdown.
            + '<input type="checkbox" id="mobile-header-menu" class="mobile-menu-checkbox">'
            + f'<label for="mobile-header-menu" class="mobile-avatar-btn">{html.escape(first_name[:1].upper())}</label>',
            unsafe_allow_html=True,
        )
    with menu_col:
        approve_toggle_col, theme_toggle_col, user_col = st.columns([1.4, 1, 1.8])
        with approve_toggle_col:
            st.write("")
            # Gated by can_approve_order_changes -- not just literal-spec
            # compliance: auto-approve, when on, commits every verified
            # order change without a human ever clicking "Approve & Apply"
            # at all. Leaving this toggle available to someone without
            # approval rights would let them bypass that button's own gate
            # entirely. Forced off (not just disabled-with-old-value) so a
            # role change mid-session can't leave a stale "on" value armed
            # -- _run_agent_and_dispatch() also re-checks this permission
            # itself before actually committing, as a second layer.
            if not can_approve_order_changes:
                st.session_state.auto_approve = False
            st.toggle(
                "Auto-approve changes", key="auto_approve", value=False,
                disabled=not can_approve_order_changes,
                help=None if can_approve_order_changes else "You don't have permission to approve order changes.",
            )
        with theme_toggle_col:
            st.write("")
            label = "☀️ Light mode" if st.session_state.theme == "dark" else "🌙 Dark mode"
            if st.button(label, type="secondary", use_container_width=True):
                st.session_state.theme = "light" if st.session_state.theme == "dark" else "dark"
                st.rerun()
        with user_col:
            st.write("")
            # First name + Logout as one right-aligned group at the far
            # right of the header, rather than the full name stacked above
            # a full-width button. (first_name is computed above, near
            # header_col -- the mobile avatar button needs it too.)
            name_col, logout_col = st.columns([1.3, 1], gap="small")
            with name_col:
                st.markdown(
                    f"""
                    <div class="header-username" style="text-align:right; font-family:'IBM Plex Mono',monospace;
                                font-size:0.75rem; color:var(--text-dim); padding-top:8px;
                                padding-bottom:16px;">
                        Logged in as <b style="color:var(--text);">{first_name}</b>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with logout_col:
                if st.button("Log out", type="secondary", use_container_width=True):
                    with closing(app_core.auth_get_connection()) as _conn:
                        auth.log_out(_conn, st.session_state.get("session_token"))
                    # Evicts just this token's cached _validate_session_cached()
                    # result. Without this, another tab/session still holding this
                    # same now-deleted token in ITS OWN session_state (so it never
                    # touches the cookie/rerun-gate above at all) would keep
                    # reading a stale "Valid" result out of the cache for up to
                    # SESSION_VALIDATION_TTL_S more seconds after this logout,
                    # instead of finding out on its very next rerun the way it
                    # did before that cache existed. Lives in app_core.py, not
                    # here or in app.py -- see app_core.py's own docstring for
                    # why importing app.py itself from another module isn't
                    # safe.
                    app_core._validate_session_cached.clear(st.session_state.get("session_token"))
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
                disabled=not email_body.strip() or not sender_email.strip() or bool(app_core.DB_INIT_ERROR) or not can_process_emails,
            )
            if not can_process_emails:
                _permission_caption("process emails")

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
                        with closing(app_core.auth_get_connection()) as conn:
                            tool_log, final_result, reply = app_core.run_agent(
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
                                if st.session_state.auto_approve and can_approve_order_changes:
                                    commit_result = app_core.commit_order_modification(conn, **verified_change)
                                    st.session_state.last_commit_result = commit_result
                                    if commit_result.get("status") == "Success":
                                        st.cache_data.clear()  # sidebar shouldn't show the pre-write snapshot
                                        st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                                        rbac.log_audit(
                                            conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                                            action="order_change_approved", target_type="order", target_id=str(verified_change["order_id"]),
                                            details=f"auto-approved: SKU {verified_change['mapped_sku']} -> qty {verified_change['requested_quantity']}",
                                        )
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
                            if st.button(
                                "✅ Approve & Apply", type="primary", use_container_width=True,
                                disabled=not can_approve_order_changes,
                            ):
                                with closing(app_core.auth_get_connection()) as conn:
                                    commit_result = app_core.commit_order_modification(conn, **pending)
                                    if commit_result.get("status") == "Success":
                                        rbac.log_audit(
                                            conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                                            action="order_change_approved", target_type="order", target_id=str(pending["order_id"]),
                                            details=f"SKU {pending['mapped_sku']} -> qty {pending['requested_quantity']}",
                                        )
                                st.session_state.last_commit_result = commit_result
                                st.session_state.pending_approval = None
                                if commit_result.get("status") == "Success":
                                    st.cache_data.clear()  # sidebar shouldn't show the pre-write snapshot
                                    st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                                else:
                                    st.toast(f"Commit failed: {commit_result.get('message')}", icon="⚠️")
                                st.rerun()
                            if not can_approve_order_changes:
                                _permission_caption("approve order changes")
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
            disabled=bool(app_core.DB_INIT_ERROR) or not can_process_emails,
        )
        if not can_process_emails:
            _permission_caption("check the inbox or process emails")

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
                    with closing(app_core.auth_get_connection()) as conn:
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
        with closing(app_core.auth_get_connection()) as conn:
            pending_emails = email_ingestion.get_pending_emails(conn)

            if pending_emails:
                st.markdown(field_label_html(f"{len(pending_emails)} email(s) pending processing"), unsafe_allow_html=True)
                process_all_clicked = st.button(
                    "▶️ Process All",
                    type="primary",
                    use_container_width=True,
                    key="process_all_ingested",
                    disabled=not can_process_emails,
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
                    if st.button("Process", key=f"process_ingested_{item['uid']}", use_container_width=True, disabled=not can_process_emails):
                        process_this_uid = item["uid"]

                # Only one of these can actually be true on any given run --
                # Streamlit only reports a click for the specific widget that
                # was clicked -- so there's no risk of double-processing here.
                if process_all_clicked:
                    for item in pending_emails:
                        _process_pending_email(conn, item, current_user, can_approve_order_changes)
                    st.rerun()
                elif process_this_uid:
                    item = next(e for e in pending_emails if e["uid"] == process_this_uid)
                    _process_pending_email(conn, item, current_user, can_approve_order_changes)
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
                if st.button(
                    "✅ Approve & Apply", type="primary", use_container_width=True, key="ingest_approve",
                    disabled=not can_approve_order_changes,
                ):
                    with closing(app_core.auth_get_connection()) as conn:
                        commit_result = app_core.commit_order_modification(
                            conn,
                            order_id=pending["order_id"],
                            mapped_sku=pending["mapped_sku"],
                            requested_quantity=pending["requested_quantity"],
                        )
                        if commit_result.get("status") == "Success":
                            rbac.log_audit(
                                conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                                action="order_change_approved", target_type="order", target_id=str(pending["order_id"]),
                                details=f"from inbox: SKU {pending['mapped_sku']} -> qty {pending['requested_quantity']}",
                            )
                    st.session_state.pending_approval_queue.pop(0)
                    if commit_result.get("status") == "Success":
                        st.cache_data.clear()
                        st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
                    else:
                        st.toast(f"Commit failed: {commit_result.get('message')}", icon="⚠️")
                    st.rerun()
                if not can_approve_order_changes:
                    _permission_caption("approve order changes")
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
        with closing(app_core.auth_get_connection()) as conn:
            pending_drafts = outbound_email.get_pending_drafts(conn)
            sent_log = outbound_email.get_sent_log(conn)
        if pending_drafts:
            if not can_approve_outbound_emails:
                _permission_caption("approve or reject outbound emails")
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
                    if st.button(
                        "✅ Approve & Send", type="primary", use_container_width=True, key=f"approve_email_{draft['id']}",
                        disabled=not can_approve_outbound_emails,
                    ):
                        with closing(app_core.auth_get_connection()) as conn:
                            send_result = outbound_email.approve_and_send(conn, draft["id"])
                            if send_result.get("status") == "Sent":
                                rbac.log_audit(
                                    conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                                    action="outbound_email_approved", target_type="outbound_email", target_id=str(draft["id"]),
                                    details=f"to {draft['to_email']}: {draft['subject']}",
                                )
                        if send_result.get("status") == "Sent":
                            st.toast(send_result.get("message", "Email sent."), icon="📧")
                        else:
                            st.toast(f"Send failed: {send_result.get('message')}", icon="⚠️")
                        st.rerun()
                with reject_email_col:
                    if st.button(
                        "❌ Reject", use_container_width=True, key=f"reject_email_{draft['id']}",
                        disabled=not can_approve_outbound_emails,
                    ):
                        with closing(app_core.auth_get_connection()) as conn:
                            outbound_email.reject_draft(conn, draft["id"])
                            rbac.log_audit(
                                conn, actor_user_id=current_user["user_id"], actor_email=current_user["email"],
                                action="outbound_email_rejected", target_type="outbound_email", target_id=str(draft["id"]),
                                details=f"to {draft['to_email']}: {draft['subject']}",
                            )
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
        with closing(app_core.auth_get_connection()) as read_conn:
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
                    with closing(app_core.auth_get_connection()) as conn:
                        hold_requests.resolve_hold(conn, hold["id"])
                    st.toast("Hold marked resolved.", icon="✅")
                    st.rerun()
                st.divider()
        else:
            st.markdown(empty_state_html("Nothing needs attention right now."), unsafe_allow_html=True)

        st.divider()
        st.markdown(section_heading_html("Needs Manual Linking"), unsafe_allow_html=True)
        if manual_linking_items:
            if not can_process_emails:
                _permission_caption("process emails")
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
                                "Link", key=f"link_hold_{ml_item['uid']}_{candidate['id']}", use_container_width=True,
                                disabled=not can_process_emails,
                            ):
                                agent_input = _build_combined_context(candidate, ml_item["body"])
                                with closing(app_core.auth_get_connection()) as conn:
                                    _run_agent_and_dispatch(conn, agent_input, ml_item, ml_sender_address, current_user, can_approve_order_changes, matched_hold=candidate)
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
                    disabled=not can_process_emails,
                ):
                    with closing(app_core.auth_get_connection()) as conn:
                        _run_agent_and_dispatch(conn, ml_item["body"], ml_item, ml_sender_address, current_user, can_approve_order_changes, matched_hold=None)
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
        with closing(app_core.auth_get_connection()) as conn:
            processed_emails = email_ingestion.get_processed_emails(conn)
        if processed_emails:
            processed_df = pd.DataFrame(processed_emails)[["sender", "subject", "final_status", "processed_at"]]
            processed_df.columns = ["Sender", "Subject", "Status", "Processed At"]
            st.dataframe(processed_df, use_container_width=True, hide_index=True)
        else:
            st.markdown(empty_state_html("No emails processed yet."), unsafe_allow_html=True)
