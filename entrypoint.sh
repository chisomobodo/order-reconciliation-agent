#!/bin/sh
# Container entrypoint: initialize the database schema BEFORE Streamlit
# ever starts accepting HTTP traffic, so no user request can race an
# uninitialized database. See init_db.py for why this moved out of the
# Streamlit request cycle.
#
# `set -e` is what makes this fail loudly: if init_db.py exits non-zero
# for any reason (bad Azure SQL credentials, unreachable server, etc.),
# this script stops immediately on that line and never reaches `exec
# streamlit run` below -- the container fails to start rather than
# silently serving an app backed by an uninitialized database.
set -e

echo "[entrypoint] Running database schema initialization..."
python3 init_db.py

echo "[entrypoint] Schema initialization complete. Starting Streamlit..."
# exec replaces this shell process with Streamlit's, instead of running
# it as a child -- so Streamlit becomes PID 1 and receives signals
# (SIGTERM on container stop/scale-down) directly, allowing a clean
# shutdown, rather than the shell swallowing them.
exec streamlit run app.py --server.port=8501 --server.address=0.0.0.0
