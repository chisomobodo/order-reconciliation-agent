# Reconciliation Agent — Project Memory

Read this file fully before making changes. It captures architecture,
design decisions (and WHY), current status, and hard-won gotchas from
past debugging sessions, so they don't get rediscovered from scratch.

## What this is

An AI agent that parses messy wholesale order-change emails for a
**fictional** beverage distributor, checks stock/dispatch status
against a database, and either reconciles the order or asks for
clarification. Built with Claude Sonnet 5, Python, Streamlit. No real
company data or systems — the distributor and products are invented.

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
a confidence threshold: any genuine ambiguity → ask, never guess.

**Mandatory order lookup:** `get_order_details` is called for EVERY
email with an order ID, no exceptions — this was made unconditional
after testing showed Claude inconsistently skipping it when it judged
an ambiguity as "self-contained," causing inconsistent behavior
between similar emails.

## File map

- `agent_config.py` — system prompt + 3 tool definitions
- `agent_engine.py` — orchestration loop, SQLite backend
- `agent_engine_azure.py` — same logic, Azure SQL backend (pyodbc).
  **IMPORTANT:** the login format must be `username@server-shortname`
  for the Linux ODBC driver, NOT just `username` — see Gotchas below.
- `app.py` — Streamlit dashboard. Switches SQLite/Azure via
  `USE_AZURE_DB` env var. Now also gates the whole app behind login
  (see auth.py).
- `theme.py` — visual design system (manifest/stamp aesthetic, dark +
  light tokens, animations)
- `setup_db.py` / `setup_db_azure.py` — schema + seed data (5 SKUs,
  3 orders, deliberately ambiguous: 2 keg sizes, 2 cider variants)
- `auth.py` — signup, password + email-code login, sessions with a
  20-min sliding inactivity timeout. bcrypt hashing, never plaintext.
- `auth_theme.py` — login/signup screen visual design (split hero +
  form layout, matches theme.py palette)
- `email_ingestion.py` — real IMAP fetch via Gmail, scoped to the
  `OrderRequests` label (matched via plus-addressed intake:
  `GMAIL_ADDRESS+orders@gmail.com`, filtered by a Gmail rule). Uses
  `BODY.PEEK[]` not `RFC822` — see Gotchas.
- `outbound_email.py` — approval-gated email sending queue. Extends
  the check/commit principle to outbound email.
- `generate_test_emails.py` / `run_batch_test.py` — synthetic test
  data generation + batch testing
- `Dockerfile` / `.dockerignore` — container build (Python 3.11 on
  Debian **bookworm**, not the default slim, + ODBC Driver 18)

## Current status

DONE:
- Core agent (3-tool loop, check/commit split, ambiguity handling)
- Local SQLite + Azure SQL dual backend, switchable
- Streamlit dashboard, manifest/stamp visual theme
- Dockerized, deployed to Azure Container Apps
- Authentication: signup, password + email-code login, sessions
- IMAP email ingestion via "Check Inbox" button, with persistent
  pending_emails/processed_emails tables (survives page refresh)
- Approval-gated outbound email sending — outbound_email.py fully
  wired into app.py: "Pending Outbound Emails" section, Approve & Send,
  Reject, and the "Sent Emails" audit log are all built and tested
  working

IN PROGRESS / NOT YET BUILT:
- Hold state + `hold_requests` table for clarification-needing orders
  (design finalized, not yet implemented — see "Hold state design"
  below)
- Scheduled follow-up job (`check_holds.py` as an Azure Container
  Apps Job) — sends a follow-up after 20 min of no customer reply,
  flags as "Past Follow-Up" for human attention. Does NOT auto-process
  anything after the deadline — that would violate the no-guessing
  principle. This needs a genuinely separate scheduled Container Apps
  Job resource, since a background thread inside the web container
  stops existing whenever Container Apps scales to zero.
- Caching (st.cache_data for sidebar ERP snapshot with TTL + explicit
  clear-on-commit; in-memory per-session cache for get_order_details
  with invalidation on write) — prompted but not confirmed built/
  tested as of last update

## Hold state design (for when this gets built)

When `request_clarification` fires AND an order ID was given: create a
`hold_requests` row storing the FULL inbound email (not a summary) +
the exact clarifying question sent. Status: 'Awaiting Reply'.

After 20 min with no reply (checked by the scheduled job): draft a
follow-up email via `outbound_email.queue_draft()` (still
approval-gated, not auto-sent) with HONEST wording — "this will remain
on hold until we hear from you" — NOT "we will process your original
order automatically," since there often isn't a safe original request
to fall back on. Mark status 'Past Follow-Up', surface prominently in
the dashboard (e.g. red/urgent stamp) for a human to actively decide
what to do. Nothing auto-processes, ever, regardless of how much time
passes. If no order ID was given at all, there's nothing to place on
hold — only the clarification email itself matters.

## Environment variables

- `ANTHROPIC_API_KEY`
- `USE_AZURE_DB` (true/false — defaults to false/local SQLite)
- `AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`, `AZURE_SQL_USERNAME`,
  `AZURE_SQL_PASSWORD`
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (Gmail App Password, requires
  2FA on the Gmail account — used for auth codes, ingestion, AND
  outbound email, all via the same account)
- `IMAP_LABEL` (defaults to "OrderRequests")

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

## Portfolio/interview context

Not built for or with a real company. User is a postgrad MSc Data
Science student building this to demonstrate skills in job interviews.
An interview-prep PDF exists covering architecture reasoning and
anticipated questions — ask the user if they want it regenerated after
major changes (it may go stale as new features like Hold state /
outbound email / scheduled jobs get added).