"""
Role-based access control for the Reconciliation Agent (Arbiter).

Adds four things on top of the existing auth system:
  1. Signup allowlist -- only emails explicitly permitted can create an
     account at all. Managed two ways: a database table (allowed_
     signup_emails) an admin edits live from the Admin page's Signup
     Access section, plus the ALLOWED_SIGNUP_EMAILS env var as a
     permanent fallback that can never be locked out by a DB mistake.
     See is_email_allowed_to_signup() for the exact precedence. Adding
     an email optionally sends it a notification email with the app's
     sign-in link (send_signup_access_email(), reusing the same Gmail
     credentials auth.py's login codes already use).
  2. Roles + editable permission mapping -- 'admin', 'approver',
     'reviewer'. Permissions are stored in a table, not hardcoded, so
     an admin can actually toggle what a role can do from the UI.
  3. Audit log -- every sensitive action (approving/rejecting an order
     change or an outbound email, changing a user's role, editing
     permissions, editing the signup allowlist) is recorded with WHO
     did it, not just what happened.
  4. Admin bootstrap via ADMIN_EMAILS -- see ensure_admin_role().

Design principle: this is additive to auth.py, not a rewrite of it.
auth.py's sign_up()/login flow stays exactly as it is; this module's
functions are called ALONGSIDE it from app.py (allowlist check before
sign_up, role/permission checks before showing approval buttons, audit
logging after an action succeeds).
"""
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

DEFAULT_ROLE = "reviewer"
VALID_ROLES = ("admin", "approver", "reviewer")

# Every permission this app actually gates. Keep this list in sync
# with the real approval-gated actions in the app -- a permission
# that isn't checked anywhere is just decoration in the admin UI.
ALL_PERMISSIONS = (
    "can_process_emails",
    "can_approve_order_changes",
    "can_approve_outbound_emails",
    "can_manage_users",
)

# Sensible starting defaults, seeded once on first schema init. All of
# these are then editable via the admin UI -- not fixed in code after
# this point.
DEFAULT_ROLE_PERMISSIONS = {
    "admin":    {"can_process_emails": True,  "can_approve_order_changes": True,  "can_approve_outbound_emails": True,  "can_manage_users": True},
    "approver": {"can_process_emails": True,  "can_approve_order_changes": True,  "can_approve_outbound_emails": True,  "can_manage_users": False},
    "reviewer": {"can_process_emails": True,  "can_approve_order_changes": False, "can_approve_outbound_emails": False, "can_manage_users": False},
}


# ---------------------------------------------------------------------
# Schema (idempotent -- safe to call on every startup, same pattern as
# every other init_*_schema function in this project)
# ---------------------------------------------------------------------

def init_rbac_schema(conn, is_azure: bool):
    cursor = conn.cursor()

    if is_azure:
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('users') AND name = 'role'
            )
            ALTER TABLE users ADD role NVARCHAR(20) NOT NULL DEFAULT 'reviewer'
        """)
        cursor.execute("""
            IF OBJECT_ID('role_permissions', 'U') IS NULL
            CREATE TABLE role_permissions (
                role NVARCHAR(20) NOT NULL,
                permission NVARCHAR(50) NOT NULL,
                enabled BIT NOT NULL DEFAULT 0,
                PRIMARY KEY (role, permission)
            )
        """)
        cursor.execute("""
            IF OBJECT_ID('audit_log', 'U') IS NULL
            CREATE TABLE audit_log (
                id INT IDENTITY PRIMARY KEY,
                actor_user_id INT,
                actor_email NVARCHAR(255) NOT NULL,
                action NVARCHAR(100) NOT NULL,
                target_type NVARCHAR(50),
                target_id NVARCHAR(100),
                details NVARCHAR(1000),
                created_at DATETIME2 NOT NULL
            )
        """)
        cursor.execute("""
            IF OBJECT_ID('allowed_signup_emails', 'U') IS NULL
            CREATE TABLE allowed_signup_emails (
                email NVARCHAR(255) PRIMARY KEY,
                added_by_email NVARCHAR(255) NOT NULL,
                added_at DATETIME2 NOT NULL
            )
        """)
    else:
        # SQLite: ALTER TABLE ADD COLUMN fails loudly if the column
        # already exists -- check first rather than relying on a
        # try/except, so a genuine unrelated error isn't swallowed.
        cursor.execute("PRAGMA table_info(users)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if "role" not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'reviewer'")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS role_permissions (
                role TEXT NOT NULL,
                permission TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (role, permission)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER,
                actor_email TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS allowed_signup_emails (
                email TEXT PRIMARY KEY,
                added_by_email TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
        """)

    conn.commit()

    # Seed default permissions once -- only inserts rows that don't
    # already exist, so re-running this never clobbers an admin's
    # later edits via the UI.
    cursor.execute("SELECT COUNT(*) FROM role_permissions")
    if cursor.fetchone()[0] == 0:
        for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
            for permission, enabled in perms.items():
                cursor.execute(
                    "INSERT INTO role_permissions (role, permission, enabled) VALUES (?, ?, ?)",
                    (role, permission, 1 if enabled else 0),
                )
        conn.commit()


