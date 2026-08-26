"""
Hold-state tracking for order requests awaiting customer clarification.

When request_clarification fires and an order ID was given, a
hold_requests row is created to track that this order is genuinely
paused pending a reply -- not lost, not silently guessed at.

Design principles (do not weaken these):
  - Stores the FULL original inbound email, not a summary -- a human
    reviewing this later needs real context, not a paraphrase that
    might have dropped a detail that mattered.
  - After a follow-up window with no reply, the record is flagged
    'Past Follow-Up' for a HUMAN to actively decide what to do next.
    Nothing here ever auto-processes an order. There is deliberately
    no code path that changes an order's quantity/SKU based on a
    timeout -- that would mean guessing on exactly the case that was
    too ambiguous to guess on in the first place.
  - If no order ID was ever given, there's nothing to place on hold --
    only the clarification email itself matters, and that's handled
    entirely by outbound_email.py, not this module.
"""
import os
import re
from datetime import datetime, timezone

FOLLOW_UP_WINDOW_MINUTES = int(os.environ.get("FOLLOW_UP_WINDOW_MINUTES", "30"))

# Matches the HOLD-{id} reference we ask customers to keep in their reply
# (Layer 2 matching) -- also appears bracketed in the subject, e.g.
# "Re: Your Order [HOLD-42]", which this pattern finds equally well since
# it isn't anchored.
HOLD_TAG_RE = re.compile(r"HOLD-(\d+)", re.IGNORECASE)


def _parse_datetime(value):
    """Reads a timestamp back from either backend. SQLite returns
    exactly what was stored -- a string, since these columns hold
    .isoformat() text -- but pyodbc auto-converts Azure SQL's DATETIME2
    columns into native datetime objects instead, so datetime.
    fromisoformat() alone raises TypeError there. Handles both, and
    makes sure the result is timezone-aware either way: DATETIME2 has no
    timezone concept, so a native datetime object back from pyodbc is
    always naive, same as a plain fromisoformat() parse can be."""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hold_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    customer_email TEXT NOT NULL,
    inbound_sender TEXT NOT NULL,
    inbound_subject TEXT NOT NULL,
    inbound_body TEXT NOT NULL,
    clarifying_question_sent TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    sent_message_id TEXT,
    follow_up_body TEXT,
    follow_up_sent_at TEXT,
    status TEXT NOT NULL DEFAULT 'Awaiting Reply'
);

CREATE TABLE IF NOT EXISTS manual_linking_emails (
    uid TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    in_reply_to TEXT,
    references_header TEXT,
    fetched_at TEXT NOT NULL,
    flagged_at TEXT NOT NULL
);
"""

AZURE_SQL_SCHEMA = """
IF OBJECT_ID('hold_requests', 'U') IS NULL
CREATE TABLE hold_requests (
    id INT IDENTITY PRIMARY KEY,
    order_id NVARCHAR(50),
    customer_email NVARCHAR(255) NOT NULL,
    inbound_sender NVARCHAR(255) NOT NULL,
    inbound_subject NVARCHAR(255) NOT NULL,
    inbound_body NVARCHAR(MAX) NOT NULL,
    clarifying_question_sent NVARCHAR(MAX) NOT NULL,
    sent_at DATETIME2 NOT NULL,
    sent_message_id NVARCHAR(998),
    follow_up_body NVARCHAR(MAX),
    follow_up_sent_at DATETIME2,
    status NVARCHAR(30) NOT NULL DEFAULT 'Awaiting Reply'
);

