"""
Approval-gated outbound email sending for the Reconciliation Agent.

Every reply the agent drafts -- clarification questions, order-change
confirmations, anything -- goes into a queue as a DRAFT first. Nothing
is ever sent automatically. A human reviews the draft in the dashboard
and explicitly clicks "Approve & Send" before anything reaches a real
customer's inbox.

This mirrors the exact same design principle already used for database
writes (check vs. commit, gated behind human approval) -- extended to
outbound email, since an email is at least as hard to undo as a
database row, arguably harder.

Reuses GMAIL_ADDRESS / GMAIL_APP_PASSWORD, same as auth.py's code
emails.
"""
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
SENDER_DISPLAY_NAME = "Reconciliation Agent"


def _default_order_intake_email():
    """Falls back to the same plus-addressed intake convention
    email_ingestion.py's IMAP_LABEL setup already documents (a Gmail
    filter routes GMAIL_ADDRESS+orders@gmail.com into the OrderRequests
    label) -- so Reply-To works out of the box without a second env var
    to configure, unless ORDER_INTAKE_EMAIL is set to override it."""
    if not GMAIL_ADDRESS or "@" not in GMAIL_ADDRESS:
        return None
    local_part, domain = GMAIL_ADDRESS.split("@", 1)
    return f"{local_part}+orders@{domain}"


# Order-related emails (clarification questions, confirmations) need
# replies to flow back into the OrderRequests label for IMAP ingestion to
# pick up -- the Hold-state feature depends on it. This is deliberately
# NOT the same as auth.py's login-code Reply-To (noreply@...), which
# stays genuinely no-reply since a reply is never expected there.
ORDER_INTAKE_EMAIL = os.environ.get("ORDER_INTAKE_EMAIL") or _default_order_intake_email()


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    context_note TEXT,
    message_id TEXT,
    status TEXT NOT NULL DEFAULT 'Pending Approval',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""

AZURE_SQL_SCHEMA = """
IF OBJECT_ID('outbound_emails', 'U') IS NULL
CREATE TABLE outbound_emails (
    id INT IDENTITY PRIMARY KEY,
    to_email NVARCHAR(255) NOT NULL,
    subject NVARCHAR(255) NOT NULL,
    body NVARCHAR(MAX) NOT NULL,
    context_note NVARCHAR(500),
    message_id NVARCHAR(998),
    status NVARCHAR(30) NOT NULL DEFAULT 'Pending Approval',
    created_at DATETIME2 NOT NULL,
    sent_at DATETIME2
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


def init_outbound_email_schema(conn, is_azure: bool):
    """Creates the outbound_emails table if it doesn't already exist.
    Call this alongside the other schema init functions at startup.
    Takes an already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()
    if is_azure:
        for statement in AZURE_SQL_SCHEMA.split(";"):
            if statement.strip():
                cursor.execute(statement)
    else:
        cursor.executescript(SQLITE_SCHEMA)
    _ensure_column(cursor, is_azure, "outbound_emails", "message_id", "TEXT", "NVARCHAR(998)")
    conn.commit()


def queue_draft(
    conn,
    to_email: str,
    subject: str,
    body: str,
    context_note: str = "",
    message_id: str | None = None,
) -> dict:
    """Adds a drafted email to the approval queue. Does NOT send
    anything -- this only ever creates a 'Pending Approval' row. Takes
    an already-open connection -- caller owns its lifecycle.

    message_id, when given, is the Message-ID header hold_requests.
    create_hold() already stored as sent_message_id for this same Hold --
    it must be set on the ACTUAL outgoing email (in approve_and_send,
    whenever a human eventually approves it) so a customer's mail client
    reply carries a matching In-Reply-To header (Layer 1 of
    hold_requests.match_reply_to_hold())."""
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO outbound_emails (to_email, subject, body, context_note, message_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'Pending Approval', ?)",
        (to_email, subject, body, context_note, message_id, now),
    )
    conn.commit()

    # Grab the id of the row we just inserted (works for both sqlite3
    # and pyodbc via a follow-up SELECT, which is more portable than
    # relying on driver-specific lastrowid behaviour).
    cursor.execute(
        "SELECT id FROM outbound_emails WHERE to_email = ? AND created_at = ?",
        (to_email, now),
    )
    row = cursor.fetchone()

    return {"status": "Queued", "draft_id": row[0] if row else None}