# ---------------------------------------------------------------------
# Signup allowlist
# ---------------------------------------------------------------------

def is_email_allowed_to_signup(conn, email: str) -> bool:
    """Checks, in order:
      1. The database allowlist (allowed_signup_emails) -- this is what
         an admin manages day-to-day via the Admin page's Signup Access
         section, no env var edit or redeploy needed.
      2. ALLOWED_SIGNUP_EMAILS env var -- a permanent, always-on
         fallback so an admin can never lock themselves out even if
         the database table is empty or something's wrong with it.
      3. If NEITHER the DB table nor the env var has any entries at
         all, signup is open to anyone -- an explicit choice so this
         doesn't silently lock everyone out before the first admin has
         had a chance to add anyone to either list.
    """
    email_normalized = email.strip().lower()

    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM allowed_signup_emails WHERE email = ?", (email_normalized,))
    if cursor.fetchone():
        return True

    admin_emails_raw = os.environ.get("ALLOWED_SIGNUP_EMAILS", "").strip()
    if admin_emails_raw:
        allowed = {e.strip().lower() for e in admin_emails_raw.split(",") if e.strip()}
        if email_normalized in allowed:
            return True
        # Env var IS set but this email isn't on it -- and the DB
        # already said no above -- so this is a genuine rejection, not
        # a "nothing configured yet" case. Don't fall through to "open
        # to anyone" below.
        return False

    cursor.execute("SELECT COUNT(*) FROM allowed_signup_emails")
    db_has_any_entries = cursor.fetchone()[0] > 0
    if db_has_any_entries:
        # DB allowlist is actively in use (has at least one entry) and
        # this email isn't in it -- genuine rejection.
        return False

    # Neither the DB table nor the env var has ever been configured --
    # nothing has restricted signup yet, so allow it.
    return True


def get_allowed_signup_emails(conn) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute("SELECT email, added_by_email, added_at FROM allowed_signup_emails ORDER BY added_at DESC")
    return [{"email": r[0], "added_by_email": r[1], "added_at": r[2]} for r in cursor.fetchall()]


def send_signup_access_email(email: str) -> dict:
    """Sends a fixed, admin-triggered notification -- not agent-drafted
    content, so this sends immediately rather than going through the
    outbound_email.py approval queue, the same reasoning auth.py uses
    for login codes: the admin clicking 'Add email' IS the approval.
    Reuses the same GMAIL_ADDRESS/GMAIL_APP_PASSWORD already configured
    for auth codes and outbound email -- no separate credential needed.

    Returns its own {"status": ...} dict rather than raising, unlike
    auth.py's _send_code_email() (which raises and lets its caller
    catch) -- matches this module's own dominant convention (every
    other admin-facing action here already returns a status dict), and
    lets add_allowed_signup_email() below report the send outcome in
    its own message without needing a try/except around this call."""
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    app_url = os.environ.get("APP_URL", "").strip()

    if not gmail_address or not gmail_app_password:
        return {"status": "Error", "message": "GMAIL_ADDRESS/GMAIL_APP_PASSWORD not configured -- email not sent."}

    link_line = f"\n{app_url}\n" if app_url else "\n(Ask your administrator for the sign-in link.)\n"
    body = (
        f"Hi,\n\n"
        f"You've been granted access to create an account on Arbiter.\n"
        f"{link_line}\n"
        f"Use this email address ({email}) when signing up.\n\n"
        f"If you weren't expecting this, you can safely ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "You've been granted access to Arbiter"
    msg["From"] = formataddr(("Arbiter", gmail_address))
    msg["To"] = email
    msg["Reply-To"] = "noreply@reconciliation-agent.local"

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, [email], msg.as_string())
        return {"status": "Success"}
    except Exception as e:
        return {"status": "Error", "message": f"Could not send notification email: {e}"}


