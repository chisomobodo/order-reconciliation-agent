"""
Interview demo dashboard for the Reconciliation Agent portfolio project.

Design concept: a shipping-manifest / dispatch-office aesthetic (see
theme.py) -- monospace order/SKU codes, stamped status badges, and a
conveyor-style staggered reveal for the tool-call chain, instead of a
generic analytics-dashboard skin.

Run with: streamlit run app.py
"""
import json
import os
import time

import pandas as pd
import sqlite3
import streamlit as st

from theme import build_css, step_card_html, stamp_html

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

st.markdown(build_css(st.session_state.theme), unsafe_allow_html=True)


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
header_col, approve_toggle_col, theme_toggle_col = st.columns([4, 1.4, 1])
with header_col:
    st.markdown(
        f"""
        <div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>
        <div class="manifest-sub">PORTFOLIO PROTOTYPE · CLAUDE SONNET 5 · FICTIONAL COMPANY, FICTIONAL DATA</div>
        <div style="margin-top: 8px;">{db_mode_badge_html(DB_MODE, bool(DB_INIT_ERROR))}</div>
        """,
        unsafe_allow_html=True,
    )
    if DB_INIT_ERROR:
        st.error(
            f"USE_AZURE_DB is set, but the app couldn't connect to Azure SQL: {DB_INIT_ERROR}\n\n"
            "Check that AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, and "
            "AZURE_SQL_PASSWORD are set correctly, then restart the app. Or set "
            "USE_AZURE_DB=false (or unset it) to fall back to the local SQLite backend."
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

st.write("")

# --- Main layout ---
col1, col2 = st.columns(2)

with col1:
    st.subheader("Inbound Email")

    sample_emails = load_sample_emails()

    if sample_emails:
        options = ["-- Write your own --"] + [
            f"[{e['category']}] {e['id']} — {e['subject']}" for e in sample_emails
        ]
        choice = st.selectbox("Pick a test case, or write your own:", options)

        if choice == "-- Write your own --":
            email_body = st.text_area("Email content:", height=200, placeholder="Paste or type a customer email here...")
        else:
            idx = options.index(choice) - 1
            email_body = st.text_area("Email content:", value=sample_emails[idx]["body"], height=200)
    else:
        st.info("No sample_emails.json found — run generate_test_emails.py first for pre-built test cases.")
        email_body = st.text_area("Email content:", height=200, placeholder="Paste or type a customer email here...")

    run_clicked = st.button(
        "Process with Agent",
        type="primary",
        disabled=not email_body.strip() or bool(DB_INIT_ERROR),
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

                if final_result and final_result.get("status") == "Success":
                    verified_change = find_verified_change(tool_log)
                    if verified_change:
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
