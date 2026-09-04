"""
Standalone schema-initialization script for Arbiter.

Runs once, at container startup (see entrypoint.sh), before Streamlit
ever starts accepting traffic -- creates every table this app needs
(auth/session tables, inbox-tracking tables, the outbound-email
approval queue, hold-requests) if they don't already exist.

This used to run inside app.py instead (_ensure_*_schema(), gated
behind @st.cache_resource so it only ran once per process) -- but
"once per process" still meant it ran as a side effect of whichever
user's page load happened to be the first request after a cold
container start, making THAT user pay for a multi-second "Connecting..."
wait (or, before a retry loop was added, a raw exception) for work that
has nothing to do with their own request. Moving it here makes schema
setup a real precondition of the container instead: entrypoint.sh runs
this script BEFORE starting Streamlit at all, so the HTTP port never
opens until the database is confirmed ready -- no request, from any
user, at any time, can race an uninitialized database again.

Run with: python3 init_db.py
Exits 0 on success. Exits non-zero (via an uncaught exception) on
failure -- deliberately NOT caught/swallowed here, so entrypoint.sh's
`set -e` stops the container from starting Streamlit against a database
that was never actually confirmed ready.
"""
import os
import sqlite3
import time
from contextlib import closing

import auth
import email_ingestion
import hold_requests
import outbound_email

USE_AZURE_DB = os.environ.get("USE_AZURE_DB") == "true"

if USE_AZURE_DB:
    # Raises immediately (KeyError on a missing AZURE_SQL_* env var, or
    # whatever agent_engine_azure's own import-time errors are) if the
    # environment isn't correctly configured -- that's the fail-loudly
    # behavior we want here, so left uncaught.
    from agent_engine_azure import get_connection as _get_connection
else:
    def _get_connection():
        return sqlite3.connect("mock_erp.db")

# Same retry shape used throughout the rest of this project's Azure SQL
# startup paths (e.g. the sidebar's get_db_snapshot()) -- a cold Azure
# SQL Database can take a few seconds to accept its first connection;
# this absorbs that instead of failing the whole container boot on a
# single transient timeout.
CONNECT_MAX_ATTEMPTS = 5
CONNECT_RETRY_DELAY_S = 1.5


def _connect_with_retry():
    last_error = None
    for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
        try:
            return _get_connection()
        except Exception as e:
            last_error = e
            if attempt < CONNECT_MAX_ATTEMPTS:
                print(
                    f"[init_db] connection attempt {attempt}/{CONNECT_MAX_ATTEMPTS} "
                    f"failed ({e}); retrying in {CONNECT_RETRY_DELAY_S}s...",
                    flush=True,
                )
                time.sleep(CONNECT_RETRY_DELAY_S)
    raise last_error


def main():
    backend = "Azure SQL" if USE_AZURE_DB else "local SQLite"
    print(f"[init_db] Initializing schema against {backend}...", flush=True)

    with closing(_connect_with_retry()) as conn:
        auth.init_auth_schema(conn, is_azure=USE_AZURE_DB)
    print("[init_db] auth schema OK", flush=True)

    with closing(_connect_with_retry()) as conn:
        email_ingestion.init_email_tracking_schema(conn, is_azure=USE_AZURE_DB)
    print("[init_db] email tracking schema OK", flush=True)

    with closing(_connect_with_retry()) as conn:
        outbound_email.init_outbound_email_schema(conn, is_azure=USE_AZURE_DB)
    print("[init_db] outbound email schema OK", flush=True)

    with closing(_connect_with_retry()) as conn:
        hold_requests.init_hold_requests_schema(conn, is_azure=USE_AZURE_DB)
    print("[init_db] hold requests schema OK", flush=True)

    print("[init_db] Schema initialization complete.", flush=True)


if __name__ == "__main__":
    main()
