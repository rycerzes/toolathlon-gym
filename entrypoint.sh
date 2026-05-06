#!/bin/bash
set -e

# MCP servers must write to the same DB the eval/preprocess scripts read from,
# else writes via gsheet/gcal/emails/gforms are invisible to the evaluator.
export PG_DATABASE=toolathlon_gym
export PG_USER=eigent
export PG_PASSWORD=camel

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