def get_pending_drafts(conn) -> list[dict]:
    """Returns all emails currently awaiting approval, oldest first."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, to_email, subject, body, context_note, message_id, created_at "
        "FROM outbound_emails WHERE status = 'Pending Approval' ORDER BY created_at ASC"
    )
    rows = cursor.fetchall()

    return [
        {
            "id": r[0], "to_email": r[1], "subject": r[2],
            "body": r[3], "context_note": r[4], "message_id": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def get_sent_log(conn) -> list[dict]:
    """Returns all previously sent emails, most recent first -- an
    audit log, same spirit as the processed_emails table."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, to_email, subject, body, sent_at "
        "FROM outbound_emails WHERE status = 'Sent' ORDER BY sent_at DESC"
    )
    rows = cursor.fetchall()

    return [
        {"id": r[0], "to_email": r[1], "subject": r[2], "body": r[3], "sent_at": r[4]}
        for r in rows
    ]


def _send_via_smtp(to_email: str, subject: str, body: str, message_id: str | None = None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set to send email.")
    if not ORDER_INTAKE_EMAIL:
        raise RuntimeError(
            "Could not determine an order-intake Reply-To address. Set ORDER_INTAKE_EMAIL "
            "explicitly, or GMAIL_ADDRESS so it can be derived as <local>+orders@<domain>."
        )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_DISPLAY_NAME, GMAIL_ADDRESS))
    msg["To"] = to_email
    # A customer reply needs to land back in the OrderRequests label for
    # IMAP ingestion to pick it up (this is what Hold state is waiting
    # on) -- NOT a no-reply address like auth.py's login codes use.
    msg["Reply-To"] = ORDER_INTAKE_EMAIL
    if message_id:
        # Set only for Hold-linked clarifying questions -- app.py generates
        # this via email.utils.make_msgid() at Hold-creation time (before
        # this send, which may happen much later once a human approves the
        # draft) and stores it as hold_requests.sent_message_id, so a
        # customer's reply carries a matching In-Reply-To header for
        # Layer 1 of match_reply_to_hold(). Other emails are sent without
        # an explicit Message-ID, same as before this feature.
        msg["Message-ID"] = message_id

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_email], msg.as_string())


def approve_and_send(conn, draft_id: int) -> dict:
    """The ONLY function that actually sends an email. Called
    exclusively from an explicit human click in the UI -- never from
    inside the agent's own reasoning loop, same principle as
    commit_order_modification never being callable by Claude directly.
    Takes an already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()

    cursor.execute(
        "SELECT to_email, subject, body, status, message_id FROM outbound_emails WHERE id = ?",
        (draft_id,),
    )
    row = cursor.fetchone()

    if not row:
        return {"status": "Error", "message": "Draft not found."}

    to_email, subject, body, status, message_id = row
    if status != "Pending Approval":
        return {"status": "Error", "message": f"This draft is already '{status}', not pending."}

    try:
        _send_via_smtp(to_email, subject, body, message_id=message_id)
    except Exception as e:
        return {"status": "Error", "message": f"Failed to send: {e}"}

    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "UPDATE outbound_emails SET status = 'Sent', sent_at = ? WHERE id = ?",
        (now, draft_id),
    )
    conn.commit()

    return {"status": "Sent", "message": f"Email sent to {to_email}."}


def reject_draft(conn, draft_id: int) -> dict:
    """Discards a drafted email without sending it. Takes an
    already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE outbound_emails SET status = 'Rejected' WHERE id = ?",
        (draft_id,),
    )
    conn.commit()
    return {"status": "Rejected"}