def add_allowed_signup_email(conn, email: str, actor_user_id, actor_email: str, notify: bool = True) -> dict:
    email_normalized = email.strip().lower()
    if not email_normalized or "@" not in email_normalized:
        return {"status": "Error", "message": "That doesn't look like a valid email address."}

    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM allowed_signup_emails WHERE email = ?", (email_normalized,))
    if cursor.fetchone():
        return {"status": "Error", "message": f"{email_normalized} is already on the allowlist."}

    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO allowed_signup_emails (email, added_by_email, added_at) VALUES (?, ?, ?)",
        (email_normalized, actor_email, now),
    )
    conn.commit()

    log_audit(
        conn, actor_user_id, actor_email, "signup_email_allowed",
        target_type="allowed_signup_email", target_id=email_normalized,
        details=f"Granted signup access to {email_normalized}",
    )

    email_sent = False
    email_error = None
    if notify:
        email_result = send_signup_access_email(email_normalized)
        email_sent = email_result["status"] == "Success"
        if not email_sent:
            email_error = email_result.get("message")

    if email_sent:
        return {"status": "Success", "message": f"{email_normalized} can now sign up. Notification email sent."}
    elif notify:
        return {"status": "Success", "message": f"{email_normalized} can now sign up, but the notification email failed to send: {email_error}"}
    else:
        return {"status": "Success", "message": f"{email_normalized} can now sign up."}


def remove_allowed_signup_email(conn, email: str, actor_user_id, actor_email: str) -> dict:
    email_normalized = email.strip().lower()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM allowed_signup_emails WHERE email = ?", (email_normalized,))
    conn.commit()

    log_audit(
        conn, actor_user_id, actor_email, "signup_email_revoked",
        target_type="allowed_signup_email", target_id=email_normalized,
        details=f"Revoked signup access for {email_normalized}",
    )
    return {"status": "Success", "message": f"{email_normalized} removed from the allowlist."}


# ---------------------------------------------------------------------
# Admin bootstrap
# ---------------------------------------------------------------------

def ensure_admin_role(conn, user_id: int, email: str):
    """Call this right after a successful signup or login. If the
    email is listed in ADMIN_EMAILS (comma-separated env var), force
    that user's role to 'admin' -- this is how the first admin(s) get
    created, without needing a chicken-and-egg 'an admin must promote
    you' UI flow for the very first one."""
    admin_emails_raw = os.environ.get("ADMIN_EMAILS", "").strip()
    if not admin_emails_raw:
        return
    admin_emails = {e.strip().lower() for e in admin_emails_raw.split(",") if e.strip()}
    if email.strip().lower() in admin_emails:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
        conn.commit()


# ---------------------------------------------------------------------
# Role / permission reads
# ---------------------------------------------------------------------

def get_user_role(conn, user_id: int) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else DEFAULT_ROLE


def has_permission(conn, user_id: int, permission: str) -> bool:
    role = get_user_role(conn, user_id)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT enabled FROM role_permissions WHERE role = ? AND permission = ?",
        (role, permission),
    )
    row = cursor.fetchone()
    return bool(row[0]) if row else False


