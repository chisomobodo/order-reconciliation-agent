# Arbiter — Project Memory

Read this file fully before making changes. It captures architecture,
design decisions (and WHY), current status, and hard-won gotchas from
past debugging sessions, so they don't get rediscovered from scratch.

## What this is

**Arbiter** — an AI agent that parses messy wholesale order-change
emails for a **fictional** beverage distributor, checks stock/dispatch
status against a database, and either reconciles the order or asks for
clarification. Built with Claude Sonnet 5, Python, Streamlit. No real
company data or systems — the distributor and products are invented.
(Project name was "Reconciliation Agent" earlier in development;
rebranded to Arbiter — see "Branding" below. `Reconciliation Agent`
still appears as a descriptive role in a few places, e.g. the Claude
system prompt's "Arbiter, the Autonomous Supply Chain Reconciliation
Agent" — that's deliberate, not a leftover.)

## Core safety architecture — DO NOT WEAKEN THIS

**Claude never has a tool that writes to the database or sends email
directly.** This is structural, not a prompt instruction:

- `verify_order_modification` (Claude's tool) → calls `check_order_
  modification()`, which is READ-ONLY. It only checks feasibility.
- The actual write lives in `commit_order_modification()` — called
  ONLY from the app layer (`app.py`), after either a human clicks
  "Approve & Apply" or the auto-approve toggle is on. Claude's tool
  loop can never reach this function.
- Same pattern for outbound email: `outbound_email.queue_draft()`
  only ever creates a "Pending Approval" row. `approve_and_send()` is
  the ONLY function that sends anything, called only from an explicit
  human click.

If asked to "simplify" or "speed up" the approval flow, do NOT remove
this separation — it's the central design principle of the whole
project, established deliberately after an earlier version let a tool
call double as a database mutation with no review step.

**Ambiguity handling:** `request_clarification` exists because an
enum-constrained SKU field always resolves to *some* value even when
ambiguous ("kegs" with 2 sizes in stock). The rule is a hard rule, not
a confidence threshold: any genuine ambiguity → ask, never guess. The
same "never guess" principle governs Hold-reply matching too — see
"Hold state" below.

**Mandatory order lookup:** `get_order_details` is called for EVERY
email with an order ID, no exceptions — this was made unconditional
after testing showed Claude inconsistently skipping it when it judged
an ambiguity as "self-contained," causing inconsistent behavior
between similar emails.

## File map

- `agent_config.py` — system prompt + 3 tool definitions. The system
  prompt names the agent "Arbiter" explicitly (its own self-identity,
  in case it ever refers to itself in a reply).
- `agent_engine.py` — orchestration loop, SQLite backend. Also exposes
  a plain `get_connection()` (for standalone scripts that need their
  own connection outside app.py's per-action lifecycle — see
  "Connection lifecycle" below).
- `agent_engine_azure.py` — same logic, Azure SQL backend (pyodbc).
  **IMPORTANT:** the login format must be `username@server-shortname`
  for the Linux ODBC driver, NOT just `username` — see Gotchas below.
- `app.py` — Streamlit dashboard, organized into 5 tabs (Process
  Email, Inbox, Outbound Queue, On Hold, History). Header + sidebar
  (Live ERP State) render outside the tabs, always visible. Switches
  SQLite/Azure via `USE_AZURE_DB` env var. Gates the whole app behind
  login (see `auth.py`).
- `theme.py` — visual design system: manifest/stamp aesthetic, dark +
  light tokens, a spacing scale (`--space-xs..xl`), shared heading/
  empty-state components (`section_heading_html`, `field_label_html`,
  `empty_state_html`) used consistently across every tab, and CSS
  overrides for native Streamlit chrome (alerts, expanders, tabs) that
  would otherwise stand out unstyled next to the custom cards.
- `setup_db.py` / `setup_db_azure.py` — schema + seed data (5 SKUs,
  3 orders, deliberately ambiguous: 2 keg sizes, 2 cider variants)
- `auth.py` — signup, password + email-code login, sessions with a
  20-min sliding inactivity timeout. bcrypt hashing, never plaintext.
  Login-code emails stay genuinely no-reply (unlike outbound_email.py
  — see below).
- `auth_theme.py` — login/signup screen visual design (split hero +
  form layout, matches theme.py palette), includes the Arbiter icon in
  the hero panel.
- `email_ingestion.py` — real IMAP fetch via Gmail, scoped to the
  `OrderRequests` label (matched via plus-addressed intake:
  `GMAIL_ADDRESS+orders@gmail.com`, filtered by a Gmail rule). Uses
  `BODY.PEEK[]` not `RFC822` — see Gotchas. Also extracts
  In-Reply-To/References headers, used for Hold-reply matching.
- `outbound_email.py` — approval-gated email sending queue. Extends
  the check/commit principle to outbound email. Order-related emails
  set `Reply-To` to `ORDER_INTAKE_EMAIL` (routes a customer's reply
  back into IMAP ingestion) — deliberately NOT the no-reply address
  `auth.py`'s login codes use, since a reply is never expected there.
  Threads a Hold's `sent_message_id` through to the real outgoing
  `Message-ID` header at send time (see "Hold state" below).
- `hold_requests.py` — Hold-state tracking + four-layer reply-to-
  clarification matching. See "Hold state" below.
- `check_holds.py` — standalone scheduled-job script (run manually or
  intended as an Azure Container Apps Job — **not yet actually
  deployed as one**, see "Current status") that drafts follow-ups for
  Holds overdue past `FOLLOW_UP_WINDOW_MINUTES`. Opens its own
  connection, independent of app.py's per-action lifecycle.
- `generate_test_emails.py` / `run_batch_test.py` / `test_agent.py` /
  `test_azure_connection.py` — synthetic test data generation + manual
  test scripts. Each opens its own connection via
  `agent_engine[_azure].get_connection()`.
- `assets/` — Arbiter branding: `arbiter_icon_header.svg` (header mark
  — no `<title>`/`<desc>`/background rect, specifically to avoid a
  hover tooltip and an unwanted background box), `arbiter_icon_only.svg`
  (auth hero panel), `arbiter_logo_unified_mark.svg` (full lockup with
  baked-in wordmark, currently unused by code but kept),
  `arbiter_favicon.png` (browser tab icon).
- `Dockerfile` / `.dockerignore` — container build (Python 3.11 on
  Debian **bookworm**, not the default slim, + ODBC Driver 18)

## Current status

DONE:
- Core agent (3-tool loop, check/commit split, ambiguity handling)
- Local SQLite + Azure SQL dual backend, switchable
- Streamlit dashboard reorganized into 5 tabs (Process Email, Inbox,
  Outbound Queue, On Hold, History), Arbiter-branded (logo, favicon,
  wordmark), with a consistent design system across every tab —
  matching section headings, empty states, spacing, and native
  Streamlit chrome restyled to match (see "UI design system" below)
- Dockerized, deployed to Azure Container Apps — **the deployed
  container is several commits behind local as of this update (Hold
  state, outbound-email Reply-To fix, tabs, rebrand, and the UI polish
  pass all postdate the last known deploy) — rebuild and push before
  relying on the live demo reflecting current behavior**
- Authentication: signup, password + email-code login, sessions
- IMAP email ingestion via "Check Inbox" button, with persistent
  pending_emails/processed_emails tables (survives page refresh)
- Approval-gated outbound email sending — "Pending Outbound Emails" /
  "Sent Emails" in the Outbound Queue tab
- Hold state + four-layer reply-to-clarification matching
  (`hold_requests.py`) — fully built: Message-ID/In-Reply-To threading,
  a `HOLD-{id}` tag fallback (survives a client that strips headers),
  a unique-sender-email fallback, and a "Needs Manual Linking" UI for
  the genuinely ambiguous case (more than one open Hold for the same
  sender) — never guessed automatically. See "Hold state" below.
- Live per-step status updates while an email is processing
  (`st.status()`, e.g. "Checking order details..." →
  "Verifying feasibility...") instead of one generic spinner for the
  whole multi-tool-call loop
- One database connection per user action instead of one per function
  call — see "Connection lifecycle" below
- Caching (sidebar ERP snapshot TTL + per-session `get_order_details`
  cache, both invalidated on write) — built and tested

IN PROGRESS / NOT YET BUILT:
- `check_holds.py` is written and tested (`python3 check_holds.py`)
  but **not yet deployed as an actual scheduled Azure Container Apps
  Job** — currently only runs on demand, not on a schedule
- Interview-prep PDF may be stale given how much has changed since it
  was last generated (Hold state, outbound email, rebrand, tabs, UI
  polish) — ask the user if they want it regenerated

## Hold state (built)

When `request_clarification` fires AND an order ID was given: a
`hold_requests` row is created storing the FULL inbound email (not a
summary) + the exact clarifying question sent, status `'Awaiting
Reply'`. Surfaced in the On Hold tab's "Awaiting Customer Reply"
section.

A later reply is matched back to the right Hold, most-reliable signal
first, **never guessing**:

1. **Message-ID threading.** `email.utils.make_msgid()` generates the
   outgoing question's `Message-ID` *before* it's even sent (sending
   is gated behind human approval and may happen much later) — stored
   as `hold_requests.sent_message_id` at Hold-creation time. If a
   reply's `In-Reply-To`/`References` header matches, that's the
   match — can't be a coincidental false positive, since we generated
   the ID ourselves.
2. **`HOLD-{id}` tag** in the subject (`[HOLD-{id}]`) and body (an
   explicit "please keep this reference" ask). Survives a mail client
   that strips threading headers.
3. **Unique sender-email match** — only safe when *exactly one* open
   Hold belongs to that sender.
4. **Manual linking** — more than one open Hold for the same sender:
   queued in the On Hold tab's "Needs Manual Linking" section instead
   of guessed at; a human picks the right one (or marks it a genuinely
   new, unrelated request).

A matched reply combines the original request + the clarifying
question + the new reply into full context, runs back through
`run_agent()`, and the Hold is marked resolved. Known limitation
(deliberate scope choice, not an oversight): if that reply is itself
still ambiguous, the new question is just sent back as an ordinary
reply — the original Hold isn't re-opened or replaced with a new one
in this version.

After `FOLLOW_UP_WINDOW_MINUTES` (default 30, see Environment
variables) with no reply — checked by `check_holds.py`, see "Current
status" for its deployment state — a follow-up is drafted (still
approval-gated, never auto-sent) with HONEST wording: "this remains on
hold until we hear from you," never "we've gone ahead and processed
it," since there often isn't a safe original request to fall back on.
Marked `'Past Follow-Up'`, surfaced in "Needs Attention" for a human to
actively decide what to do. Nothing here EVER auto-processes, no
matter how much time passes.

## Connection lifecycle

Every DB-touching function across the project (`auth.py`,
`hold_requests.py`, `outbound_email.py`, `email_ingestion.py`'s DB
functions, `agent_engine[_azure].py`) takes an already-open `conn` as
its first argument — NOT a `get_connection` callable it opens and
closes itself. This was a deliberate refactor: a single user action
used to chain through 5+ functions, each opening (and over Azure SQL,
paying the handshake cost of) its own connection. Callers now open one
connection per action — `with closing(auth_get_connection()) as conn:`
— and thread it through the whole chain, including into `run_agent()`'s
internal Claude tool-use loop.

**The two calling conventions are deliberately NOT both supported.**
Passing the old connection-opening callable where a function now
expects an open connection fails immediately and loudly
(`AttributeError: 'function' object has no attribute 'cursor'`)
instead of silently working — so a missed call site can't hide as a
quiet bug. If you add a new DB-touching function, follow this
convention: take `conn`, don't open/close it yourself.

Standalone scripts (`check_holds.py`, `test_agent.py`,
`run_batch_test.py`, `test_azure_connection.py`) aren't part of
app.py's per-request lifecycle — each opens and closes its own single
connection via `agent_engine[_azure].get_connection()`.

## UI design system

`theme.py` defines the ONE style for each recurring role — don't
reach for `st.header`/`st.subheader`/bold markdown ad hoc for a new
section, use the shared helpers instead:

- `section_heading_html(text)` — every top-level named section inside
  a tab (e.g. "Agent Execution", "Pending Outbound Emails"). No emoji
  — status is already communicated by `.stamp` badges.
- `field_label_html(text)` — a smaller, dimmer label for a sub-field
  within ONE result (e.g. "Tool Call Chain"/"Outcome"/"Reply to
  Customer" are sub-fields of one Agent Execution result, not
  independent sections).
- `empty_state_html(text)` — a designed "nothing here" box (dashed
  border) for any list/section that can be empty, instead of a bare
  `st.caption()`.
- Spacing: use the `--space-xs` through `--space-xl` custom properties
  already defined in `:root`, not new hardcoded pixel values.

**Restyling native Streamlit chrome requires live DOM inspection, not
guessing selectors from memory or from Streamlit's Python source** —
the actual class/testid structure is generated by the (minified,
frequently-changed) frontend bundle. Two confirmed gotchas from doing
this the hard way:
- `st.info`/`st.warning`/`st.error`/`st.success`: the kind-specific
  tint (blue for info, etc.) lives on a **nested** `[data-testid=
  "stAlertContainer"]` div with its own translucent background, NOT on
  the outer `[data-testid="stAlert"]` — overriding only the outer
  container leaves the inner tint visible as a mismatched color patch.
- `st.tabs()`: the active tab's color is Streamlit's own red
  `primaryColor` default. The tab label itself is
  `[data-testid="stTab"]` (with `aria-selected="true"` when active);
  the moving underline is a **sibling** `.react-aria-SelectionIndicator`,
  not a border on the tab.

To find the real selector for anything else: launch the app for real
(see "Local dev / screenshot verification" below) and inspect the live
DOM via a Playwright `page.evaluate()` — don't guess.

## Local dev / screenshot verification

No project-specific `run` skill exists yet for this app (consider
`/run-skill-generator` if this workflow recurs). To visually verify a
UI change:

1. Launch: `streamlit run app.py --server.port <port> --server.headless true`
   (unset `USE_AZURE_DB` first for local SQLite).
2. `chromium-cli` is not installed in this environment. Fallback: `npm
   install playwright` (just the package, skip `npx playwright
   install` — no need to download Chromium) and drive the machine's
   existing Chrome/Edge via `chromium.launch({ executablePath:
   'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' })`.
3. Auth: the app is cookie-gated. Seed a real user + session directly
   in `mock_erp.db` (`auth.sign_up()` + an `INSERT INTO sessions`),
   then `context.addCookies([{name: 'session_token', value: TOKEN,
   domain: 'localhost', path: '/'}])` before navigating — reproduces a
   real returning session without driving the actual login-code email
   flow.
4. **Wait for a dashboard-specific selector, not just page text.** The
   pre-login loading states (cookie-probe, schema-init retry) render
   the SAME "Arbiter" text as the real header before the real
   dashboard ever mounts — `page.waitForSelector('text=Arbiter')`
   alone can catch the loading screen. Wait for `.app-header-logo-row`
   (or another element that only exists post-login) instead.
5. `AppTest` (Streamlit's own test harness) has the equivalent gotcha:
   it can't simulate the browser cookie component at all (always
   returns its `default`), so a fresh `AppTest` run gets stuck in the
   cookie-probe retry loop and never reaches the real logged-out auth
   screen. Seed `at.session_state["just_logged_out"] = True` before
   `.run()` to skip cookie probing and reach `_render_auth_screen()`
   directly.
6. Streamlit's `st.dataframe` renders via canvas (glide-data-grid) —
   not reachable by injected CSS at all, only by Streamlit's own
   `[theme]` config (`.streamlit/config.toml`). Known, accepted
   limitation — every dataframe in the app (sidebar, History,
   Outbound Queue) consistently shows the same unthemed white-table
   look; fixing it would mean adopting Streamlit's native theme system
   instead of (or alongside) the current CSS-injection approach, a
   much bigger change than a CSS pass.

## Environment variables

- `ANTHROPIC_API_KEY`
- `USE_AZURE_DB` (true/false — defaults to false/local SQLite)
- `AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`, `AZURE_SQL_USERNAME`,
  `AZURE_SQL_PASSWORD`
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (Gmail App Password, requires
  2FA on the Gmail account — used for auth codes, ingestion, AND
  outbound email, all via the same account)
- `IMAP_LABEL` (defaults to "OrderRequests")
- `ORDER_INTAKE_EMAIL` (optional — order-related outbound emails'
  Reply-To; defaults to `<GMAIL_ADDRESS local-part>+orders@<domain>`,
  matching the plus-addressed Gmail filter that routes replies into
  `IMAP_LABEL`)
- `FOLLOW_UP_WINDOW_MINUTES` (optional, defaults to 30 — how long a
  Hold waits with no reply before `check_holds.py` drafts a follow-up)

Local dev: set via `setx` (Windows) + reopen terminal, or a `.env`
file (already in `.gitignore`) for Docker's `--env-file`.

## Deployment sequence (manual, every time)

```
docker build -t reconciliation-agent .
az acr login --name ca289d1c4e31acr
docker tag reconciliation-agent ca289d1c4e31acr.azurecr.io/reconciliation-agent:latest
docker push ca289d1c4e31acr.azurecr.io/reconciliation-agent:latest
az containerapp update --name reconciliation-agent --resource-group reconciliation-agent-rg --image ca289d1c4e31acr.azurecr.io/reconciliation-agent:latest
```

(Container/registry/resource-group names still use the old
`reconciliation-agent` naming — these are live Azure resource
identifiers, not display branding, and haven't been renamed as part of
the Arbiter rebrand. Don't rename them without deliberately planning
the Azure-side migration; it's out of scope for a code change.)

Resource group: `reconciliation-agent-rg`. Region: `swedencentral`
(Container App) — Azure SQL server is in `germanywestcentral`. Both
work fine cross-region for this project's traffic level.

Azure for Students subscription is restricted to a specific region
allowlist (`swedencentral`, `italynorth`, `germanywestcentral`,
`spaincentral`, `switzerlandnorth`) — check via the subscription's
policy assignment if a new resource gets a region-disallowed error.

`az containerapp up --source .` does NOT work on this subscription —
ACR Tasks (remote build) is restricted. Must build locally and push a
pre-built image instead (the sequence above).

Cold starts: Container App scales to zero on idle (free). Use
`az containerapp update ... --min-replicas 1` temporarily before a
demo/interview, then `--min-replicas 0` after — leaving it at 1
permanently costs roughly $13/month.

## Known gotchas (don't rediscover these)

1. **Debian `apt-key` is deprecated/removed on newer releases.** Use
   `gpg --dearmor` + `signed-by=` in the apt source line instead.
2. **Base image must be `python:3.11-slim-bookworm`, not plain
   `python:3.11-slim`** — the latter silently resolves to a newer
   Debian release Microsoft's ODBC repo doesn't support.
3. **Need `ca-certificates` installed** in the Dockerfile, or TLS
   handshakes to Azure SQL can hang despite working DNS/TCP.
4. **The Linux ODBC driver needs `UID=username@server-shortname`**,
   not just `username` — unlike the Windows driver, which appends
   this automatically. This caused a long-running "Login timeout"
   bug that looked identical across many different attempted fixes
   (DNS, TCP, TLS certs, MTU were all ruled out first — the real bugs
   were this login format issue AND separately a `.env` file with
   trailing commas corrupting every credential value).
5. **`.env` files: no trailing commas.** If given credentials as a
   comma-separated list in prose, do NOT carry the commas into
   separate `.env` lines.
6. **IMAP fetch must use `BODY.PEEK[]`, not `RFC822`** — the latter
   has a side effect of marking a message as read just by fetching
   it, which silently "consumes" unread emails even if they're never
   actually processed.
7. **Streamlit + cookie-based sessions (`extra-streamlit-components`
   CookieManager):** `.get(name)` can't distinguish "component still
   loading" from "genuinely no cookie" — both return `None`. Must use
   `.get_all()` and check for `None` (still loading → `st.stop()`,
   show a loading state) vs. an actual dict (loaded → check for the
   key). Getting this wrong causes false logouts on page refresh.
8. **Wrapping `st.columns()` inside a manually-written `<div>` via
   `st.markdown()` does NOT actually nest them** in the real DOM —
   causes broken/collapsed layouts. Style Streamlit's own generated
   container elements via CSS selectors instead of hand-written
   wrapper divs.
9. **PowerShell won't run scripts from the current directory** without
   `.\` prefix, OR just use `python script.py` instead — more
   reliable than relying on execution policy.
10. **`setx` (env vars) and new installs don't apply to already-open
    terminals** — always close and reopen ALL terminals + VS Code
    after `setx` or installing something that adds a new command.
11. **SQL dialect: `LIMIT 1` (SQLite) has no equivalent syntax in
    T-SQL** — Azure SQL needs `SELECT TOP 1 ...` instead, placed right
    after `SELECT`, not at the end of the query. Branch on an
    `is_azure` parameter wherever a query needs "most recent row."
12. **pyodbc auto-converts `DATETIME2` columns to native `datetime`
    objects, not strings** — unlike SQLite, which returns exactly what
    was stored (`.isoformat()` text). `datetime.fromisoformat()` alone
    raises `TypeError` against a real `datetime` object. Every module
    with a `DATETIME2` column has its own small `_parse_datetime()`
    helper that handles both shapes (str-or-datetime, normalizes to
    UTC-aware) — duplicated per module rather than shared, matching
    this codebase's convention for small dual-backend helpers.
13. **`CREATE TABLE IF NOT EXISTS` (SQLite) / `IF OBJECT_ID(...) IS
    NULL CREATE TABLE` (Azure) are no-ops once a table already exists
    from an earlier deploy** — a column added only to the schema
    string never reaches a database that was set up before the
    change. Every module with a schema needing a later column addition
    has an `_ensure_column(cursor, is_azure, table, column,
    sqlite_type, azure_type)` migration helper, called after the
    CREATE TABLE step in its `init_*_schema()` function.
14. **`st.rerun()` called from inside a script is purely server-side —
    it does NOT wait on any round-trip to the browser.** Matters for
    anything that just mounted a component needing real browser-side
    work (the cookie-write custom component) or that just changed
    visible state a human needs a moment to register — needs a real
    `time.sleep()` before the rerun, not an immediate one.
15. **Streamlit native chrome needs live DOM inspection to restyle
    correctly, not guessed selectors** — see "UI design system" above
    for the two confirmed cases (alert box nested tint, tab active
    color/indicator).
16. **`AppTest` can't simulate the browser cookie component** — see
    "Local dev / screenshot verification" above for the
    `just_logged_out=True` seed needed to reach the real auth screen
    in a test.

## Branding

Rebranded from "Reconciliation Agent" to **Arbiter** across the UI
(page title/favicon, header logo+wordmark, auth hero panel, connection
badge), the outbound-email sender display name, the login-code email
subject, and every module docstring. `assets/` holds the logo/icon/
favicon files (see File map). "Reconciliation Agent" deliberately
still appears as a descriptive role — the Claude system prompt's own
self-identity ("Arbiter, the ... Reconciliation Agent"), the README's
subtitle, and Azure resource names (not renamed — see Deployment) — so
don't "finish the job" by scrubbing those too; they're intentional.

## Portfolio/interview context

Not built for or with a real company. User is a postgrad MSc Data
Science student building this to demonstrate skills in job interviews.
An interview-prep PDF exists covering architecture reasoning and
anticipated questions — likely stale now given how much has changed
(Hold state, outbound email, rebrand, tabs, UI polish) — ask the user
if they want it regenerated.
