"""
Authentication module for the Reconciliation Agent app.

Flow:
  1. Sign up: email + password + name. Password is hashed with bcrypt --
     the plaintext password is never stored, ever.
  2. Log in, step 1: email + password checked against the stored hash.
  3. Log in, step 2: a 6-digit code is generated, emailed via Gmail SMTP,
     and must be entered correctly (and within 10 minutes) to complete
     login. This is a genuine second factor, not just a password check.
  4. On success, a random opaque session token is created and stored as
     a browser cookie, so reloading the page keeps the user logged in.
  5. Every page load validates the session: it must exist, not be
     manually logged out, and have been active within the last 20
     minutes (sliding window -- each interaction refreshes it, so it's
     "20 minutes of inactivity", not a fixed 20-minute timer).

Works against either backend (SQLite or Azure SQL) via the same
get_connection() pattern used elsewhere in the project -- pass in
whichever connection function matches the active backend.

Environment variables needed for sending codes:
  GMAIL_ADDRESS    -- the Gmail address codes are sent FROM
  GMAIL_APP_PASSWORD -- a Gmail App Password (NOT your normal password --
                        generate one at myaccount.google.com/apppasswords,
                        requires 2FA enabled on the Gmail account)
"""
import os
import random
import secrets
import smtplib
import string
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

import bcrypt

CODE_LENGTH = 6
CODE_VALID_MINUTES = 10
SESSION_INACTIVITY_MINUTES = 20


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


# ---------------------------------------------------------------------
# Schema (call once at app startup / in setup_db.py & setup_db_azure.py)
# ---------------------------------------------------------------------

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    last_active_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""

AZURE_SQL_SCHEMA = """
IF OBJECT_ID('sessions', 'U') IS NULL
CREATE TABLE sessions (
    session_token NVARCHAR(64) PRIMARY KEY,
    user_id INT NOT NULL,
    last_active_at DATETIME2 NOT NULL
);

IF OBJECT_ID('login_codes', 'U') IS NULL
CREATE TABLE login_codes (
    id INT IDENTITY PRIMARY KEY,
    user_id INT NOT NULL,
    code NVARCHAR(10) NOT NULL,
    expires_at DATETIME2 NOT NULL,
    used BIT NOT NULL DEFAULT 0
);

IF OBJECT_ID('users', 'U') IS NULL
CREATE TABLE users (
    id INT IDENTITY PRIMARY KEY,
    email NVARCHAR(255) UNIQUE NOT NULL,
    name NVARCHAR(255) NOT NULL,
    password_hash NVARCHAR(255) NOT NULL,
    created_at DATETIME2 NOT NULL
);
"""


def init_auth_schema(conn, is_azure: bool):
    """Creates the users/login_codes/sessions tables if they don't exist.
    Call this once alongside the existing ERP schema setup. Takes an
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
    conn.commit()


# ---------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------

def hash_password(plaintext_password: str) -> str:
    """bcrypt handles salting internally -- never store plaintext."""
    return bcrypt.hashpw(plaintext_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext_password: str, stored_hash: str) -> bool:
    return bcrypt.checkpw(plaintext_password.encode("utf-8"), stored_hash.encode("utf-8"))


# ---------------------------------------------------------------------
# Sign up
# ---------------------------------------------------------------------

def sign_up(conn, email: str, name: str, password: str) -> dict:
    """Creates a new user. Returns {'status': 'Success'} or
    {'status': 'Error', 'message': ...} (e.g. email already registered).
    Takes an already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    if cursor.fetchone():
        return {"status": "Error", "message": "An account with this email already exists."}

    password_hash = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, name, password_hash, now),
    )
    conn.commit()
    return {"status": "Success", "message": "Account created. You can now log in."}


# ---------------------------------------------------------------------
# Login step 1: password check + code generation/send
# ---------------------------------------------------------------------

def _generate_code() -> str:
    return "".join(random.choices(string.digits, k=CODE_LENGTH))


def _send_code_email(to_email: str, code: str):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

    body = (
        f"Your login verification code is: {code}\n\n"
        f"This code expires in {CODE_VALID_MINUTES} minutes. "
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"This is an automated message, please do not reply to this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Your Reconciliation Agent login code"
    msg["From"] = formataddr(("Reconciliation Agent", gmail_address))
    # Keeps replies from routing to the personal Gmail inbox sending
    # these -- doesn't need to be a real, resolvable domain, it's just
    # signaling "don't reply here" to the recipient's email client.
    msg["Reply-To"] = "noreply@reconciliation-agent.local"
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_email], msg.as_string())


