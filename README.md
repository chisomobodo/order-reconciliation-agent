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

## Limitations & Production Considerations

This is a prototype, not a production system:

- No real email ingestion pipeline — emails are pasted or selected in the UI, not received via IMAP/webhook
- No real ERP integration — the "ERP" is a mock schema seeded with invented data
- Product catalog is a hardcoded SKU list in `agent_config.py`'s tool schema, not a real product-catalog lookup