def get_all_role_permissions(conn) -> dict:
    """Returns {role: {permission: bool, ...}, ...} for rendering the
    admin permission-mapping toggle grid."""
    cursor = conn.cursor()
    cursor.execute("SELECT role, permission, enabled FROM role_permissions")
    result = {role: {} for role in VALID_ROLES}
    for role, permission, enabled in cursor.fetchall():
        result.setdefault(role, {})[permission] = bool(enabled)
    return result


# ---------------------------------------------------------------------
# Admin actions (each one audit-logs itself)
# ---------------------------------------------------------------------

def get_all_users(conn) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, name, role, created_at FROM users ORDER BY created_at ASC")
    return [
        {"id": r[0], "email": r[1], "name": r[2], "role": r[3], "created_at": r[4]}
        for r in cursor.fetchall()
    ]


def set_user_role(conn, target_user_id: int, new_role: str, actor_user_id: int, actor_email: str) -> dict:
    if new_role not in VALID_ROLES:
        return {"status": "Error", "message": f"'{new_role}' is not a valid role."}

    cursor = conn.cursor()
    cursor.execute("SELECT email, role FROM users WHERE id = ?", (target_user_id,))
    row = cursor.fetchone()
    if not row:
        return {"status": "Error", "message": "User not found."}
    target_email, old_role = row

    cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, target_user_id))
    conn.commit()

    log_audit(
        conn, actor_user_id, actor_email, "user_role_changed",
        target_type="user", target_id=str(target_user_id),
        details=f"{target_email}: '{old_role}' -> '{new_role}'",
    )
    return {"status": "Success", "message": f"{target_email} is now '{new_role}'."}


def set_role_permission(conn, role: str, permission: str, enabled: bool, actor_user_id: int, actor_email: str) -> dict:
    if role not in VALID_ROLES or permission not in ALL_PERMISSIONS:
        return {"status": "Error", "message": "Unknown role or permission."}

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE role_permissions SET enabled = ? WHERE role = ? AND permission = ?",
        (1 if enabled else 0, role, permission),
    )
    conn.commit()

    log_audit(
        conn, actor_user_id, actor_email, "permission_changed",
        target_type="role", target_id=role,
        details=f"{permission} -> {'enabled' if enabled else 'disabled'}",
    )
    return {"status": "Success"}


# ---------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------

def log_audit(conn, actor_user_id, actor_email: str, action: str,
              target_type: str = None, target_id: str = None, details: str = None):
    """Call this after any sensitive action succeeds -- order commits,
    outbound email approve/reject, role changes, permission edits,
    signup allowlist edits. Not wrapped in its own try/except
    deliberately: if audit logging itself fails, that's worth
    surfacing, not silently swallowing -- an audit trail that can
    silently fail isn't one you can trust."""
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO audit_log (actor_user_id, actor_email, action, target_type, target_id, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor_user_id, actor_email, action, target_type, target_id, details, now),
    )
    conn.commit()


def get_audit_log(conn, is_azure: bool, limit: int = 200) -> list[dict]:
    """is_azure picks the right "most recent N rows" syntax -- LIMIT is
    SQLite-only; Azure SQL (T-SQL) has no LIMIT clause at all and needs
    TOP placed right after SELECT instead of at the end of the query.
    Same class of bug already fixed twice elsewhere in this project
    (auth.verify_login_code, hold_requests) -- this function was missed
    when it was first written, since it happened to only ever get
    exercised locally against SQLite."""
    cursor = conn.cursor()
    if is_azure:
        # TOP doesn't take a parameterized placeholder the same way a
        # normal value does -- safe to format directly here since
        # `limit` is always an internal, hardcoded default (200), never
        # raw user input.
        cursor.execute(
            f"SELECT TOP {int(limit)} id, actor_email, action, target_type, target_id, details, created_at "
            "FROM audit_log ORDER BY created_at DESC"
        )
    else:
        cursor.execute(
            "SELECT id, actor_email, action, target_type, target_id, details, created_at "
            "FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r[0], "actor_email": r[1], "action": r[2],
            "target_type": r[3], "target_id": r[4], "details": r[5], "created_at": r[6],
        }
        for r in cursor.fetchall()
    ]
