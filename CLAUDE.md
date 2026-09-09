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
```

Read the `docker push` output for the final line: `latest: digest: sha256:<digest>`.
Then deploy pinned to that exact digest, NOT to `:latest`:

```
az containerapp update --name reconciliation-agent --resource-group reconciliation-agent-rg --image "ca289d1c4e31acr.azurecr.io/reconciliation-agent@sha256:<digest>"
```

See "Deployment gotchas" below for why the digest-pin step is not optional.

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

## Deployment gotchas (read before debugging a "my fix isn't showing up" mystery)

Lost the better part of a session chasing what looked like a live runtime
bug (a button rendering Streamlit's default red instead of the theme's
amber, on a fix that was independently confirmed correct in local
source, correct inside the built Docker image, and had already been
through a full `docker push` + `az containerapp update` cycle that
reported success) before finding that none of that actually mattered --
the running container had never restarted. Read this section FIRST next
time a fix that checks out everywhere else still isn't visible live;
don't re-derive it.

**Never deploy using the mutable `:latest` tag alone.**
`az containerapp update --image ...:latest` does NOT reliably restart a
running replica just because `:latest` now points to different content
in the registry. Confirmed directly: `docker push` succeeded and
reported a genuinely new digest, `az containerapp update` reported
`"provisioningState": "Succeeded"` and bumped the Container App
resource's `systemData.lastModifiedAt` to the current time -- and NONE
of that meant the running container had actually changed.
`az containerapp replica list ... --query
"[0].properties.containers[0].runningStateDetails"` showed the exact
same "Container started at ..." timestamp from a deploy made two days
earlier, `restartCount: 0` -- the same OS process, never touched, the
whole time. Azure Container Apps appears to only trigger a real
revision/restart when the image *reference string itself* changes;
since `:latest` is always the same string, repeated deploys against it
can silently leave the old image running indefinitely, with every
individual step along the way reporting success. This is why the
"Deployment sequence" above pins to the resolved digest instead of
`:latest` -- a digest is a different string on every push, so Container
Apps has no way to mistake it for "no change." Treat that pin step as
mandatory, not an occasional workaround.

**`docker push` can fail with an ACR auth error while still leaving you
free to run `az containerapp update` right afterward.** ACR login
tokens (`az acr login`) expire after a few hours, and a failed push
doesn't stop the next command in a script/session from running anyway.
That subsequent `containerapp update` will still report "succeeded" --
it just redeploys whatever digest was already sitting in the registry,
not your new build, and gives no error indicating that's what happened.
Always read the actual `docker push` output and confirm it shows layers
uploading and ends with a `latest: digest: sha256:...` line, not an
authentication error, before trusting anything that runs after it. If
it's been a while since the last login, just re-run `az acr login` --
harmless if already logged in.

**When "my fix isn't showing up on the live site" happens, verify in
this exact order** -- each step is a hard, unambiguous check against
real evidence, not a visual glance at the page or a re-read of the
source:
1. Confirm the fix is actually in the local source file (a plain read
   or grep).
2. Confirm it's inside the actual **built Docker image**, not just the
   source tree Docker was pointed at:
   `docker run --rm --entrypoint sh <image> -c "grep -n '<text>'
   <path-inside-image>"`
3. Confirm the Container App is **actually configured to run that exact
   digest**:
   `az containerapp show --name reconciliation-agent --resource-group
   reconciliation-agent-rg --query
   "properties.template.containers[0].image"`
   -- compare against the digest reported by your last successful
   `docker push` (or resolve what `:latest` currently points to in the
   registry: `az acr repository show --name ca289d1c4e31acr --image
   reconciliation-agent:latest --query digest`). If steps 1 and 2 both
   check out but the live site still shows old behavior, the answer is
   almost certainly here (or in the replica's actual
   `runningStateDetails` start time, per the first gotcha above) --
   not a mystery in the application code. Don't go looking for a
   runtime bug in `build_css()`, `st.markdown()`, or anything else
   downstream until this step has actually been checked.

