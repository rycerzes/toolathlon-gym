"""
Discover all MCP server tools at build/boot time.

For each YAML config in /app/configs/mcp_servers/, launches the server,
sends JSON-RPC initialize + tools/list, and collects tool schemas.
Saves {server_name: [{name, description, inputSchema}, ...]} to /app/tool_schemas.json.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

CONFIGS_DIR = Path("/app/configs/mcp_servers")
LOCAL_SERVERS = "/opt/local_servers"
OUTPUT_FILE = Path("/app/tool_schemas.json")

# Placeholder values for token-based config vars (PG-backed servers don't need real tokens)
TOKEN_VARS = {
    "token.snowflake_account": "local_pg",
    "token.snowflake_warehouse": "local",
    "token.snowflake_user": "postgres",
    "token.snowflake_private_key_path": "",
    "token.snowflake_role": "PUBLIC",
    "token.snowflake_database": "toolathlon",
    "token.snowflake_schema": "sf",
    "token.snowflake_op_allowed_databases": "toolathlon",
    "token.google_oauth2_credentials_path": "",
    "token.google_oauth2_token_path": "",
    "token.google_sheets_folder_id": "",
    "token.emails_config_file": "",
    "token.notion_allowed_page_ids": "",
    "token.notion_integration_key_eval": "ntn-placeholder",
    "token.canvas_api_token": "placeholder",
    "token.canvas_domain": "localhost:8080",
    "token.google_client_id": "placeholder",
    "token.google_client_secret": "placeholder",
    "token.google_refresh_token": "placeholder",
    "token.woocommerce_site_url": "http://localhost:8081",
    "token.woocommerce_api_key": "placeholder",
    "token.woocommerce_api_secret": "placeholder",
}


def resolve(value: str, workspace: str) -> str:
    if not isinstance(value, str):
        return value
    value = value.replace("${local_servers_paths}", LOCAL_SERVERS)
    value = value.replace("${agent_workspace}", workspace)
    for k, v in TOKEN_VARS.items():
        value = value.replace("${" + k + "}", v)
    return value


def make_request(method, params=None, req_id=1) -> bytes:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


def parse_response(line: bytes):
    try:
        return json.loads(line.decode())
    except Exception:
        return None


def discover_server(yaml_path: Path, workspace: str) -> list[dict] | None:
    """Launch a server, initialize it, list tools, return tool schemas."""
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        return None

    params = cfg.get("params", {})
    command = resolve(params.get("command", ""), workspace)
    args = [resolve(a, workspace) for a in params.get("args", [])]
    env_vars = {k: resolve(v, workspace) for k, v in params.get("env", {}).items()}
    cwd = resolve(params.get("cwd", workspace), workspace)

    full_env = {**os.environ, **env_vars}
    os.makedirs(cwd, exist_ok=True)

    name = cfg.get("name", yaml_path.stem)
    print(f"  Discovering {name}...", end=" ", flush=True)

    try:
        proc = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=cwd,
        )
    except Exception as e:
        print(f"LAUNCH FAIL: {e}")
        return None

    try:
        time.sleep(2)  # let server boot

        # Check if process already died
        if proc.poll() is not None:
            print("PROCESS DIED")
            return None

        # Initialize
        try:
            proc.stdin.write(make_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "discover", "version": "1.0"},
            }, req_id=0))
            proc.stdin.flush()
        except BrokenPipeError:
            print("BROKEN PIPE (init)")
            return None

        deadline = time.time() + 15
        init_ok = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            resp = parse_response(line)
            if resp and resp.get("id") == 0 and "result" in resp:
                init_ok = True
                break

        if not init_ok:
            print("INIT FAIL")
            return None

        # Send initialized notification
        try:
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            proc.stdin.write(notif.encode())
            proc.stdin.flush()

            # List tools
            proc.stdin.write(make_request("tools/list", {}, req_id=1))
            proc.stdin.flush()
        except BrokenPipeError:
            print("BROKEN PIPE (list)")
            return None

        deadline = time.time() + 15
        tools = None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            resp = parse_response(line)
            if resp and resp.get("id") == 1 and "result" in resp:
                tools = resp["result"].get("tools", [])
                break

        if tools is None:
            print("LIST FAIL")
            return None

        schemas = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in tools
        ]
        print(f"OK ({len(schemas)} tools)")
        return schemas

    except Exception as e:
        print(f"ERROR: {e}")
        return None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main():
    print("Discovering MCP server tool schemas...")
    workspace = tempfile.mkdtemp(prefix="discover_")
    all_schemas: dict[str, list[dict]] = {}

    for yaml_path in sorted(CONFIGS_DIR.glob("*.yaml")):
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f)
        if not cfg:
            continue
        name = cfg.get("name", yaml_path.stem)
        server_workspace = os.path.join(workspace, name)
        os.makedirs(server_workspace, exist_ok=True)

        tools = discover_server(yaml_path, server_workspace)
        if tools is not None:
            all_schemas[name] = tools
        else:
            print(f"  WARNING: Could not discover tools for {name}, skipping")

    OUTPUT_FILE.write_text(json.dumps(all_schemas, indent=2))
    total_tools = sum(len(v) for v in all_schemas.values())
    print(f"\nSaved {len(all_schemas)} servers, {total_tools} tools to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
