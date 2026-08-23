"""
Backend orchestration for the Reconciliation Agent portfolio project --
Azure SQL Database version.

Identical logic to agent_engine.py (the local SQLite version) -- same
multi-step tool loop, same check/commit split for the approval gate --
just swaps sqlite3 for pyodbc, connecting to Azure SQL Database instead
of a local .db file.

Reads connection details from environment variables (same ones used by
setup_db_azure.py):
  AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD

Approval model (unchanged from the local version):
  Claude never writes to the database directly. verify_order_modification
  only checks whether a change WOULD succeed and reports back -- it does
  not commit. The actual write happens via commit_order_modification(),
  called separately by the app layer, either immediately (auto-approve
  mode) or only after a human clicks Approve (manual-approval mode).
"""
import json
import os

import anthropic
import pyodbc
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from agent_config import SYSTEM_PROMPT, TOOLS

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
MODEL = "claude-sonnet-5"
MAX_TOOL_ROUNDS = 5  # safety cap so a confused agent can't loop forever

SERVER = os.environ["AZURE_SQL_SERVER"]
DATABASE = os.environ["AZURE_SQL_DATABASE"]
USERNAME = os.environ["AZURE_SQL_USERNAME"]
PASSWORD = os.environ["AZURE_SQL_PASSWORD"]

# The Linux ODBC driver (used inside the Docker container) does not
# auto-append the server name to the login the way the Windows driver
# does -- it must be explicit as "username@short-server-name", or the
# login silently hangs until timeout. Harmless to include on Windows too.
SERVER_SHORT_NAME = SERVER.split(".")[0]
UID = f"{USERNAME}@{SERVER_SHORT_NAME}"

CONNECTION_STRING = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{SERVER},1433;"
    f"DATABASE={DATABASE};"
    f"UID={UID};"
    f"PWD={PASSWORD};"
    # Short timeout so a cold connection fails fast rather than eating
    # most of the retry budget in app.py's get_db_snapshot() on a single
    # attempt -- a warm connection succeeds well within 10s regardless.
    "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=10;"
)


def get_connection():
    return pyodbc.connect(CONNECTION_STRING)


# Fallback cache for callers outside a real Streamlit session (test_agent.py,
# run_batch_test.py, test_azure_connection.py all call run_agent() directly
# via `python script.py`, not `streamlit run`). Never used when the app is
# actually running -- see _order_cache().
_FALLBACK_ORDER_CACHE = {}


def _order_cache():
    """dict to cache get_order_details lookups in, keyed by order_id.
    Backed by st.session_state when running inside a real Streamlit
    session, so each user's cache is their own -- one visitor's lookups
    can never leak into another's. Falls back to a plain module-level dict
    for the CLI entry points, which have no session concept anyway (each
    invocation is its own short-lived process)."""
    if get_script_run_ctx() is not None:
        if "order_details_cache" not in st.session_state:
            st.session_state.order_details_cache = {}
        return st.session_state.order_details_cache
    return _FALLBACK_ORDER_CACHE


def get_order_details(order_id):
    """Read-only lookup. Never writes to the database.

    Cached per session (see _order_cache) since the same order is often
    looked up more than once while working through an email. The cache is
    invalidated for a specific order_id the moment commit_order_modification
    writes a change to it, so a hit here can never return pre-update data."""
    cache = _order_cache()
    if order_id in cache:
        return cache[order_id]

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT customer_name, sku, quantity, delivery_status FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()
    conn.close()

    if not order:
        result = {"status": "Not Found", "message": f"Order {order_id} not found in system."}
    else:
        customer_name, sku, quantity, delivery_status = order
        result = {
            "status": "Found",
            "order_id": order_id,
            "customer_name": customer_name,
            "current_sku": sku,
            "current_quantity": quantity,
            "delivery_status": delivery_status,
        }

    cache[order_id] = result
    return result