IF OBJECT_ID('manual_linking_emails', 'U') IS NULL
CREATE TABLE manual_linking_emails (
    uid NVARCHAR(255) PRIMARY KEY,
    sender NVARCHAR(500) NOT NULL,
    subject NVARCHAR(998) NOT NULL,
    body NVARCHAR(MAX) NOT NULL,
    in_reply_to NVARCHAR(998),
    references_header NVARCHAR(MAX),
    fetched_at DATETIME2 NOT NULL,
    flagged_at DATETIME2 NOT NULL
);
"""


def _ensure_column(cursor, is_azure: bool, table: str, column: str, sqlite_type: str, azure_type: str):
    """Adds `column` to `table` if it isn't already there. Needed because
    CREATE TABLE IF NOT EXISTS (SQLite) / IF OBJECT_ID(...) IS NULL CREATE
    TABLE (Azure) are no-ops once the table already exists from an earlier
    deploy -- a column only added to the schema string above would never
    actually reach a database that was set up before this change."""
    if is_azure:
        cursor.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? AND COLUMN_NAME = ?",
            (table, column),
        )
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE {table} ADD {column} {azure_type}")
    else:
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        if column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")


def init_hold_requests_schema(get_connection, is_azure: bool):
    conn = get_connection()
    cursor = conn.cursor()
    if is_azure:
        for statement in AZURE_SQL_SCHEMA.split(";"):
            if statement.strip():
                cursor.execute(statement)
    else:
        cursor.executescript(SQLITE_SCHEMA)
    _ensure_column(cursor, is_azure, "hold_requests", "sent_message_id", "TEXT", "NVARCHAR(998)")
    conn.commit()
    conn.close()


def create_hold(
    get_connection,
    order_id: str | None,
    customer_email: str,
    inbound_sender: str,
    inbound_subject: str,
    inbound_body: str,
    clarifying_question_sent: str,
    sent_message_id: str | None = None,
) -> dict:
    """Creates a new hold record when request_clarification fires and
    an order ID was actually given. If order_id is None, the caller
    should NOT call this -- there's nothing to place on hold, only the
    clarification email itself matters (handled by outbound_email.py
    separately).

    sent_message_id is the Message-ID header generated (via
    email.utils.make_msgid()) for the outgoing clarifying question,
    stored here at creation time -- before the email is actually sent,
    since sending is gated behind human approval and may happen much
    later. This is what Layer 1 of match_reply_to_hold() checks a
    reply's In-Reply-To/References against."""
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO hold_requests "
        "(order_id, customer_email, inbound_sender, inbound_subject, inbound_body, "
        " clarifying_question_sent, sent_at, sent_message_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Awaiting Reply')",
        (order_id, customer_email, inbound_sender, inbound_subject, inbound_body,
         clarifying_question_sent, now, sent_message_id),
    )
    conn.commit()

    cursor.execute(
        "SELECT id FROM hold_requests WHERE customer_email = ? AND sent_at = ?",
        (customer_email, now),
    )
    row = cursor.fetchone()
    conn.close()

    return {"status": "Success", "hold_id": row[0] if row else None}


def get_awaiting_reply(get_connection) -> list[dict]:
    """All holds still waiting on a customer reply (not yet past the
    follow-up window), oldest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, order_id, customer_email, inbound_sender, inbound_subject, "
        "inbound_body, clarifying_question_sent, sent_at "
        "FROM hold_requests WHERE status = 'Awaiting Reply' ORDER BY sent_at ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r, has_follow_up=False) for r in rows]


def get_past_follow_up(get_connection) -> list[dict]:
    """All holds that have had a follow-up sent and still need a human
    decision -- surface these prominently in the dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, order_id, customer_email, inbound_sender, inbound_subject, "
        "inbound_body, clarifying_question_sent, sent_at, follow_up_body, follow_up_sent_at "
        "FROM hold_requests WHERE status = 'Past Follow-Up' ORDER BY follow_up_sent_at ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [_row_to_dict(r, has_follow_up=True) for r in rows]


def _row_to_dict(row, has_follow_up: bool) -> dict:
    d = {
        "id": row[0], "order_id": row[1], "customer_email": row[2],
        "inbound_sender": row[3], "inbound_subject": row[4], "inbound_body": row[5],
        "clarifying_question_sent": row[6], "sent_at": row[7],
    }
    if has_follow_up:
        d["follow_up_body"] = row[8]
        d["follow_up_sent_at"] = row[9]
    return d


def _get_open_holds(get_connection) -> list[dict]:
    """All holds that can still receive a matching reply -- both
    'Awaiting Reply' and 'Past Follow-Up' are open (the latter is just
    overdue, not closed); only 'Resolved' holds are excluded. Includes
    sent_message_id/status, which the other row-fetching functions in
    this module don't need."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, order_id, customer_email, inbound_sender, inbound_subject, "
        "inbound_body, clarifying_question_sent, sent_at, sent_message_id, status "
        "FROM hold_requests WHERE status IN ('Awaiting Reply', 'Past Follow-Up')"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "order_id": r[1], "customer_email": r[2],
            "inbound_sender": r[3], "inbound_subject": r[4], "inbound_body": r[5],
            "clarifying_question_sent": r[6], "sent_at": r[7],
            "sent_message_id": r[8], "status": r[9],
        }
        for r in rows
    ]


def get_open_holds_for_sender(get_connection, customer_email: str) -> list[dict]:
    """Open holds whose customer_email matches, case-insensitive -- the
    Layer 3 sender-fallback query, also reused to show candidates in the
    Needs Manual Linking UI when Layer 3 alone can't disambiguate."""
    email_lower = (customer_email or "").strip().lower()
    return [
        h for h in _get_open_holds(get_connection)
        if h["customer_email"].strip().lower() == email_lower
    ]


