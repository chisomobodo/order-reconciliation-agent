# Reconciliation Agent — Portfolio Project

An AI agent that parses messy wholesale order-change emails, checks stock and
dispatch status against a mock ERP, and either reconciles the order or asks
for clarification when a product reference is genuinely ambiguous. Every
database write goes through a human-approval gate before it happens — Claude
can check whether a change is feasible, but it can never apply one itself.

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
app.py                        Streamlit dashboard
theme.py                       Design system (shipping-manifest aesthetic, stamp badges)
auth.py                         Login/sign-up backend (bcrypt + emailed code + sessions)
auth_theme.py                    Login/sign-up screen design, restyled in the app's palette
email_ingestion.py                Real IMAP ingestion of customer emails from a Gmail label
setup_db.py                     Mock ERP schema + seed data (SQLite)
setup_db_azure.py                Mock ERP schema + seed data (Azure SQL)
generate_test_emails.py           Generates sample_emails.json via Claude
run_batch_test.py                  Batch-runs sample_emails.json through the agent
test_agent.py                       Quick manual test (2 hand-picked emails)
test_azure_connection.py             Sanity check for the Azure SQL backend
Dockerfile                            Container build (Python 3.11, ODBC Driver 18)
.dockerignore                          Excludes venv, local DB, .env, etc. from the build context
requirements.txt                        Python dependencies
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here  # or set in your shell profile
python3 setup_db.py
```

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
3. Create an app password (any name, e.g. "Reconciliation Agent") and copy
   the 16-character code it generates — that's `GMAIL_APP_PASSWORD`, not
   the Gmail login password.

The `users` / `login_codes` / `sessions` tables are created automatically
on first run (`auth.init_auth_schema`, called once per process at
startup), in whichever database `USE_AZURE_DB` currently points at — no
separate setup script needed.

The login/sign-up screen (`auth_theme.py`) is styled directly on
Streamlit's own DOM (`stHorizontalBlock`/`stColumn`/`stTextInputRootElement`)
rather than hand-written wrapper `<div>`s, since the latter doesn't
actually nest around real Streamlit elements and was causing real layout
bugs (a collapsed/blank screen, a stray border seam, an oversized empty
box around the code-entry step's buttons). All fields go through
`st.form`/`st.form_submit_button` so Enter submits a whole group at once.
See "Design notes" below for the session-cookie reliability work, which
was the more substantial fix in this area.

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
   sidebar shows the full `processed_emails` history (sender, subject,
   status, timestamp) as a standing reference, viewable anytime.

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

### Notable engineering challenges

- Azure for Students subscription region restrictions — had to identify the actual allowed regions via policy rather than the general Azure region list
- ACR Tasks (remote container builds) is blocked for student subscriptions — worked around by building locally and pushing the image directly
- Diagnosed a persistent Docker-to-Azure-SQL connection failure by systematically ruling out DNS, network connectivity, TLS certificates, and MTU issues before finding the actual causes: a malformed `.env` file and a Linux-ODBC-driver-specific login format requirement

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

## Limitations & Production Considerations

This is a prototype, not a production system:

- No real ERP integration — the "ERP" is a mock schema seeded with invented data
- Product catalog is a hardcoded SKU list in `agent_config.py`'s tool schema, not a real product-catalog lookup
