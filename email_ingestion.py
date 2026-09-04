"""
Real IMAP email ingestion for Arbiter, an AI order-reconciliation agent.

Connects to Gmail via IMAP, fetches unread emails under a specific
label (NOT the whole inbox -- scoped deliberately so the agent never
sees or processes personal email), parses sender/subject/body, and
marks them processed afterward so they aren't re-ingested.

Reuses the same GMAIL_ADDRESS / GMAIL_APP_PASSWORD environment
variables already used for sending login codes -- Gmail App Passwords
work for both SMTP (sending) and IMAP (reading).

Setup required (one-time, in Gmail's own settings, not code):
  1. Create a Gmail label, e.g. "OrderRequests".
  2. Route or manually apply that label to whichever emails should be
     treated as customer order-change requests.
  3. Set IMAP_LABEL below (or via env var) to match.

This module only fetches and marks messages -- it does not decide
what to do with them. The caller (app.py) is expected to run each
parsed email through the exact same run_agent() pipeline as manually
typed/pasted emails, so the approval-gate design is untouched.
"""
import email
import email.message  # for the email.message.Message annotation below --
                       # not otherwise guaranteed to be populated as an
                       # attribute of `email` by a bare `import email`;
                       # this module currently only works when something
                       # imported earlier (e.g. auth.py's email.mime
                       # imports) happens to pull it in first
import imaplib
import os
from datetime import datetime, timezone
from email.header import decode_header

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
IMAP_LABEL = os.environ.get("IMAP_LABEL", "OrderRequests")

IMAP_HOST = "imap.gmail.com"


# ---------------------------------------------------------------------
# Schema (call once at app startup, alongside auth.init_auth_schema --
# same conn/is_azure pattern, same database)
# ---------------------------------------------------------------------

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_emails (
    uid TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    in_reply_to TEXT,
    references_header TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_emails (
    uid TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    tool_chain_summary TEXT,
    final_status TEXT,
    reply_text TEXT,
    processed_at TEXT NOT NULL
);
"""

AZURE_SQL_SCHEMA = """
IF OBJECT_ID('pending_emails', 'U') IS NULL
CREATE TABLE pending_emails (
    uid NVARCHAR(255) PRIMARY KEY,
    sender NVARCHAR(500) NOT NULL,
    subject NVARCHAR(998) NOT NULL,
    body NVARCHAR(MAX) NOT NULL,
    in_reply_to NVARCHAR(998),
    references_header NVARCHAR(MAX),
    fetched_at DATETIME2 NOT NULL
);

IF OBJECT_ID('processed_emails', 'U') IS NULL
CREATE TABLE processed_emails (
    uid NVARCHAR(255) PRIMARY KEY,
    sender NVARCHAR(500) NOT NULL,
    subject NVARCHAR(998) NOT NULL,
    body NVARCHAR(MAX) NOT NULL,
    tool_chain_summary NVARCHAR(MAX),
    final_status NVARCHAR(100),
    reply_text NVARCHAR(MAX),
    processed_at DATETIME2 NOT NULL
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


def init_email_tracking_schema(conn, is_azure: bool):
    """Creates the pending_emails/processed_emails tables if they don't
    exist. Call this once alongside auth.init_auth_schema. Takes an
    already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()
    schema = AZURE_SQL_SCHEMA if is_azure else SQLITE_SCHEMA
    if is_azure:
        # Azure SQL doesn't support multiple statements separated by ';'
        # in one execute() the way sqlite3 does -- split and run each.
        for statement in schema.split(";"):
            if statement.strip():
                cursor.execute(statement)
    else:
        cursor.executescript(schema)
    _ensure_column(cursor, is_azure, "pending_emails", "in_reply_to", "TEXT", "NVARCHAR(998)")
    _ensure_column(cursor, is_azure, "pending_emails", "references_header", "TEXT", "NVARCHAR(MAX)")
    conn.commit()


# ---------------------------------------------------------------------
# Pending / processed email tracking
# ---------------------------------------------------------------------

def sync_fetched_emails(conn, fetched: list[dict]) -> int:
    """Inserts each newly-fetched email into pending_emails, skipping
    any uid already present in EITHER pending_emails or processed_emails.
    That dedup is what makes "Check Inbox" always safe to click
    repeatedly -- it can never duplicate an email still awaiting
    processing, and can never resurrect one already processed. Returns
    the number of genuinely new emails inserted. Takes an already-open
    connection -- caller owns its lifecycle."""
    if not fetched:
        return 0

    cursor = conn.cursor()
    inserted = 0
    now = datetime.now(timezone.utc).isoformat()

    for item in fetched:
        cursor.execute("SELECT 1 FROM pending_emails WHERE uid = ?", (item["uid"],))
        if cursor.fetchone():
            continue
        cursor.execute("SELECT 1 FROM processed_emails WHERE uid = ?", (item["uid"],))
        if cursor.fetchone():
            continue
        cursor.execute(
            "INSERT INTO pending_emails (uid, sender, subject, body, in_reply_to, references_header, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item["uid"], item["sender"], item["subject"], item["body"],
                item.get("in_reply_to"), item.get("references"), now,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


def get_pending_emails(conn) -> list[dict]:
    """Returns the current pending_emails contents, oldest fetched
    first -- queried fresh from the database every call, not cached, so
    it's correct regardless of which browser session originally fetched
    an email and survives a page refresh."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT uid, sender, subject, body, in_reply_to, references_header, fetched_at "
        "FROM pending_emails ORDER BY fetched_at"
    )
    rows = cursor.fetchall()
    return [
        {
            "uid": r[0], "sender": r[1], "subject": r[2], "body": r[3],
            "in_reply_to": r[4], "references": r[5], "fetched_at": r[6],
        }
        for r in rows
    ]


def remove_pending_email(conn, uid: str):
    """Removes a pending email WITHOUT moving it to processed_emails --
    used when hold_requests.match_reply_to_hold() finds it ambiguous
    (more than one open Hold from the same sender): the email moves into
    the manual-linking queue instead (hold_requests.
    queue_for_manual_linking), to be processed later once a human picks
    the right Hold."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_emails WHERE uid = ?", (uid,))
    conn.commit()


def get_processed_emails(conn) -> list[dict]:
    """Returns the processed_emails log, most recently processed first."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT uid, sender, subject, final_status, processed_at FROM processed_emails "
        "ORDER BY processed_at DESC"
    )
    rows = cursor.fetchall()
    return [
        {"uid": r[0], "sender": r[1], "subject": r[2], "final_status": r[3], "processed_at": r[4]}
        for r in rows
    ]


