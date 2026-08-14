"""
Interview demo dashboard for the Reconciliation Agent portfolio project.

Design concept: a shipping-manifest / dispatch-office aesthetic (see
theme.py) -- monospace order/SKU codes, stamped status badges, and a
conveyor-style staggered reveal for the tool-call chain, instead of a
generic analytics-dashboard skin.

Run with: streamlit run app.py
"""
import json
import time

import pandas as pd
import sqlite3
import streamlit as st

from agent_engine import run_agent, commit_order_modification
from theme import build_css, step_card_html, stamp_html

st.set_page_config(layout="wide", page_title="Reconciliation Agent", page_icon="📦")

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


def get_db_snapshot():
    conn = sqlite3.connect("mock_erp.db")
    inv_df = pd.read_sql_query("SELECT * FROM inventory", conn)
    ord_df = pd.read_sql_query("SELECT * FROM orders", conn)
    conn.close()
    return inv_df, ord_df


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
        """
        <div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>
        <div class="manifest-sub">PORTFOLIO PROTOTYPE · CLAUDE SONNET 5 · FICTIONAL COMPANY, FICTIONAL DATA</div>
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

st.write("")

# --- Sidebar: live ERP ledger ---
with st.sidebar:
    st.header("Live ERP State")
    inv, ords = get_db_snapshot()
    st.subheader("Inventory")
    st.dataframe(inv, use_container_width=True, hide_index=True)
    st.subheader("Orders")
    st.dataframe(ords, use_container_width=True, hide_index=True)
    st.caption("Refreshes automatically after each successful reconciliation.")

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

    run_clicked = st.button("Process with Agent", type="primary", disabled=not email_body.strip())

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
