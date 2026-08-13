"""
Agent configuration: system prompt + tool definitions for the
Reconciliation Agent portfolio project.

Fictional distributor, fictional products — see setup_db.py for the
mock inventory this prompt assumes.

Key design decisions (the interview-question answers):
  1. Claude has THREE tools, not one. It must call `request_clarification`
     instead of `verify_order_modification` whenever a product reference
     could map to more than one SKU. There is no numeric confidence
     threshold — any genuine ambiguity routes to a human question rather
     than a guess, because a wrong automated guess ships the wrong
     product, which is worse than a short delay.
  2. `get_order_details` exists because customers describe changes
     relatively ("add 2 more", "same quantity, different variant") far
     more often than in absolute final numbers. Without a way to look up
     the current order, Claude has no number to pass to
     `verify_order_modification` and previously just gave up and replied
     in prose instead of calling any tool. Now the agent looks the order
     up first, computes the new absolute quantity itself, then proceeds.
     This also surfaces "order not found" and "already dispatched" cases
     earlier, before an update is even attempted.
  3. `get_order_details` is called for EVERY email that includes an order
     ID, no exceptions — even when the ambiguity or change looks
     resolvable without it. Early testing showed Claude skipping the
     lookup for emails where it judged the ambiguity as self-contained
     (e.g. "swap the keg size" without needing the current order), which
     produced inconsistent behavior between similar emails and no audit
     trail for cases that turned out to need one. Removing the judgment
     call makes behavior predictable and gives every order-referencing
     email a consistent audit log entry, at the cost of a small number of
     extra tool calls (cheap, since it's a Haiku-cost lookup either way).
"""

SYSTEM_PROMPT = """You are the Autonomous Supply Chain Reconciliation Agent for a beverage wholesale distributor. Your job is to process inbound customer order-adjustment emails.

You must follow these strict operational rules:

1. Identify the Order ID, Customer Name, product reference, and requested change from the email.

2. MANDATORY ORDER LOOKUP: if the email includes an order ID, you MUST call `get_order_details` first — always, with no exceptions, even if you think you already have enough information to resolve the request without it (e.g. even if the ambiguity is purely about which SKU size/variant, not about the order's current state). This keeps behavior consistent across emails and gives every order-referencing request an audit trail. Only skip this step if the email contains no order ID at all.

3. If `get_order_details` reports the order was not found, or reports a delivery_status of 'Dispatched' or 'Delivered', do not proceed to `verify_order_modification` — go straight to explaining that in your reply (order not found: ask the customer to double check the reference; already dispatched: explain changes can't be made once it's left the warehouse).

4. RELATIVE VS ABSOLUTE QUANTITY: if the customer describes the change relatively rather than as a final number — "add 2 more", "same quantity, just swap the variant", "increase by X" — use the current quantity from `get_order_details` (step 2) to compute the absolute final quantity yourself before proceeding.

5. Map the customer's product reference to an exact inventory SKU using the catalog you've been given.

6. AMBIGUITY CHECK: if the customer's wording could plausibly refer to more than one SKU in the catalog (for example, they say "kegs" without specifying a size, or "the cider" without specifying a variant), you do NOT get to guess, even if one option seems more likely, and even if the order lookup didn't resolve it either. Call `request_clarification` instead of `verify_order_modification`. Never silently pick the "most probable" SKU when more than one genuinely matches what the customer wrote.

7. If the product reference is unambiguous and you have (or have computed) an absolute final quantity, you must ALWAYS call `verify_order_modification` to check database constraints before promising anything to the customer.

8. If that tool reports that delivery_status is 'Dispatched' or 'Delivered', reject the request politely; changes cannot be made once the order has left the warehouse.

9. If that tool reports 'Insufficient Stock', offer a partial fulfilment or flag an exception. Do not update the order.

10. Only confirm a change to the customer if `verify_order_modification` returns a 'Success' status.

11. When you call `request_clarification`, draft a short, specific question back to the customer listing the exact options they need to choose between (e.g. "Did you mean the 50L or 30L keg?"). Do not ask a vague "can you clarify?" question.
"""

# Tool 1: the original reconciliation check
verify_order_modification_tool = {
    "name": "verify_order_modification",
    "description": (
        "Validates if an order can be modified based on current inventory "
        "levels and delivery dispatch status. Only call this when the SKU "
        "is unambiguous."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "The unique order reference string (e.g., ORD-9941) extracted from the email.",
            },
            "mapped_sku": {
                "type": "string",
                "description": "The exact inventory SKU code matching the product the customer wants to change.",
                "enum": [
                    "HGL-LAG-50L",
                    "HGL-LAG-30L",
                    "ORC-CID-ORIG",
                    "ORC-CID-LIME",
                    "VLE-WNE-PGR",
                ],
            },
            "requested_quantity": {
                "type": "integer",
                "description": "The total final quantity the customer wants to change the item line to.",
            },
        },
        "required": ["order_id", "mapped_sku", "requested_quantity"],
    },
}

# Tool 2: the fix for the ambiguity gap. This is the tool Claude calls
# INSTEAD of verify_order_modification when the product reference doesn't
# resolve to exactly one SKU.
request_clarification_tool = {
    "name": "request_clarification",
    "description": (
        "Use this when the customer's product reference could match more "
        "than one SKU in the catalog, or when the requested change is "
        "otherwise ambiguous. Do NOT guess a SKU when this applies — call "
        "this tool instead so a human/customer can resolve the ambiguity."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "The order reference the ambiguity relates to, if known.",
            },
            "ambiguous_reference": {
                "type": "string",
                "description": "The exact phrase the customer used that was ambiguous (e.g. 'kegs', 'the cider').",
            },
            "candidate_skus": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The list of SKUs this reference could plausibly map to.",
            },
            "clarifying_question": {
                "type": "string",
                "description": "The specific question to send back to the customer, naming the actual options.",
            },
        },
        "required": ["ambiguous_reference", "candidate_skus", "clarifying_question"],
    },
}

# Tool 3: read-only lookup, added to fix the "relative quantity" gap.
# Claude must call this BEFORE verify_order_modification whenever the
# customer describes a change relatively (add X, same quantity, swap
# variant) rather than stating a final absolute number.
get_order_details_tool = {
    "name": "get_order_details",
    "description": (
        "Looks up the current SKU, quantity, and delivery status of an "
        "existing order. Read-only — does not modify anything. MUST be "
        "called for every email that includes an order ID, before any "
        "other tool — no exceptions, even if the request seems resolvable "
        "without it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "The order reference to look up (e.g., ORD-9941).",
            },
        },
        "required": ["order_id"],
    },
}

TOOLS = [verify_order_modification_tool, request_clarification_tool, get_order_details_tool]