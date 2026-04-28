FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# Base system deps (includes PostgreSQL server, not just client)
RUN apt-get update && apt-get install -y \
    curl wget git ca-certificates gnupg \
    python3 python3-pip rsync \
    postgresql postgresql-client \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libatspi2.0-0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libxcb1 \
    libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Node.js 22
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@latest

# Python venv with ORS + task dependencies
RUN uv venv /opt/venv --python 3.12 && uv pip install --python /opt/venv/bin/python \
    "openreward>=0.1.95" \
    "mcp>=1.0.0" \
    "pydantic>=2.0" \
    psycopg2-binary \
    openpyxl \
    python-docx \
    python-pptx \
    pyyaml \
    aiofiles \
    termcolor \
    psutil \
    arxiv \
    bibtexparser \
    canvasapi \
    prompt_toolkit

ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV="/opt/venv"
ENV LOCAL_SERVERS_PATH=/opt/local_servers

# Install Playwright browser
RUN playwright install chromium || true

# Copy and build MCP servers
COPY local_servers/ /opt/local_servers/

# Build Node.js MCP servers (parallel)
RUN for dir in \
        /opt/local_servers/12306-mcp \
        /opt/local_servers/Calendar-Autoauth-MCP-Server \
        /opt/local_servers/filesystem \
        /opt/local_servers/google-forms-mcp \
        /opt/local_servers/HowToCook-mcp \
        /opt/local_servers/mcp-canvas-lms \
        /opt/local_servers/mcp-npx-fetch \
        /opt/local_servers/playwright-mcp \
        /opt/local_servers/servers \
        /opt/local_servers/youtube-mcp-server; do \
    ( [ -f "$dir/package.json" ] && \
        echo "=== npm: $dir ===" && cd "$dir" && npm install && (npm run build 2>/dev/null || true) ) & \
done && wait

# These servers need pg for their Toolathlon PG-backed forks
RUN cd /opt/local_servers/woocommerce-mcp && npm install pg @types/pg && npm run build
RUN cd /opt/local_servers/notion-mcp-server && npm install pg @types/pg && npm run build

# Build Python MCP servers (parallel)
RUN for dir in \
        /opt/local_servers/arxiv-mcp-server \
        /opt/local_servers/arxiv-latex-mcp \
        /opt/local_servers/yahoo-finance-mcp \
        /opt/local_servers/emails-mcp \
        /opt/local_servers/mcp-google-sheets \
        /opt/local_servers/mcp-snowflake-server \
        /opt/local_servers/mcp-scholarly \
        /opt/local_servers/Office-Word-MCP-Server \
        /opt/local_servers/Office-PowerPoint-MCP-Server \
        /opt/local_servers/excel-mcp-server \
        /opt/local_servers/pdf-tools-mcp \
        /opt/local_servers/mcp-youtube-transcript \
        /opt/local_servers/cli-mcp-server; do \
    ( [ -f "$dir/pyproject.toml" ] && \
        echo "=== uv: $dir ===" && cd "$dir" && uv sync 2>/dev/null || true ) & \
done && wait

# yahoo-finance Toolathlon fork uses psycopg2 for PG-backed data
RUN cd /opt/local_servers/yahoo-finance-mcp && uv add psycopg2-binary

# Setup PostgreSQL: create user/database and allow trust auth for local connections
USER postgres
RUN service postgresql start && \
    psql -c "CREATE USER eigent WITH PASSWORD 'camel' SUPERUSER;" && \
    psql -c "CREATE DATABASE toolathlon_gym OWNER eigent;" && \
    psql -c "CREATE DATABASE toolathlon OWNER eigent;" && \
    service postgresql stop
# Allow all local connections without password (trust auth)
RUN PG_HBA=$(find /etc/postgresql -name pg_hba.conf) && \
    sed -i 's/peer$/trust/' "$PG_HBA" && \
    sed -i 's/scram-sha-256$/trust/' "$PG_HBA" && \
    sed -i 's/md5$/trust/' "$PG_HBA"
USER root

# Copy project files
WORKDIR /app
COPY tasks/ /app/tasks/
COPY configs/ /app/configs/
COPY db/ /app/db/
COPY server.py /app/server.py
COPY discover_tools.py /app/discover_tools.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
CMD ["/app/entrypoint.sh"]
