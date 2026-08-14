"""
Backend orchestration for the Reconciliation Agent portfolio project.

Runs a proper multi-step tool loop: Claude can call a sequence of tools
(e.g. get_order_details, THEN verify_order_modification) within a single
email's processing, rather than being limited to one tool call per email.
This is what makes relative-quantity requests ("add 2 more", "same qty,
different variant") work -- Claude looks the order up first, computes the
new absolute total itself, then proceeds to the modification check.

Three tool paths, matching agent_config.py:
  - get_order_details          -> read-only lookup, no DB write
  - verify_order_modification  -> checks the mock ERP for feasibility only
  - request_clarification      -> no DB write; packages the clarifying
                                   question, and short-circuits the loop
                                   since that question IS the reply

Human-approval gate: verify_order_modification (called from within
Claude's tool-use loop) only ever checks feasibility -- it never writes.
The actual database write lives in commit_order_modification, which is
NEVER called from this loop. It's called only from the app layer
(app.py), after a human has approved the proposed change -- either via
an explicit click or an auto-approve toggle. This means a wrong or
premature commit can't happen just because Claude decided to call a
tool.
"""
import json
import sqlite3

import anthropic

from agent_config import SYSTEM_PROMPT, TOOLS

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
MODEL = "claude-sonnet-5"
DB_PATH = "mock_erp.db"
MAX_TOOL_ROUNDS = 5  # safety cap so a confused agent can't loop forever


def get_order_details(order_id):
    """Read-only lookup. Never writes to the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT customer_name, sku, quantity, delivery_status FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()
    conn.close()

    if not order:
        return {"status": "Not Found", "message": f"Order {order_id} not found in system."}

    customer_name, sku, quantity, delivery_status = order
    return {
        "status": "Found",
        "order_id": order_id,
        "customer_name": customer_name,
        "current_sku": sku,
        "current_quantity": quantity,
        "delivery_status": delivery_status,
    }


def _validate_modification(cursor, order_id, mapped_sku, requested_quantity):
    """Shared feasibility logic used by both check_order_modification and
    commit_order_modification. Returns (error_dict, quantity_difference)
    -- error_dict is None when the change is feasible."""
    cursor.execute(
        "SELECT quantity, delivery_status FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = cursor.fetchone()

    if not order:
        return {"status": "Error", "message": f"Order {order_id} not found in system."}, None

    current_qty, status = order
    if status in ("Dispatched", "Delivered"):
        return {
            "status": "Rejected",
            "message": f"Cannot modify order. Status is already '{status}'.",
        }, None

    cursor.execute("SELECT available_stock FROM inventory WHERE sku = ?", (mapped_sku,))
    stock_record = cursor.fetchone()
    available_stock = stock_record[0] if stock_record else 0

    quantity_difference = requested_quantity - current_qty

    if quantity_difference > available_stock:
        return {
            "status": "Insufficient Stock",
            "message": (
                f"Requested {requested_quantity} units. "
                f"Short by {quantity_difference - available_stock} units."
            ),
        }, None

    return None, quantity_difference


def check_order_modification(order_id, mapped_sku, requested_quantity):
    """Read-only feasibility check against the mock ERP -- never writes.
    This is what Claude's tool-use loop calls. A 'Success' status here
    means the change is feasible, NOT that it has been applied; the
    actual write only happens later via commit_order_modification, after
    a human has approved it."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    error, _ = _validate_modification(cursor, order_id, mapped_sku, requested_quantity)
    conn.close()

    if error:
        return error
    return {"status": "Success", "message": "Change is feasible and pending approval."}


def commit_order_modification(order_id, mapped_sku, requested_quantity):
    """Re-validates from scratch, then applies the UPDATE statements. This
    is the only function that writes to the database -- it must NEVER be
    called from within Claude's tool-use loop, only from the app layer,
    after a human has approved the change."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    error, quantity_difference = _validate_modification(cursor, order_id, mapped_sku, requested_quantity)
    if error:
        conn.close()
        return error

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
        # This tool's own message IS the customer-facing reply -- no
        # further Claude call should happen once this fires, to avoid
        # Claude re-narrating it as a meta-summary.
        return result, True

    return {"status": "Error", "message": f"Unknown tool: {tool_name}"}, False


def run_agent(email_text):
    """Orchestrates a multi-step tool-use loop: Claude may call several
    tools in sequence (e.g. get_order_details, then verify_order_
    modification) before producing its final customer-facing reply."""

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
                # Claude never called a tool at all across the whole loop.
                return None, {"status": "No Action", "message": "Claude did not call a tool."}, reply
            return tool_call_log, tool_call_log[-1]["result"], reply

        tool_use = next(b for b in response.content if b.type == "tool_use")
        tool_name = tool_use.name
        tool_inputs = tool_use.input

        result, should_stop = _execute_tool(tool_name, tool_inputs)
        tool_call_log.append({"tool_called": tool_name, "inputs": tool_inputs, "result": result})

        if should_stop:
            return tool_call_log, result, result["message"]

        # Feed the tool result back and let Claude decide the next step
        # (another tool call, or a final reply).
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

    # Safety cap hit -- surface this rather than looping silently forever.
    return tool_call_log, {"status": "Error", "message": "Max tool rounds exceeded."}, (
        "Sorry, this request needs manual review — I wasn't able to resolve it automatically."
    )