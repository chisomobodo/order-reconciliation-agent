# Arbiter — an AI order-reconciliation agent

An AI agent that reads real customer order-change emails (via IMAP), checks
stock and dispatch status against a mock ERP, and either reconciles the order
or asks for clarification when a product reference is genuinely ambiguous —
placing the order "on hold" and matching a later reply back to it
automatically. Sits behind real authentication (signup, password + emailed
code, sessions), and nothing it decides takes effect unreviewed: every
database write and every outbound email goes through a human-approval gate —
Claude can check whether a change is feasible and draft a reply, but it can
never apply the write or send the email itself.

**Fictional company, fictional data.** Not built for or with any real
company's data or systems — the distributor and products in `setup_db.py`
are invented.

## Status so far

- [x] Mock ERP — `setup_db.py` (SQLite) and `setup_db_azure.py` (Azure SQL),
      same schema and seed data in both, with two deliberately ambiguous
      product pairs (two keg sizes, two cider variants) to test the
      clarification path
- [x] `agent_config.py` — system prompt + three tool definitions:
      `get_order_details` (read-only lookup), `verify_order_modification`
      (feasibility check only — never writes), and `request_clarification`
      (ambiguous path — the fix for the "what if Claude guesses the wrong
      SKU" gap)
- [x] `agent_engine.py` / `agent_engine_azure.py` — orchestration loop:
      Claude can call a sequence of tools per email (e.g. look the order up,
      then check the requested change), routes each call to local logic,
      and produces the final customer-facing reply. Both are backed by the
      same tool contract; only the storage layer differs (sqlite3 vs pyodbc)
- [x] Human-approval gate — `verify_order_modification` only checks
      feasibility; the actual write lives in a separate `commit_order_
      modification` function that Claude's tool loop can never call. Only
      the app layer calls it, after a human approves (or an auto-approve
      toggle is on)
- [x] Streamlit dashboard (`app.py`) — shipping-manifest themed UI (see
      `theme.py`): tool-call chain, outcome stamp, customer reply, and the
      approve/reject flow for pending database writes
- [x] Sample/synthetic test emails (`generate_test_emails.py` →
      `sample_emails.json`) — 15 Claude-generated emails across
      unambiguous, ambiguous, and edge-case categories, plus a batch runner
      (`run_batch_test.py`) that scores routing decisions against them
- [x] Azure SQL backend (`agent_engine_azure.py`, `setup_db_azure.py`) —
      swappable via `USE_AZURE_DB`, with a UI-visible mode badge and a
      graceful (non-crashing) error path if the connection isn't configured
- [x] Containerized with Docker and deployed to Azure Container Apps,
      backed by Azure SQL Database (see "Docker & Deployment" below)
- [x] Login/sign-up with email-code two-factor auth (`auth.py`, styled by
      `auth_theme.py`) — gates the entire dashboard behind a session,
      hardened against real cookie-timing races (see "Login &
      Authentication" and "Design notes" below)
- [x] Caching — the sidebar's ERP snapshot (`@st.cache_data(ttl=5)`) and
      per-session `get_order_details` lookups, both invalidated exactly
      at the point a write actually commits (see "Design notes" below)
- [x] Real email ingestion (`email_ingestion.py`) — a "Check Inbox for New
      Orders" button fetches unread emails from a dedicated Gmail label
      via IMAP and lists them for review; a human then chooses which to
      run (individually, or all at once) through the identical
      `run_agent()` pipeline as a manually pasted email — same tool-call
      display, same approval-gate flow; see "Email Ingestion" below
- [x] Approval-gated outbound email (`outbound_email.py`) — every reply
      the agent drafts is queued as a "Pending Approval" row, never sent
      automatically; a human explicitly clicks **Approve & Send** before
      anything reaches a real inbox, mirroring the same check/commit
      split used for database writes; see "Approval-Gated Outbound
      Email" below
- [x] Hold state + four-layer reply matching (`hold_requests.py`) — a
      clarification-needing order is placed "on hold" (full inbound
      email + question preserved) instead of silently dropped while
      waiting on a reply; a later customer reply is matched back to the
      right Hold automatically (Message-ID threading, a `HOLD-{id}` tag,
      or a unique sender match), and a genuinely ambiguous case is
      queued for a human to link manually rather than guessed at; see
      "Hold State & Reply Matching" below
- [x] Follow-up job for stale Holds (`check_holds.py`) — a standalone
      script (built and tested, intended to eventually run as a
      scheduled Azure Container Apps Job — see "Limitations" below for
      its current on-demand-only status) that drafts an honest "still
      on hold" follow-up for any Hold that's gone unanswered past
      `FOLLOW_UP_WINDOW_MINUTES`; still fully gated behind human
      approval, never auto-sent
- [x] Dashboard reorganized into tabs — Process Email, Inbox, Outbound
      Queue, On Hold, History — grouping what had grown into one long
      scrolling page by task; the sidebar (live ERP state) and header
      stay visible regardless of which tab is active
- [x] One database connection per user action, not per function call —
      every DB-touching function across the project now takes an
      already-open `conn` instead of opening and closing its own; a
      single "Process" click used to open 5+ separate Azure SQL
      connections in sequence, now opens exactly one
- [x] Role-based access control (`rbac.py`) — a signup allowlist, three
      roles (admin/approver/reviewer) with an editable permission
      mapping (who can process emails, approve order changes, approve
      outbound emails, manage users), and an audit log recording who
      did what. A genuinely separate **Admin** page (`admin_page.py`,
      via Streamlit's `st.navigation`/`st.Page`) — not another tab —
      appears in navigation only for an admin; see "Roles & Admin
      Access" below
- [x] Restructured into a real Streamlit multipage app — the dashboard
      (`dashboard_page.py`) and the admin page (`admin_page.py`) are
      separate page files, routed to by `app.py` (now just the entry
      point: auth gate, session validation, role lookup, then
      `st.navigation`); shared connection/backend logic lives in
      `app_core.py` so both pages can reach it without re-executing
      `app.py` itself (see "Design notes" below)

## How it works

1. An inbound email goes to Claude along with the system prompt and three
   tools (`agent_config.py`).
2. Claude runs a multi-step tool loop (`run_agent` in `agent_engine.py`):
   if the email references an order ID, it must call `get_order_details`
   first — always, so relative-quantity requests ("add 2 more") and
   already-dispatched orders are caught early and consistently.
3. If the product reference is ambiguous (e.g. "kegs" with two sizes in
   stock), Claude calls `request_clarification` instead of guessing, and
   that question becomes the reply directly.
4. If it's unambiguous, Claude calls `verify_order_modification`, which
   only checks feasibility (order exists, not dispatched, enough stock) —
   it never writes to the database.
5. `app.py` reads the outcome. If the check succeeded, the proposed change
   (order ID, SKU, new quantity) either commits immediately (auto-approve
   toggle on) or waits as a "Pending Approval" stamp with **Approve & Apply**
   / **Reject** buttons (the default). Only `commit_order_modification` —
   called from the app layer, never from Claude's loop — actually writes.

## Project structure

```
agent_config.py            System prompt + tool definitions (3 tools)
agent_engine.py             Orchestration loop, SQLite backend
agent_engine_azure.py        Orchestration loop, Azure SQL backend (pyodbc)
app.py                        Entry point / router -- auth gate, session
                               validation, role lookup, then st.navigation()
app_core.py                    Shared connection/backend logic + branded
                               header, used by app.py and both pages below
dashboard_page.py              Main dashboard page (the five tabs) -- routed
                               to via st.navigation() for every signed-in user
admin_page.py                  Admin page (User Management, Permission
                               Mapping, Audit Log) -- routed to via
                               st.navigation() ONLY when role == 'admin'
rbac.py                         Signup allowlist, roles + editable permission
                                 mapping, audit log
theme.py                       Design system (shipping-manifest aesthetic, stamp badges)
auth.py                         Login/sign-up backend (bcrypt + emailed code + sessions)
auth_theme.py                    Login/sign-up screen design, restyled in the app's palette
email_ingestion.py                Real IMAP ingestion of customer emails from a Gmail label
outbound_email.py                  Approval-gated outbound email queue (draft -> human approves -> sent)
hold_requests.py                    Hold-state tracking + four-layer reply-to-clarification matching
check_holds.py                       Standalone script: drafts follow-ups for overdue Holds
init_db.py                            Schema init for auth/inbox/outbound/hold/rbac tables (run once, before app.py)
setup_db.py                     Mock ERP schema + seed data (SQLite)
setup_db_azure.py                Mock ERP schema + seed data (Azure SQL)
generate_test_emails.py           Generates sample_emails.json via Claude
run_batch_test.py                  Batch-runs sample_emails.json through the agent
test_agent.py                       Quick manual test (2 hand-picked emails)
test_azure_connection.py             Sanity check for the Azure SQL backend
assets/                                Arbiter branding: header/hero icons, favicon
Dockerfile                            Container build (Python 3.11 bookworm, ODBC Driver 18)
entrypoint.sh                          Container entrypoint: runs init_db.py, then starts Streamlit
.dockerignore                          Excludes venv, local DB, .env, etc. from the build context
requirements.txt                        Python dependencies
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here  # or set in your shell profile
python3 setup_db.py   # mock ERP schema + seed data (products, orders, stock)
python3 init_db.py    # app schema: auth/session, inbox-tracking, outbound-email
                       # queue, hold-requests, and rbac tables
```

These are two separate one-time scripts because they own different tables:
`setup_db.py` seeds the mock ERP the agent reasons about; `init_db.py` creates
everything the app itself needs to run (login, email tracking, the approval
queue, Hold state, roles/permissions). `init_db.py` used to run automatically inside `app.py` on
first page load — it's now a required, separate step (see "Design notes"
below for why), so **`streamlit run app.py` will fail without it** if this is
a first-time setup.

Run the dashboard:

```bash
streamlit run app.py
```

Other entry points:

```bash
python3 test_agent.py          # two hand-picked emails (clear + ambiguous), printed to console
python3 generate_test_emails.py  # regenerates sample_emails.json via Claude
python3 run_batch_test.py      # runs every sample email through the agent, scores routing decisions
```

### Optional: Azure SQL mode

The app can run against Azure SQL instead of local SQLite by setting
`USE_AZURE_DB=true` (see `agent_engine_azure.py` / `app.py`). This needs
four more env vars — `AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`,
`AZURE_SQL_USERNAME`, `AZURE_SQL_PASSWORD` — and a system-level dependency
that `pip install` can't provide: the **ODBC Driver 18 for SQL Server**.
`pyodbc` (in `requirements.txt`) is just the Python binding; without the
driver installed on the machine, connecting will fail even with valid
credentials.

- **Windows:** [download the MSI from Microsoft](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
- **macOS:** `brew install msodbcsql18` (via the `microsoft/mssql-release` tap)
- **Linux (Debian/Ubuntu):** follow Microsoft's [`apt` instructions](https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server) for `msodbcsql18`

Once the driver and env vars are in place:

```bash
python3 setup_db_azure.py   # one-time: creates schema + seed data in Azure SQL
python3 test_azure_connection.py   # sanity check before wiring it into the app
```

Then launch the dashboard with `USE_AZURE_DB=true` set. The header shows a
badge for whichever backend is active. If `USE_AZURE_DB=true` but the driver
or env vars are missing, `app.py` shows a connection error in the UI rather
than crashing — it does not fall back to SQLite automatically.

### Login & Authentication

The dashboard is gated behind login/sign-up (`auth.py`, styled by
`auth_theme.py`) — nothing else renders until there's a valid session.
Flow: sign up with email + password (bcrypt-hashed, never stored in
plaintext) → log in with password → a 6-digit code is emailed and must be
entered within 10 minutes → a session cookie is issued, valid for 20
minutes of inactivity (sliding window; any interaction refreshes it).

Sending the code requires a Gmail account with an **App Password** (not
the account's normal password):

```bash
export GMAIL_ADDRESS=youraddress@gmail.com
export GMAIL_APP_PASSWORD=your_16_char_app_password
```

To generate one:
1. Enable 2-Step Verification on the Gmail account, if not already on:
   [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create an app password (any name, e.g. "Arbiter") and copy
   the 16-character code it generates — that's `GMAIL_APP_PASSWORD`, not
   the Gmail login password.

The `users` / `login_codes` / `sessions` tables (along with every other
app-owned table — inbox tracking, the outbound-email queue, hold
requests) are created by `init_db.py`, run once before `streamlit run
app.py` (see "Setup" above), in whichever database `USE_AZURE_DB`
currently points at. This used to run automatically inside `app.py`
itself on first page load; it's now a standalone prerequisite step
instead — see "Design notes" below.

The login/sign-up screen (`auth_theme.py`) is styled directly on
Streamlit's own DOM (`stHorizontalBlock`/`stColumn`/`stTextInputRootElement`)
rather than hand-written wrapper `<div>`s, since the latter doesn't
actually nest around real Streamlit elements and was causing real layout
bugs (a collapsed/blank screen, a stray border seam, an oversized empty
box around the code-entry step's buttons). All fields go through
`st.form`/`st.form_submit_button` so Enter submits a whole group at once.
See "Design notes" below for the session-cookie reliability work, which
was the more substantial fix in this area.

### Roles & Admin Access

On top of authentication, `rbac.py` adds a signup allowlist, three roles
(**admin**, **approver**, **reviewer**) with an editable permission
mapping, and an audit log.

The signup allowlist has two layers, checked in this order by
`rbac.is_email_allowed_to_signup()`:

1. **A database table (`allowed_signup_emails`)** — this is the one an
   admin manages day-to-day, live, from the Admin page's **Signup
   Access** section (below): add an email, it can sign up immediately;
   revoke it, it's blocked again immediately. No env var edit or
   redeploy needed for the common case of adding a new person. Adding
   an email can also send that person a notification email with the
   app's sign-in link (`rbac.send_signup_access_email`) — reuses the
   same `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` as login codes, and reads
   the sign-in link itself from `APP_URL`.
2. **`ALLOWED_SIGNUP_EMAILS`** (comma-separated, case-insensitive env
   var) — a permanent fallback checked if the email isn't in the DB
   table. Exists so an admin can never lock themselves out even if the
   database table is empty or something's wrong with it.

**If NEITHER the DB table nor the env var has any entries at all,
sign-up is open to anyone** — a deliberate choice so a fresh setup
doesn't silently lock everyone out before the first admin has had a
chance to add anyone to either list.

```bash
export ALLOWED_SIGNUP_EMAILS=you@example.com,teammate@example.com  # optional -- permanent fallback
export ADMIN_EMAILS=you@example.com                                # optional
export APP_URL=https://reconciliation-agent.bravecoast-56485b41.swedencentral.azurecontainerapps.io  # optional
```

- **`APP_URL`** — the app's own public URL, included in the "you've
  been granted access" notification email so the recipient has a
  direct sign-in link instead of having to be told it separately.
  Optional: if unset, the email still sends, just with "(Ask your
  administrator for the sign-in link.)" in place of a URL. For local
  dev this would be `http://localhost:8501` (or whichever port);
  there's no correct default to fall back to automatically, since the
  app has no way to know its own externally-reachable address.
- **`ADMIN_EMAILS`** (comma-separated, case-insensitive) — any email
  listed here is force-promoted to the `admin` role the moment it signs
  up OR logs in (checked both places, so it doesn't matter which one
  happens first for a brand-new account). This is the bootstrap: it's
  how the very first admin gets created at all, without a
  chicken-and-egg "an admin has to promote you" flow. Every other role
  change after that happens through the Admin page itself (see below),
  not through further env var edits — though re-adding an email to
  `ADMIN_EMAILS` and logging in again always re-grants admin, which
  doubles as a recovery path if every admin account is ever
  accidentally demoted.

Once signed in as an admin, an **Admin** entry appears in Streamlit's
page navigation (built via `st.navigation`/`st.Page` — a genuinely
separate page, not another tab) with four sections:

- **User Management** — every user, with a role dropdown + Save per row
  (`rbac.set_user_role`).
- **Signup Access** — the database allowlist described above: every
  currently-allowed email (who added it, when), an "Add email" field
  (with an "Also send them a notification email" checkbox, checked by
  default — un-check it to add someone without emailing them), and a
  Revoke button per row (`rbac.add_allowed_signup_email` /
  `rbac.remove_allowed_signup_email`). Shows the real outcome inline
  after adding, including whether the notification email actually
  sent, not just a generic "done."
- **Permission Mapping** — a grid (rows = roles, columns = permissions:
  `can_process_emails`, `can_approve_order_changes`,
  `can_approve_outbound_emails`, `can_manage_users`) with checkboxes
  that take effect immediately (`rbac.set_role_permission`).
- **Audit Log** — every role change, permission change, signup
  allowlist edit, order-change approval, and outbound-email
  approve/reject, with who did it, most recent first (`rbac.log_audit`
  / `rbac.get_audit_log`).

The Admin page genuinely isn't reachable by anyone else, not just
hidden: it's only ever added to the list passed to `st.navigation()`
when the current user's role is `'admin'`, and Streamlit falls back to
the default (dashboard) page for any URL that doesn't match a page in
that list — so a non-admin who guesses or bookmarks the admin URL still
never reaches it, confirmed directly (not just reasoned about) during
this feature's own testing.

A denied action elsewhere in the app (an approval button without the
right permission) disables that specific button with a short caption
explaining why, rather than doing nothing on click.

### Email Ingestion

The **📥 Check Inbox for New Orders** button (`email_ingestion.py`) is
two steps, not one:

1. **Fetch and review.** Clicking it pulls real, unread customer emails
   over IMAP from a dedicated Gmail label — scoped deliberately to that
   one label, never the whole inbox, so the agent can never see or
   process personal email — and inserts any not already known (deduped
   by uid) into a `pending_emails` table. The pending list shown on
   screen is queried fresh from that table on every render, not from
   session state, so it's correct regardless of which browser session
   did the fetching and survives a page refresh. Clicking Check Inbox
   repeatedly is always safe: the dedup means it can never duplicate an
   email still pending or resurrect one already processed. Nothing runs
   through the agent yet, and nothing is marked read in Gmail yet.
2. **Choose what to process.** A **Process** button on each card handles
   just that email; **▶️ Process All** works through the whole list, one
   at a time. Either way, an email only runs through the exact same
   `run_agent()` pipeline as a manually pasted one once a human triggers
   it — same tool-call chain, same approval-gate logic. Its result also
   replaces whatever the "Agent Execution" panel was showing (the same
   single-result display used for manually pasted emails, not a growing
   list — the panel always shows only the most recently processed
   email). On success, the email moves from `pending_emails` to a
   `processed_emails` table (with a timestamp, tool-chain summary, and
   final status) and is marked read in Gmail (`mark_processed`) — both
   immediately after that specific email's own processing, not batched
   at the end. If `run_agent()` itself raises, the email is left in
   `pending_emails` and never marked read, so it's retried on the next
   check instead of silently lost. A **Processed Emails** log in the
   **History** tab shows the full `processed_emails` history (sender,
   subject, status, timestamp) as a standing reference, viewable anytime.

Reuses `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` from "Login &
Authentication" above (Gmail App Passwords work for both sending, via
SMTP, and reading, via IMAP) — no separate credential needed. One more
optional env var:

```bash
export IMAP_LABEL=OrderRequests  # optional -- this is the default
```

One-time setup in Gmail itself (not code): create a label (e.g.
`OrderRequests`) and route or manually apply it to whichever emails
should be treated as customer order-change requests.

Every reply the agent drafts (clarification questions, order-change
confirmations — see "Approval-Gated Outbound Email" below) sets its
`Reply-To` to this same intake address, not a no-reply one, since a
customer's reply needs to land back in `OrderRequests` for the next
"Check Inbox" to pick it up — this is what the Hold-state feature (see
"Hold State & Reply Matching" below) is waiting on. Defaults to
`<GMAIL_ADDRESS local-part>+orders@<domain>` (Gmail's own
plus-addressing, matching the filter rule that routes mail into the
`OrderRequests` label); override it explicitly if needed:

```bash
export ORDER_INTAKE_EMAIL=youraddress+orders@gmail.com  # optional -- derived from GMAIL_ADDRESS if unset
```

(Login-code emails from `auth.py` are unaffected — those keep a genuine
no-reply address, since a reply is never expected there.)

### Approval-Gated Outbound Email

Every reply `run_agent()` drafts — a clarification question, an
order-change confirmation, anything — goes into an approval queue
(`outbound_email.py`) as a **Pending Approval** row first. Nothing is
ever sent automatically: a human reviews the draft in the **Outbound
Queue** tab and explicitly clicks **✅ Approve & Send** before anything
reaches a real inbox, or **❌ Reject** to discard it. This is the exact
same check/commit split already used for database writes
(`verify_order_modification` vs `commit_order_modification`), extended
to outbound email — `queue_draft()` only ever creates a row;
`approve_and_send()` is the only function that actually calls SMTP, and
it's never reachable from Claude's own tool loop.

Sent emails are logged permanently in the **Sent Emails** audit trail
(same tab, below the pending queue) — nothing appears there without a
matching explicit approval click.

### Hold State & Reply Matching

When `request_clarification` fires *and* the customer gave an order ID,
the order is placed **on hold** (`hold_requests.py`) rather than
silently lost while waiting for an answer: the full original inbound
email and the exact clarifying question sent are both stored (not a
summary — a human reviewing this later needs real context). Holds
appear in the **On Hold** tab under "⏳ Awaiting Customer Reply".

When a later email comes in, it's checked against every open Hold
before being treated as a new request, most-reliable signal first:

1. **Message-ID threading.** The clarifying question's outgoing
   `Message-ID` header (generated via `email.utils.make_msgid()` at
   Hold-creation time, before the email is even sent) is stored on the
   Hold. If the reply's `In-Reply-To` or `References` header matches
   it, that's a match — this can't be a coincidental false positive,
   since the ID was generated locally.
2. **`HOLD-{id}` tag.** Every clarifying question's subject and body
   include a `[HOLD-{id}]` reference and ask the customer to keep it in
   their reply. Survives a mail client that strips threading headers.
3. **Sender-email fallback.** If neither of the above matches, and
   *exactly one* open Hold belongs to that sender, that's the match.
4. **Manual linking.** If more than one open Hold belongs to that
   sender, the system never guesses — the email is queued in the **🔗
   Needs Manual Linking** section (On Hold tab) with the candidate
   Holds listed, for a human to pick the right one (or mark it as a
   genuinely new, unrelated request).

A matched reply is combined with the original request and the
clarifying question into full context, run back through `run_agent()`,
and the Hold is marked resolved. If that reply is itself still
ambiguous, the agent's new question is sent back to the customer as an
ordinary reply — the original Hold is not left open or replaced with a
second one in this version.

The follow-up job (`check_holds.py`) is a standalone script, run on
demand today (`python3 check_holds.py`) — it's written and tested, and
designed to eventually run as a scheduled **Azure Container Apps Job**,
deliberately *not* a background thread inside the web app (Container
Apps scales the web app to zero on idle, which would silently kill any
in-process thread too), but that scheduled deployment isn't set up yet;
see "Limitations" below. Past
`FOLLOW_UP_WINDOW_MINUTES` (default 30) with no reply, it drafts an
honest follow-up — "this remains on hold until we hear from you," never
"we've gone ahead and processed it" — queues it the normal
approval-gated way, and flags the Hold "Past Follow-Up" in the **🚨
Needs Attention** section. Nothing here is ever auto-processed,
regardless of how long it waits.

```bash
export FOLLOW_UP_WINDOW_MINUTES=30  # optional -- this is the default
```

## Docker & Deployment

The app is containerized (`Dockerfile`, `.dockerignore`) and deployed to
Azure Container Apps, backed by Azure SQL Database. The image is Python 3.11
on Debian bookworm with the ODBC Driver 18 for SQL Server installed for
`pyodbc`; `USE_AZURE_DB=true` is set inside the image, since a stateless
container has no reason to run against local SQLite.

Build and run locally:

```bash
docker build -t reconciliation-agent .
docker run -p 8501:8501 --env-file .env reconciliation-agent
```

Deployment: built and tested locally, then pushed directly to Azure
Container Registry and deployed via the Azure CLI. This was necessary
because ACR Tasks (Azure's remote build service) is restricted on Azure for
Students subscriptions.

Deploy pinned to the exact image digest, **not** the mutable `:latest` tag:

```bash
docker build -t reconciliation-agent .
az acr login --name ca289d1c4e31acr
docker tag reconciliation-agent ca289d1c4e31acr.azurecr.io/reconciliation-agent:latest
docker push ca289d1c4e31acr.azurecr.io/reconciliation-agent:latest
# docker push's final line looks like: latest: digest: sha256:<digest>

az containerapp update --name reconciliation-agent --resource-group reconciliation-agent-rg \
  --image "ca289d1c4e31acr.azurecr.io/reconciliation-agent@sha256:<digest>"
```

(Container/registry/resource-group names still use the pre-rebrand
`reconciliation-agent` naming — real Azure resource identifiers, not
display branding, left as-is deliberately rather than migrated as part
of a code change.)

The digest pin is not optional, and cost real debugging time to learn:
`az containerapp update --image ...:latest` does **not** reliably restart
a running replica just because `:latest` now points to different content
in the registry. Confirmed directly — a `docker push` succeeded with a
genuinely new digest, `az containerapp update` reported
`"provisioningState": "Succeeded"`, and the running container had still
not restarted (`az containerapp replica list` showed the same
multi-day-old start timestamp, `restartCount: 0`). Azure Container Apps
only triggers a real restart when the image *reference string itself*
changes — since `:latest` is always the same string, repeated deploys
against it can silently leave stale code running indefinitely while
every individual step reports success. A digest is a different string
on every push, so pinning to it is what actually guarantees a deploy
takes effect.

### Notable engineering challenges

- Azure for Students subscription region restrictions — had to identify the actual allowed regions via policy rather than the general Azure region list
- ACR Tasks (remote container builds) is blocked for student subscriptions — worked around by building locally and pushing the image directly
- Diagnosed a persistent Docker-to-Azure-SQL connection failure by systematically ruling out DNS, network connectivity, TLS certificates, and MTU issues before finding the actual causes: a malformed `.env` file and a Linux-ODBC-driver-specific login format requirement
- The `:latest`-tag deploy trap above — every step in a deploy (`docker push`, `az containerapp update`) can report success while the running container never actually changes; root-caused by comparing the replica's real start timestamp against the deploy time, not by trusting any command's own "succeeded" output
- `docker push` can fail with an expired ACR auth token while still leaving the next command in a script free to run — silently redeploying whatever was already in the registry rather than the new build, with no error indicating that's what happened; the fix is always reading the actual push output, not just its exit code

## Design notes worth remembering

**Ambiguity handling (`request_clarification`).** An enum-constrained tool
call will always resolve to *some* SKU even when the customer's wording was
genuinely ambiguous (e.g. "kegs" with two sizes in stock). Rather than a
numeric confidence threshold, the system prompt uses a hard rule: any
genuine ambiguity in the SKU mapping routes to a clarifying question, never
a guess — because a wrong guess ships the wrong product, which is worse
than a short delay.

**Mandatory order lookup (`get_order_details`).** Customers describe
changes relatively far more often than as absolute final numbers ("add 2
more", "same quantity, different variant"). Early testing also showed
Claude skipping the lookup when it judged the ambiguity as self-contained,
which produced inconsistent behavior between similar emails. The system
prompt now requires calling `get_order_details` for every email with an
order ID, no exceptions — this makes behavior predictable, surfaces
"not found" / "already dispatched" cases before a modification is even
attempted, and gives every order-referencing email a consistent audit
trail, at the cost of a small number of extra (cheap) tool calls.

**Human-approval gate (`verify_order_modification` vs
`commit_order_modification`).** The original design had one function that
both checked feasibility and wrote to the database, called directly from
Claude's tool loop — meaning a tool call was also a database mutation, with
no review step in between. `verify_order_modification` now only checks
feasibility and reports back; the write lives in `commit_order_modification`,
which Claude's loop never touches. `app.py` decides when to call it — either
immediately, if the auto-approve toggle is on, or only after a human clicks
**Approve & Apply** (the default). This closes the gap where a hallucinated
or premature tool call could silently mutate the ERP.

**Swappable storage backend.** `agent_engine.py` (SQLite) and
`agent_engine_azure.py` (Azure SQL via pyodbc) implement the identical tool
contract and approval-gate split, so `app.py` can switch between them via
`USE_AZURE_DB` without changing any agent logic — fast local iteration day
to day, with a working cloud version to demo when needed.

**Email-code login (`auth.py`).** Password alone is one factor; the emailed
code is a genuine second one, not just a re-check of something already
known. Sessions use a sliding inactivity window rather than a fixed expiry
so an active user is never logged out mid-task, but an abandoned tab
still expires. The session token lives in a browser cookie (via
`extra-streamlit-components`) so a page reload doesn't require logging in
again, but the server re-validates it — and refreshes the window — on
every single rerun, not just once at load.

**Session cookie reliability (`app.py`).** `extra-streamlit-components`
wraps a browser-side custom component, and its public API
(`CookieManager.get()`/`get_all()`) hardcodes a `default={}` fallback —
making "the component's browser round-trip hasn't finished yet"
indistinguishable from "it finished, and there's genuinely no cookie."
That ambiguity was causing a hard refresh to occasionally log a
genuinely-valid session out. The fix reads the same underlying component
call directly with our own sentinel default (`None`) instead, so the two
cases can actually be told apart; if unresolved, it retries with a real
`time.sleep()` between attempts (bounded, so a broken component can't
hang the page forever) — a bare `st.rerun()` doesn't help here, since a
rerun triggered from inside the script is purely server-side and never
waits on the browser at all. The write side (`.set()` on login,
`.delete()` on logout) had the mirror-image bug: an `st.rerun()`
immediately after issuing the cookie instruction was tearing the
just-mounted component back out of the page before its iframe had time
to actually execute `document.cookie = ...`, so the write silently never
happened. Both are now followed by a short real delay before the rerun
that would otherwise cut them off.

**Caching (`get_db_snapshot`, `get_order_details`).** Streamlit reruns
the whole script on almost every UI interaction, so the sidebar's ERP
snapshot is cached for 5 seconds (`@st.cache_data(ttl=5)`) rather than
re-querying the database on every click when nothing's changed.
`get_order_details` is cached per browser session (via
`st.session_state`, not a bare module-level dict, since a real dict
would be shared across every visitor hitting the same server process),
since the same order is often looked up more than once while a single
email is being processed. Both caches are invalidated exactly at the
point `commit_order_modification` actually writes — the sidebar cache
via an explicit `st.cache_data.clear()` right after a successful commit,
the order-details cache by evicting that specific `order_id`'s entry.
Feasibility checks (`check_order_modification`) are never cached, only
ever queried live, so a stale cache can never be the reason an approval
decision is wrong — only the reason a *display* is a few seconds behind.

**"Processed" vs "approved" (`email_ingestion.py`, `app.py`).**
Fetching and processing are deliberately two separate steps — "Check
Inbox" only lists what's there; nothing runs through the agent or gets
marked read until a human clicks Process (or Process All) — so a
customer's actual wording is visible before anything acts on it.
`mark_processed(uid)` is then called once `run_agent()` has reached an
outcome for that specific email — auto-committed, queued for manual
approval, or no action needed — not once a human has actually clicked
Approve on the result. A batch of ingested emails can produce several items that each
need independent manual review, so they go into a list
(`pending_approval_queue`) rather than the single `pending_approval` slot
the manual-paste flow uses, and are shown one at a time with the same
card/stamp styling; marking each email read as soon as the agent's part
of the work is done means a still-undecided approval lives in the app's
own state, not in the inbox — the same email is never re-fetched and
re-run through the agent on a later check just because nobody's clicked
Approve yet. An email is deliberately left unread (not marked processed)
if `run_agent()` itself raises, so a transient failure (e.g. a dropped
Claude API call) gets retried on the next check instead of silently
losing that email.

**One connection per action, not per function (`app.py` and every
module it calls into).** Every DB-touching function across the project
originally opened and closed its own connection — clean in isolation,
but a single user action routinely chains through five or more of them
(e.g. "Process" touching `get_order_details`, `verify_order_
modification`, `create_hold`, `queue_draft`, `record_processed_email`),
each paying its own connection-handshake cost. Over Azure SQL that cost
is real and stacks up visibly. Every such function now takes an
already-open `conn` instead of a connection-opening callable, and
`app.py` opens exactly one connection per action (`with
closing(auth_get_connection()) as conn:`, guaranteeing it closes even
if something raises partway through) and threads it through the whole
chain, including into `run_agent()`'s internal Claude tool-use loop.
The two calling conventions are deliberately not both supported —
passing the old connection-opening callable where a function now
expects an open connection fails immediately and loudly (`AttributeError:
'function' object has no attribute 'cursor'`) rather than silently
working, so a missed call site can't hide as a quiet bug.

**Tabs, not one long scrolling page (`app.py`).** As Inbox, Outbound
Queue, Hold state, and History each grew their own section, the
dashboard became one long stacked column. Reorganizing into
`st.tabs()` — Process Email, Inbox, Outbound Queue, On Hold, History —
is a pure layout change: identical widgets, keys, session state, and
function calls, just grouped by task instead of by however each
feature happened to be bolted on. The sidebar (live ERP state) and
header stay outside the tabs, visible regardless of which one is
active, since they're context a user wants no matter what they're
doing.

**Schema setup moved out of the request path (`init_db.py`,
`entrypoint.sh`).** Table creation for auth/session, inbox-tracking, the
outbound-email queue, and hold-requests used to run inside `app.py`
itself — once per process, on whichever page load happened to be the
first request after a cold start. That still meant a real user's page
load paid for a multi-second "Connecting..." wait (or, before a retry
loop existed, a raw exception) for setup work that had nothing to do
with their own request. `init_db.py` is now a standalone script that
`entrypoint.sh` runs to completion, in the container, *before* Streamlit
starts accepting any HTTP traffic at all — so no request, from any user,
at any time, can race an uninitialized database. The same script is
just as needed for local dev (see "Setup" above); it isn't an
Azure-only step.

**Multipage restructuring (`app.py`, `app_core.py`, `dashboard_page.py`,
`admin_page.py`).** Adding the Admin page as a genuinely separate page
(not another tab) meant moving off a single flat script. `app.py` is now
just the entry point — page config, CSS, the auth gate, session
validation, role lookup, then `st.navigation()` — and hands off to
`dashboard_page.py` or `admin_page.py` as plain Python functions.
Connection handling, backend selection, and the branded header moved
into a third file, `app_core.py`, that both pages import — deliberately
NOT something either page reaches by `import app` directly. Streamlit
runs `app.py` as `__main__` (`streamlit run app.py`), and Python's
import machinery doesn't register a script run that way under its own
filename in `sys.modules` — so a later `import app` from another file
loads it a SECOND time as a distinct module object, re-running every
top-level statement in it, including `st.set_page_config()`, which
raises the moment it executes twice in one script run. `app_core.py`
holding the shared pieces instead is what avoids that entirely.

**RBAC extends the check/commit split to "who's allowed to click
approve" (`rbac.py`, `dashboard_page.py`).** The core safety principle
already covered database writes (`verify_order_modification` vs
`commit_order_modification`) and outbound email (`queue_draft` vs
`approve_and_send`) — RBAC adds a third layer on top of both: not just
"is this action reviewed by a human," but "is THIS human allowed to
review it." Permissions are read live from the database on every page
render, never cached, the same principle `check_order_modification`
already follows ("a stale cache can never be the reason an approval
decision is wrong") — a role change made in the Admin page takes effect
on the affected user's very next rerun. The auto-approve toggle is
gated by the same `can_approve_order_changes` permission as the manual
"Approve & Apply" button, and the commit path itself re-checks that
permission a second time right before writing — otherwise auto-approve
would let anyone bypass the button's own gate just by leaving the
toggle on.

## Limitations & Production Considerations

This is a prototype/portfolio project, not a production system:

- No real ERP integration — the "ERP" is a mock schema seeded with invented data
- Product catalog is a hardcoded SKU list in `agent_config.py`'s tool schema, not a real product-catalog lookup
- The follow-up job for stale Holds (`check_holds.py`) is written and tested but not yet deployed as an actual scheduled job — it only runs when invoked manually today, not on a real cadence
- One Gmail account (via App Password) does triple duty as the login-code sender, the order-intake IMAP mailbox, and the outbound-reply SMTP sender — not a dedicated transactional email service, and a single point of failure for all three
- No rate limiting on signup, login-code requests, or login attempts — a real deployment would need this before being open to untrusted traffic
- SQLite and Azure SQL schemas are hand-maintained in parallel (`setup_db.py`/`setup_db_azure.py`, plus a small `_ensure_column()` migration helper per module for later column additions) rather than through a real migration framework
- `st.dataframe`'s native rendering (canvas-based, via glide-data-grid) can't be restyled through the app's injected CSS at all — every dataframe in the app (sidebar, History, Outbound Queue) consistently shows Streamlit's default unthemed table look; a known, accepted limitation rather than a bug
- No structured logging/observability beyond container stdout and Streamlit's own error surface — no request tracing, metrics, or alerting
- Single-tenant design throughout (one shared `mock_erp.db` / Azure SQL database for every signed-up user) — there's no per-tenant data isolation