def record_processed_email(
    conn,
    uid: str,
    sender: str,
    subject: str,
    body: str,
    tool_chain_summary: str,
    final_status: str,
    reply_text: str,
):
    """Moves an email from pending_emails to processed_emails: inserts
    the outcome into processed_emails (with the current timestamp), then
    deletes the pending_emails row. Does NOT touch Gmail -- call
    mark_processed(uid) separately; keeping the DB move and the IMAP
    flag as two explicit steps mirrors how commit_order_modification and
    email_ingestion.mark_processed are already kept as separate,
    deliberate actions elsewhere in this project. Takes an already-open
    connection -- caller owns its lifecycle."""
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO processed_emails "
        "(uid, sender, subject, body, tool_chain_summary, final_status, reply_text, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, sender, subject, body, tool_chain_summary, final_status, reply_text, now),
    )
    cursor.execute("DELETE FROM pending_emails WHERE uid = ?", (uid,))
    conn.commit()


def _decode_mime_words(value: str) -> str:
    """Email subjects/names can be MIME-encoded (e.g. '=?UTF-8?B?...?=');
    decode to a plain readable string."""
    decoded_parts = decode_header(value)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def _extract_body(msg: email.message.Message) -> str:
    """Handles both plain-text and multipart emails, preferring the
    plain-text part over HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace").strip()
        # Fall back to HTML if no plain-text part exists.
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace").strip()
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace").strip()
        return ""


def fetch_new_order_emails() -> list[dict]:
    """Connects to Gmail via IMAP, fetches UNSEEN emails under
    IMAP_LABEL, and returns them as a list of dicts:
    [{"uid": ..., "sender": ..., "subject": ..., "body": ...}, ...]

    Does NOT mark anything as read/processed -- call mark_processed()
    per-email after it's actually been run through the agent, so a
    crash mid-batch doesn't silently lose/skip an email.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set to use email ingestion."
        )

    results = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

        # Gmail exposes labels as IMAP folders. A label with a space
        # needs quoting. readonly=True: this function never writes
        # anything (mark_processed() does that, on its own separate
        # connection) -- opening read-only is a second, independent
        # guarantee against any flag getting mutated as a side effect of
        # a fetch here, on top of using BODY.PEEK[] below.
        status, _ = conn.select(f'"{IMAP_LABEL}"', readonly=True)
        if status != "OK":
            raise RuntimeError(
                f"Could not open Gmail label '{IMAP_LABEL}'. "
                f"Create this label in Gmail first, or set IMAP_LABEL to an existing one."
            )

        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []

        uids = data[0].split()

        for uid in uids:
            # BODY.PEEK[], not RFC822 -- RFC822 (and plain BODY[]) fetch
            # the full message but implicitly set \Seen as a side effect
            # of the fetch itself (RFC 3501 SS6.4.5), even though this
            # function never calls STORE. That silently marked every
            # fetched email as read the moment "Check Inbox" ran, whether
            # or not it was ever actually processed -- so an unprocessed
            # email would vanish on the next check instead of still
            # showing up. PEEK reads the identical content without that
            # side effect; only mark_processed()'s explicit +FLAGS \Seen
            # call below should ever mark an email read.
            status, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            sender = _decode_mime_words(msg.get("From", ""))
            subject = _decode_mime_words(msg.get("Subject", "(no subject)"))
            body = _extract_body(msg)
            # In-Reply-To/References are plain ASCII Message-ID tokens
            # (RFC 5322), never MIME-encoded like subject/display names,
            # so no _decode_mime_words() needed here -- used for Layer 1
            # of hold_requests.match_reply_to_hold().
            in_reply_to = msg.get("In-Reply-To")
            references = msg.get("References")

            results.append({
                "uid": uid.decode() if isinstance(uid, bytes) else uid,
                "sender": sender,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to.strip() if in_reply_to else None,
                "references": references.strip() if references else None,
            })

        return results
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


def mark_processed(uid: str):
    """Marks a single email as read (Seen) so it isn't fetched again
    on the next check. Call this AFTER the email has actually been
    run through the agent -- not before -- so a mid-process crash
    doesn't cause the email to be silently skipped forever."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        conn.select(f'"{IMAP_LABEL}"', readonly=False)
        conn.store(uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()