def check_order_modification(order_id, mapped_sku, requested_quantity):
    """Read-only feasibility check -- does NOT write to the database."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT quantity, delivery_status FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()

    if not order:
        conn.close()
        return {"status": "Error", "message": f"Order {order_id} not found in system."}

    current_qty, status = order
    if status in ("Dispatched", "Delivered"):
        conn.close()
        return {
            "status": "Rejected",
            "message": f"Cannot modify order. Status is already '{status}'.",
        }

    cursor.execute("SELECT available_stock FROM inventory WHERE sku = ?", (mapped_sku,))
    stock_record = cursor.fetchone()
    available_stock = stock_record[0] if stock_record else 0
    conn.close()

    quantity_difference = requested_quantity - current_qty

    if quantity_difference > available_stock:
        return {
            "status": "Insufficient Stock",
            "message": (
                f"Requested {requested_quantity} units. "
                f"Short by {quantity_difference - available_stock} units."
            ),
        }

    return {
        "status": "Success",
        "message": "Change is feasible and awaiting commit.",
        "order_id": order_id,
        "mapped_sku": mapped_sku,
        "requested_quantity": requested_quantity,
    }


def commit_order_modification(order_id, mapped_sku, requested_quantity):
    """Actually writes the change to the database. Re-validates from
    scratch to guard against the order's state having changed between
    check and approval."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT quantity, delivery_status FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()

    if not order:
        conn.close()
        return {"status": "Error", "message": f"Order {order_id} not found in system."}

    current_qty, status = order
    if status in ("Dispatched", "Delivered"):
        conn.close()
        return {
            "status": "Rejected",
            "message": f"Cannot modify order. Status is already '{status}'.",
        }

    cursor.execute("SELECT available_stock FROM inventory WHERE sku = ?", (mapped_sku,))
    stock_record = cursor.fetchone()
    available_stock = stock_record[0] if stock_record else 0

    quantity_difference = requested_quantity - current_qty

    if quantity_difference > available_stock:
        conn.close()
        return {
            "status": "Insufficient Stock",
            "message": (
                f"Requested {requested_quantity} units. "
                f"Short by {quantity_difference - available_stock} units."
            ),
        }

    try:
        cursor.execute(
            "UPDATE inventory SET available_stock = available_stock - ? WHERE sku = ?",
            (quantity_difference, mapped_sku),
        )
        cursor.execute(
            "UPDATE orders SET quantity = ? WHERE order_id = ?",
            (requested_quantity, order_id),
        )
        conn.commit()
        # The cached get_order_details result for this order is now stale
        # -- drop it immediately so the next lookup hits the database.
        _order_cache().pop(order_id, None)
        return {"status": "Success", "message": "Order and inventory successfully reconciled."}
    except Exception as e:
        conn.rollback()
        return {"status": "Error", "message": str(e)}
    finally:
        conn.close()


def package_clarification(ambiguous_reference, candidate_skus, clarifying_question):
    """No DB write here — this path never touches inventory or orders."""
    return {
        "status": "Needs Clarification",
        "message": clarifying_question,
        "ambiguous_reference": ambiguous_reference,
        "candidate_skus": candidate_skus,
    }


def _execute_tool(tool_name, tool_inputs):
    """Dispatches a single tool call to its local implementation.
    Returns the result dict, and a flag for whether this tool should
    short-circuit the loop (used only by request_clarification)."""
    if tool_name == "get_order_details":
        return get_order_details(order_id=tool_inputs["order_id"]), False

    if tool_name == "verify_order_modification":
        result = check_order_modification(
            order_id=tool_inputs["order_id"],
            mapped_sku=tool_inputs["mapped_sku"],
            requested_quantity=tool_inputs["requested_quantity"],
        )
        return result, False

    if tool_name == "request_clarification":
        result = package_clarification(
            ambiguous_reference=tool_inputs["ambiguous_reference"],
            candidate_skus=tool_inputs["candidate_skus"],
            clarifying_question=tool_inputs["clarifying_question"],
        )
        return result, True

    return {"status": "Error", "message": f"Unknown tool: {tool_name}"}, False


def run_agent(email_text):
    """Orchestrates a multi-step tool-use loop against Azure SQL Database."""

    messages = [{"role": "user", "content": email_text}]
    tool_call_log = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            text_blocks = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_blocks)
            if not tool_call_log:
                return None, {"status": "No Action", "message": "Claude did not call a tool."}, reply
            return tool_call_log, tool_call_log[-1]["result"], reply

        tool_use = next(b for b in response.content if b.type == "tool_use")
        tool_name = tool_use.name
        tool_inputs = tool_use.input

        result, should_stop = _execute_tool(tool_name, tool_inputs)
        tool_call_log.append({"tool_called": tool_name, "inputs": tool_inputs, "result": result})

        if should_stop:
            return tool_call_log, result, result["message"]

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                }
            ],
        })

    return tool_call_log, {"status": "Error", "message": "Max tool rounds exceeded."}, (
        "Sorry, this request needs manual review — I wasn't able to resolve it automatically."
    )