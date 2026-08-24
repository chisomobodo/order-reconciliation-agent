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


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    context_note TEXT,
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
    status NVARCHAR(30) NOT NULL DEFAULT 'Pending Approval',
    created_at DATETIME2 NOT NULL,
    sent_at DATETIME2
);
"""


def init_outbound_email_schema(get_connection, is_azure: bool):
    """Creates the outbound_emails table if it doesn't already exist.
    Call this alongside the other schema init functions at startup."""
    conn = get_connection()
    cursor = conn.cursor()
    if is_azure:
        for statement in AZURE_SQL_SCHEMA.split(";"):
            if statement.strip():
                cursor.execute(statement)
    else:
        cursor.executescript(SQLITE_SCHEMA)
    conn.commit()
    conn.close()


def queue_draft(get_connection, to_email: str, subject: str, body: str, context_note: str = "") -> dict:
    """Adds a drafted email to the approval queue. Does NOT send
    anything -- this only ever creates a 'Pending Approval' row."""
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO outbound_emails (to_email, subject, body, context_note, status, created_at) "
        "VALUES (?, ?, ?, ?, 'Pending Approval', ?)",
        (to_email, subject, body, context_note, now),
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
    conn.close()

    return {"status": "Queued", "draft_id": row[0] if row else None}


def get_pending_drafts(get_connection) -> list[dict]:
    """Returns all emails currently awaiting approval, oldest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, to_email, subject, body, context_note, created_at "
        "FROM outbound_emails WHERE status = 'Pending Approval' ORDER BY created_at ASC"
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r[0], "to_email": r[1], "subject": r[2],
            "body": r[3], "context_note": r[4], "created_at": r[5],
        }
        for r in rows
    ]


def get_sent_log(get_connection) -> list[dict]:
    """Returns all previously sent emails, most recent first -- an
    audit log, same spirit as the processed_emails table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, to_email, subject, body, sent_at "
        "FROM outbound_emails WHERE status = 'Sent' ORDER BY sent_at DESC"
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {"id": r[0], "to_email": r[1], "subject": r[2], "body": r[3], "sent_at": r[4]}
        for r in rows
    ]


def _send_via_smtp(to_email: str, subject: str, body: str):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set to send email.")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_DISPLAY_NAME, GMAIL_ADDRESS))
    msg["To"] = to_email
    msg["Reply-To"] = "noreply@reconciliation-agent.local"

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_email], msg.as_string())


def approve_and_send(get_connection, draft_id: int) -> dict:
    """The ONLY function that actually sends an email. Called
    exclusively from an explicit human click in the UI -- never from
    inside the agent's own reasoning loop, same principle as
    commit_order_modification never being callable by Claude directly."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT to_email, subject, body, status FROM outbound_emails WHERE id = ?",
        (draft_id,),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        return {"status": "Error", "message": "Draft not found."}

    to_email, subject, body, status = row
    if status != "Pending Approval":
        conn.close()
        return {"status": "Error", "message": f"This draft is already '{status}', not pending."}

    try:
        _send_via_smtp(to_email, subject, body)
    except Exception as e:
        conn.close()
        return {"status": "Error", "message": f"Failed to send: {e}"}

    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "UPDATE outbound_emails SET status = 'Sent', sent_at = ? WHERE id = ?",
        (now, draft_id),
    )
    conn.commit()
    conn.close()

    return {"status": "Sent", "message": f"Email sent to {to_email}."}


def reject_draft(get_connection, draft_id: int) -> dict:
    """Discards a drafted email without sending it."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE outbound_emails SET status = 'Rejected' WHERE id = ?",
        (draft_id,),
    )
    conn.commit()
    conn.close()
    return {"status": "Rejected"}