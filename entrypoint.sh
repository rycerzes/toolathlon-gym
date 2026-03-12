#!/bin/bash
set -e

# Start PostgreSQL
service postgresql start
until pg_isready -U eigent -d toolathlon_gym 2>/dev/null; do sleep 1; done

# Initialize databases on first boot
if [ ! -f /tmp/.db_initialized ]; then
    gunzip -c /app/db/init.sql.gz | psql -U eigent -d toolathlon_gym
    # Some MCP servers (snowflake) connect to database "toolathlon"
    gunzip -c /app/db/init.sql.gz | psql -U eigent -d toolathlon
    touch /tmp/.db_initialized
fi

# Discover MCP tool schemas on first boot (needs running DB for PG-backed servers)
if [ ! -f /app/tool_schemas.json ]; then
    python3 /app/discover_tools.py
fi

exec python3 /app/server.py
