"""
Scheduled job: checks for Hold requests past the 20-minute follow-up
window and drafts (never auto-sends) a follow-up email for each.

Intended to run as an Azure Container Apps Job on a schedule (e.g.
every 5-10 minutes) -- NOT as a background thread inside the web app,
since Container Apps scales the web app to zero when idle, which
would silently stop any in-process thread too. This script is a
separate, independent entrypoint precisely so it keeps running on
schedule regardless of whether anyone's visited the main app.

Design principle (do not weaken): this script NEVER modifies an
order, NEVER guesses a SKU/quantity, and NEVER sends an email
directly. It only:
  1. Finds holds past the follow-up window
  2. Drafts a follow-up email with honest wording (the order remains
     on hold, it does not get auto-processed)
  3. Queues that draft via outbound_email.queue_draft() -- still
     gated behind a human's "Approve & Send" click, same as every
     other outbound email in this project
  4. Marks the hold 'Past Follow-Up' so the dashboard surfaces it for
     a human to actively decide what to do next

Run manually for testing: python3 check_holds.py
"""
import os
import sys
from contextlib import closing

import hold_requests
import outbound_email

USE_AZURE_DB = os.environ.get("USE_AZURE_DB", "false").lower() == "true"

if USE_AZURE_DB:
    from agent_engine_azure import get_connection
else:
    from agent_engine import get_connection


FOLLOW_UP_SUBJECT_TEMPLATE = "Still awaiting your reply — order {order_id} [HOLD-{hold_id}]"

FOLLOW_UP_BODY_TEMPLATE = """Hi,

We haven't heard back regarding your order {order_id} yet. To recap, we asked:

"{clarifying_question}"

This request will remain on hold until we hear from you — nothing has been changed or processed automatically. Please reply whenever you're able to, and be sure to keep the reference HOLD-{hold_id} in your reply so we can match it to the right request.

Thanks for your patience.
"""


def run_check():
    """One pass: find holds due for a follow-up, draft (queue, don't
    send) a follow-up email for each, mark them 'Past Follow-Up'.

    Opens a single connection for the whole pass -- independent of the
    web app's own per-action connection handling, since this is a
    separate scheduled-job entrypoint (see module docstring) that never
    runs inside app.py's process."""
    with closing(get_connection()) as conn:
        due_holds = hold_requests.get_holds_due_for_follow_up(conn)

        if not due_holds:
            print("No holds currently due for a follow-up.")
            return {"checked": True, "follow_ups_drafted": 0}

        print(f"Found {len(due_holds)} hold(s) past the follow-up window.")

        drafted_count = 0
        for hold in due_holds:
            order_id = hold["order_id"] or "your request"
            subject = FOLLOW_UP_SUBJECT_TEMPLATE.format(
                order_id=order_id, hold_id=hold["id"]
            )
            body = FOLLOW_UP_BODY_TEMPLATE.format(
                order_id=order_id,
                clarifying_question=hold["clarifying_question_sent"],
                hold_id=hold["id"],
            )

            result = outbound_email.queue_draft(
                conn,
                to_email=hold["customer_email"],
                subject=subject,
                body=body,
                context_note=f"Follow-up for HOLD-{hold['id']} (order {order_id})",
            )

            if result["status"] == "Queued":
                hold_requests.mark_follow_up_sent(conn, hold["id"], body)
                drafted_count += 1
                print(f"  HOLD-{hold['id']}: follow-up drafted and queued for approval.")
            else:
                print(f"  HOLD-{hold['id']}: failed to queue follow-up -- {result}")

    print(f"Done. {drafted_count} follow-up(s) drafted and awaiting human approval.")
    return {"checked": True, "follow_ups_drafted": drafted_count}


if __name__ == "__main__":
    try:
        result = run_check()
        sys.exit(0)
    except Exception as e:
        print(f"check_holds.py failed: {e}", file=sys.stderr)
        sys.exit(1)