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