## Multi-replica gotchas (Streamlit's session model vs. autoscaling)

Spent a long session chasing a post-login reload bug that "confirmed
locally" not once but twice, and both times the fix genuinely worked
locally and still failed live. The actual root causes were never
reachable from local testing at all — they only exist once the app is
running as more than one replica, which local dev (always exactly one
process) can never reproduce. Read this before trusting any "confirmed
locally" claim for session/login/cookie-adjacent behavior in this app,
and before spending another session rediscovering any of the three
lessons below.

**Session affinity is not optional for this app.** Streamlit's
`st.session_state` lives entirely in ONE replica's process memory, tied
to a specific script-execution session. Azure Container Apps'
`stickySessions` ingress setting defaults to unset (`null`) --
functionally "none" -- meaning a fresh connection, or a reconnect, can
land on ANY currently-running replica with no guarantee of hitting the
one that has this user's actual session_state. Under real autoscaling
(confirmed directly: a short burst of test traffic scaled this app from
1-2 replicas to 8-9), that's not a rare edge case, it's routine. Fixed
with:
```
az containerapp ingress sticky-sessions set --name reconciliation-agent --resource-group reconciliation-agent-rg --affinity sticky
```
Verify it actually took: `az containerapp show --name reconciliation-agent
--resource-group reconciliation-agent-rg --query
"properties.configuration.ingress.stickySessions"` should show
`{"affinity": "sticky"}`, not `null`. This has to be treated as a
required part of this app's deployment, not a one-time fix -- if the
Container App or its ingress config is ever recreated, check this
again before assuming session/login bugs are a code problem.

**`az containerapp logs show` only ever shows ONE replica.** Its own
`--help` text says so directly ("logs are only taken from one revision,
replica, and container"), but it's easy to miss and the symptom looks
exactly like "no logs at all" -- confirmed directly: repeated
`az containerapp logs show --tail 300` calls came back with zero
matches for print statements that WERE executing, simply because the
specific replica that handled that request wasn't the one the command
happened to pick. For real visibility across every replica (which
autoscaling makes the normal case, not the exception), query Log
Analytics directly instead:
```
az extension add --name log-analytics --yes
az monitor log-analytics query --workspace <workspace-customerId> \
  --analytics-query "ContainerAppConsoleLogs_CL | where Log_s contains '<search term>' | order by TimeGenerated asc | project TimeGenerated, ContainerGroupName_s, Log_s"
```
The workspace's `customerId` (GUID) comes from `az containerapp env show
--name <env> --resource-group reconciliation-agent-rg --query
"properties.appLogsConfiguration.logAnalyticsConfiguration.customerId"`.
`ContainerGroupName_s` is the replica name -- keep it in the projection,
it's exactly what makes cross-replica issues (like the one above)
visible in the first place, since you can literally see the same login
flow's log lines split across several different replica names.

**Don't guess a timing/retry budget for anything that crosses a real
network hop -- measure it from real production logs, then size the
budget from that data.** A cookie-readback confirmation loop tuned on
local testing (8 attempts, 0.35s apart, ~3s total) looked completely
solved locally, every single time. Live, real `az monitor
log-analytics query` output showed it repeatedly exhausting that entire
budget with the probe never once reporting ready in that window -- not
"just barely too slow", genuinely not enough time under real cloud
network/current-load conditions for the very first component mount to
finish. There was no way to derive the right number analytically; it
came directly from watching real timestamps in real logs across several
live attempts and sizing the new budget (15 attempts, 0.6s apart, ~9s
total) to comfortably cover what was actually observed, then confirming
via the SAME log query that the widened budget brought the "gave up"
warning down to zero across a fresh batch of live attempts. The
general principle: local/loopback timing is not a valid stand-in for
real inter-service latency for anything that has to survive a genuine
network round-trip in production.

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