def request_login_code(conn, email: str, password: str) -> dict:
    """Step 1 of login: verify password, then email a fresh code.
    Returns {'status': 'Success', 'user_id': ...} or an Error status.
    Takes an already-open connection -- caller owns its lifecycle."""
    cursor = conn.cursor()

    cursor.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()

    if not row:
        return {"status": "Error", "message": "No account found with this email."}

    user_id, stored_hash = row
    if not verify_password(password, stored_hash):
        return {"status": "Error", "message": "Incorrect password."}

    code = _generate_code()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=CODE_VALID_MINUTES)).isoformat()

    cursor.execute(
        "INSERT INTO login_codes (user_id, code, expires_at, used) VALUES (?, ?, ?, 0)",
        (user_id, code, expires_at),
    )
    conn.commit()

    try:
        _send_code_email(email, code)
    except Exception as e:
        return {"status": "Error", "message": f"Could not send verification email: {e}"}

    return {"status": "Success", "user_id": user_id, "message": "A verification code has been emailed to you."}


# ---------------------------------------------------------------------
# Login step 2: verify the code, create a session
# ---------------------------------------------------------------------

def verify_login_code(conn, user_id: int, submitted_code: str, is_azure: bool) -> dict:
    """Step 2 of login: check the code, and if valid, create a session.
    Returns {'status': 'Success', 'session_token': ...} or an Error.
    Takes an already-open connection -- caller owns its lifecycle.

    is_azure picks the right "most recent row" syntax -- LIMIT 1 is
    SQLite-only; Azure SQL (T-SQL) has no LIMIT clause at all and needs
    TOP 1 placed right after SELECT instead of at the end of the query."""
    cursor = conn.cursor()

    if is_azure:
        cursor.execute(
            "SELECT TOP 1 id, code, expires_at, used FROM login_codes "
            "WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        )
    else:
        cursor.execute(
            "SELECT id, code, expires_at, used FROM login_codes "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
    row = cursor.fetchone()

    if not row:
        return {"status": "Error", "message": "No code was requested. Please log in again."}

    code_id, stored_code, expires_at, used = row

    if used:
        return {"status": "Error", "message": "This code has already been used. Please request a new one."}

    if _parse_datetime(expires_at) < datetime.now(timezone.utc):
        return {"status": "Error", "message": "This code has expired. Please request a new one."}

    if submitted_code.strip() != stored_code:
        return {"status": "Error", "message": "Incorrect code."}

    # Mark the code used so it can never be replayed.
    cursor.execute("UPDATE login_codes SET used = 1 WHERE id = ?", (code_id,))

    session_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO sessions (session_token, user_id, last_active_at) VALUES (?, ?, ?)",
        (session_token, user_id, now),
    )
    conn.commit()

    return {"status": "Success", "session_token": session_token}


# ---------------------------------------------------------------------
# Session validation (called on every page load)
# ---------------------------------------------------------------------

def validate_session(conn, session_token: str) -> dict:
    """Checks whether a session token is still valid (exists, and was
    active within the last SESSION_INACTIVITY_MINUTES). If valid,
    refreshes last_active_at (sliding expiry) and returns the user info.
    If invalid/expired, returns {'status': 'Invalid'}. Takes an
    already-open connection -- caller owns its lifecycle."""
    if not session_token:
        return {"status": "Invalid"}

    cursor = conn.cursor()

    cursor.execute(
        "SELECT s.user_id, s.last_active_at, u.email, u.name "
        "FROM sessions s JOIN users u ON s.user_id = u.id "
        "WHERE s.session_token = ?",
        (session_token,),
    )
    row = cursor.fetchone()

    if not row:
        return {"status": "Invalid"}

    user_id, last_active_at, email, name = row
    last_active = _parse_datetime(last_active_at)

    if datetime.now(timezone.utc) - last_active > timedelta(minutes=SESSION_INACTIVITY_MINUTES):
        # Expired from inactivity -- clean it up.
        cursor.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        conn.commit()
        return {"status": "Invalid", "message": "Session expired due to inactivity."}

    # Still valid -- refresh the sliding window.
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "UPDATE sessions SET last_active_at = ? WHERE session_token = ?",
        (now, session_token),
    )
    conn.commit()

    return {"status": "Valid", "user_id": user_id, "email": email, "name": name}


def log_out(conn, session_token: str):
    """Immediately invalidates a session, regardless of the inactivity
    window. Takes an already-open connection -- caller owns its
    lifecycle."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
    conn.commit()