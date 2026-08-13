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

from agent_engine import run_agent
from theme import build_css, step_card_html, stamp_html

st.set_page_config(layout="wide", page_title="Reconciliation Agent", page_icon="📦")

# --- Theme state ---
if "theme" not in st.session_state:
    st.session_state.theme = "dark"

st.markdown(build_css(st.session_state.theme), unsafe_allow_html=True)


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
header_col, toggle_col = st.columns([5, 1])
with header_col:
    st.markdown(
        """
        <div class="manifest-title"><span class="accent-bar"></span>Reconciliation Agent</div>
        <div class="manifest-sub">PORTFOLIO PROTOTYPE · CLAUDE SONNET 5 · FICTIONAL COMPANY, FICTIONAL DATA</div>
        """,
        unsafe_allow_html=True,
    )
with toggle_col:
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
                if final_result and final_result.get("status") == "Success":
                    st.toast("Order reconciled — inventory and order tables updated.", icon="✅")
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

    if not run_clicked and "last_result" not in st.session_state:
        st.info("Select or write an email on the left, then click **Process with Agent**.")
