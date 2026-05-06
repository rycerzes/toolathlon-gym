#!/bin/bash
set -e

# Postgres seed (toolathlon_template) and the MCP tool catalog
# (/app/tool_schemas.json) are baked into the image at build time. At runtime
# we only start postgres and exec the env server; per-session DBs are cloned
# from the template inside server.py.

# Start PostgreSQL
service postgresql start
until pg_isready -U eigent 2>/dev/null; do sleep 1; done

# GC any leftover per-session DBs (only relevant for long-lived dev containers
# where a previous run crashed before teardown ran).
psql -U eigent -d postgres -tAc \
    "SELECT datname FROM pg_database WHERE datname LIKE 's\\_%' ESCAPE '\\'" |
    while IFS= read -r db; do
        [ -z "$db" ] && continue
        echo "[entrypoint] dropping orphan DB: $db"
        psql -U eigent -d postgres -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE);" || true
    done

exec python3 /app/server.py
