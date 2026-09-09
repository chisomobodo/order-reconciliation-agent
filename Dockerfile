# Dockerfile for the Reconciliation Agent Streamlit app.
# Builds a container that runs app.py, connecting to Azure SQL Database
# via environment variables (never baked into the image).

FROM python:3.11-slim-bookworm

# Install the ODBC driver + build tools needed for pyodbc to talk to
# Azure SQL Database, plus curl/gnupg for the Microsoft package repo,
# and ca-certificates -- without it, TLS certificate validation during
# the encrypted connection handshake (Encrypt=yes) can stall and time
# out even though raw TCP connectivity works fine.
# Uses the modern gpg-keyring + signed-by approach instead of the
# deprecated `apt-key`, which newer Debian releases (trixie+) removed.
# The apt source line is written directly rather than via sed against
# Microsoft's prod.list, since that produced a malformed entry on this
# base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    unixodbc \
    unixodbc-dev \
    && update-ca-certificates \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,armhf,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# entrypoint.sh is written with LF line endings and needs the execute
# bit set explicitly -- NTFS (where this repo is developed) has no
# concept of a Unix execute permission, so this can't be relied on to
# already be set just because the file was committed that way.
RUN chmod +x entrypoint.sh

# --- Dark-flash fix: patch Streamlit's own served index.html ---
# .streamlit/config.toml (native theme) fixes the LONG, script-dependent
# white flash, but not all of it: Streamlit's compiled frontend bundle
# mounts React with its OWN built-in default (light) theme immediately
# on load -- before the browser has received the custom dark theme at
# all, which only ever arrives later, over the WebSocket session-
# bootstrap frames. That round trip is fast on loopback (~100-150ms,
# imperceptible) but measured directly against the live production URL
# (network + WebSocket tracing, not assumption) to take far longer over
# a real cross-region connection: background went white at t=1825ms,
# right after the page's own `load` event, and only turned dark at
# t=2190ms, after the first substantive WS frame -- a real, visible
# ~365ms flash that config.toml alone cannot prevent, because config.toml
# is never delivered any other way.
#
# This inlines the dark background directly into index.html's own
# <head>, so the browser paints dark from the very first byte of HTML --
# no JS execution and no WebSocket round trip required. That closes the
# ~365ms bundle-mount-to-theme-delivery gap; what's left after this is
# only the genuinely unavoidable time-to-first-byte sliver before ANY
# response has arrived at all, which no app-side or theme configuration
# can act on (confirmed: every background-color sample taken during that
# pre-response window showed no document yet, not "white" -- there is
# nothing to paint, by construction, until the first response byte
# lands).
#
# FRAGILE ON PURPOSE, not overlooked: this depends on Streamlit's
# index.html still opening with a literal `<head>` tag, which is why
# requirements.txt now pins streamlit==1.63.0 instead of >=1.38.0 --
# an unpinned build could silently pick up a newer Streamlit release
# whose generated index.html changed shape, and the sed below would
# then just as silently stop matching. The grep -q right after the sed
# turns that into a loud BUILD FAILURE instead: if you deliberately
# bump the pinned Streamlit version, this step will tell you immediately
# whether the patch still applies, rather than quietly shipping the
# flash again with no signal that anything broke.
RUN STREAMLIT_STATIC_DIR="$(python -c 'import streamlit, os; print(os.path.join(os.path.dirname(streamlit.__file__), "static"))')" \
    && test -f "$STREAMLIT_STATIC_DIR/index.html" \
    && grep -q '<head>' "$STREAMLIT_STATIC_DIR/index.html" \
    && sed -i 's|<head>|<head>\n    <style>html,body{background:#14181C}</style>|' "$STREAMLIT_STATIC_DIR/index.html" \
    && grep -q 'background:#14181C' "$STREAMLIT_STATIC_DIR/index.html"

# This container is always the Azure-backed version -- no point running
# SQLite inside a stateless container that gets replaced on every deploy.
ENV USE_AZURE_DB=true

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# entrypoint.sh runs init_db.py (schema setup) to completion BEFORE
# starting Streamlit -- see entrypoint.sh and init_db.py for why this
# moved out of the Streamlit request cycle. If schema init fails, the
# container fails to start instead of serving an uninitialized app.
ENTRYPOINT ["./entrypoint.sh"]