def match_reply_to_hold(
    get_connection,
    sender_email: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict:
    """Four-layer matching to decide whether an inbound email is a reply
    to an existing open Hold, most-reliable first. Returns one of:
      {"status": "matched", "hold": {...}, "layer": 1|2|3}
      {"status": "ambiguous", "candidates": [...]}   -- Layer 3 found > 1
      {"status": "no_match"}                          -- treat as new

    Never guesses: Layer 3 only returns a match when exactly one open
    Hold belongs to the sender. More than one is handed to a human via
    the "ambiguous" status (Layer 4 -- see queue_for_manual_linking)."""
    open_holds = _get_open_holds(get_connection)
    if not open_holds:
        return {"status": "no_match"}

    # Layer 1: In-Reply-To / References against the Message-ID WE sent.
    # Most reliable -- sent_message_id is a value we generated ourselves,
    # so a header match here can't be a coincidental false positive.
    candidate_msg_ids = set()
    if in_reply_to:
        candidate_msg_ids.add(in_reply_to.strip())
    if references:
        candidate_msg_ids.update(ref.strip() for ref in references.split() if ref.strip())
    if candidate_msg_ids:
        for hold in open_holds:
            if hold["sent_message_id"] and hold["sent_message_id"] in candidate_msg_ids:
                return {"status": "matched", "hold": hold, "layer": 1}

    # Layer 2: explicit HOLD-<id> tag in subject or body -- survives a
    # mail client that strips or rewrites Message-ID/In-Reply-To headers,
    # since we also ask the customer to keep this reference in their
    # reply text itself.
    tag_match = HOLD_TAG_RE.search(f"{subject or ''}\n{body or ''}")
    if tag_match:
        tagged_id = int(tag_match.group(1))
        for hold in open_holds:
            if hold["id"] == tagged_id:
                return {"status": "matched", "hold": hold, "layer": 2}

    # Layer 3: sender-email fallback -- safe only when exactly one open
    # Hold belongs to this sender.
    email_lower = (sender_email or "").strip().lower()
    sender_matches = [h for h in open_holds if h["customer_email"].strip().lower() == email_lower]
    if len(sender_matches) == 1:
        return {"status": "matched", "hold": sender_matches[0], "layer": 3}
    elif len(sender_matches) > 1:
        return {"status": "ambiguous", "candidates": sender_matches}

    return {"status": "no_match"}


def queue_for_manual_linking(get_connection, item: dict):
    """Moves an ambiguous inbound email (Layer 3 found more than one open
    Hold for the same sender) into the manual-linking queue instead of
    guessing -- a human picks the right Hold in the dashboard. `item` is
    the same dict shape as a pending_emails row (uid/sender/subject/body/
    in_reply_to/references)."""
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO manual_linking_emails "
        "(uid, sender, subject, body, in_reply_to, references_header, fetched_at, flagged_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["uid"], item["sender"], item["subject"], item["body"],
            item.get("in_reply_to"), item.get("references"),
            item.get("fetched_at", now), now,
        ),
    )
    conn.commit()
    conn.close()


def get_manual_linking_emails(get_connection) -> list[dict]:
    """Emails currently awaiting a human decision on which open Hold
    they reply to, oldest-flagged first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT uid, sender, subject, body, in_reply_to, references_header, fetched_at "
        "FROM manual_linking_emails ORDER BY flagged_at"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "uid": r[0], "sender": r[1], "subject": r[2], "body": r[3],
            "in_reply_to": r[4], "references": r[5], "fetched_at": r[6],
        }
        for r in rows
    ]


def remove_from_manual_linking(get_connection, uid: str):
    """Called once a human has resolved the ambiguity -- either by
    linking to a specific Hold or choosing to treat the email as a
    brand-new request."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM manual_linking_emails WHERE uid = ?", (uid,))
    conn.commit()
    conn.close()


def get_holds_due_for_follow_up(get_connection) -> list[dict]:
    """Holds where FOLLOW_UP_WINDOW_MINUTES have passed with status
    still 'Awaiting Reply'. This is what the scheduled job (not yet
    built) will call to decide who needs a follow-up email drafted."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, order_id, customer_email, inbound_sender, inbound_subject, "
        "inbound_body, clarifying_question_sent, sent_at "
        "FROM hold_requests WHERE status = 'Awaiting Reply'"
    )
    rows = cursor.fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    due = []
    for r in rows:
        sent_at = _parse_datetime(r[7])
        minutes_elapsed = (now - sent_at).total_seconds() / 60
        if minutes_elapsed >= FOLLOW_UP_WINDOW_MINUTES:
            due.append(_row_to_dict(r, has_follow_up=False))
    return due


def mark_follow_up_sent(get_connection, hold_id: int, follow_up_body: str):
    """Called after a follow-up email has been drafted (and, per the
    approval-gate design, only actually sent once a human approves it
    via outbound_email.approve_and_send). This just records that the
    follow-up step happened and flips status to 'Past Follow-Up' so
    the dashboard surfaces it for human attention -- it does NOT
    change anything about the underlying order."""
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "UPDATE hold_requests SET status = 'Past Follow-Up', "
        "follow_up_body = ?, follow_up_sent_at = ? WHERE id = ?",
        (follow_up_body, now, hold_id),
    )
    conn.commit()
    conn.close()


def resolve_hold(get_connection, hold_id: int):
    """Marks a hold as resolved -- called when a human has dealt with
    it (customer replied and it got reprocessed, order was cancelled,
    etc). This is a manual/explicit action, never automatic."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE hold_requests SET status = 'Resolved' WHERE id = ?",
        (hold_id,),
    )
    conn.commit()
    conn.close()