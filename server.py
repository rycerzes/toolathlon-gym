"""
Toolathlon Gym — OpenReward Standard environment server.

Exposes 503 multi-tool tasks via ORS. The ORS server, PostgreSQL, and MCP
server subprocesses all run inside the same Docker container.

Tool exposure
-------------
A task's MCP tools can run into the hundreds, which blows past the 128-tool
ceiling on OpenAI-compatible inference endpoints. Instead of registering
each MCP tool individually, `list_task_tools()` returns just two
meta-tools, `get_tool_details` and `call_tool`. The full per-task catalog
(name + one-line description for every available tool) is inlined into the
system prompt so the agent sees the menu up front; it fetches a tool's
input_schema on demand via `get_tool_details` and invokes it via
`call_tool`.
"""
import asyncio
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from openreward.environments.environment import Environment, tool
from openreward.environments.server import Server
from openreward.environments.types import (
    ListToolsOutput,
    RunToolError,
    RunToolOutput,
    RunToolSuccess,
    TextBlock,
    ToolOutput,
    ToolSpec,
)

# ── Constants ────────────────────────────────────────────────────────────────

TASKS_ROOT = Path("/app/tasks/finalpool")
CONFIGS_DIR = Path("/app/configs/mcp_servers")
LOCAL_SERVERS = "/opt/local_servers"
TOOL_SCHEMAS_FILE = Path("/app/tool_schemas.json")

CATALOG_DESC_MAX_CHARS = 160

# ── Load pre-discovered tool schemas ─────────────────────────────────────────

ALL_TOOL_SCHEMAS: dict[str, list[dict]] = {}
if TOOL_SCHEMAS_FILE.exists():
    ALL_TOOL_SCHEMAS = json.loads(TOOL_SCHEMAS_FILE.read_text())


# ── Template resolution (from original tool_servers.py) ──────────────────────

def _resolve(value: str, workspace: str) -> str:
    if not isinstance(value, str):
        return value
    return (
        value
        .replace("${local_servers_paths}", LOCAL_SERVERS)
        .replace("${agent_workspace}", workspace)
    )


# ── MCP Bridge — manages MCP server subprocesses per session ─────────────────

def _make_request(method: str, params: dict | None = None, req_id: int = 1) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


class _MCPProcess:
    """A single MCP server subprocess with JSON-RPC communication."""

    def __init__(self, name: str, proc: asyncio.subprocess.Process):
        self.name = name
        self.proc = proc
        self._req_id = 10

    async def send_recv(self, method: str, params: dict | None = None, timeout: float = 30) -> dict | None:
        self._req_id += 1
        req_id = self._req_id
        req = _make_request(method, params, req_id)
        self.proc.stdin.write(req)
        await self.proc.stdin.drain()

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(),
                    timeout=max(0.1, deadline - asyncio.get_event_loop().time()),
                )
            except asyncio.TimeoutError:
                break
            if not line:
                await asyncio.sleep(0.05)
                continue
            try:
                resp = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if resp.get("id") == req_id:
                return resp
        return None

    async def initialize(self) -> bool:
        resp = await self.send_recv("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "toolathlon-ors", "version": "1.0"},
        }, timeout=15)
        if resp and "result" in resp:
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            self.proc.stdin.write(notif.encode())
            await self.proc.stdin.drain()
            return True
        return False

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        resp = await self.send_recv("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=120)
        if resp is None:
            return "Error: MCP tool call timed out"
        if "error" in resp:
            return f"Error: {resp['error'].get('message', str(resp['error']))}"
        result = resp.get("result", {})
        # Extract content from MCP result
        content_parts = result.get("content", [])
        texts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif isinstance(part, dict):
                texts.append(json.dumps(part))
            else:
                texts.append(str(part))
        return "\n".join(texts) if texts else json.dumps(result)

    async def close(self):
        try:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class MCPBridge:
    """Manages MCP server subprocesses for a single session."""

    def __init__(self, needed_servers: list[str], workspace_dir: str):
        self.needed_servers = needed_servers
        self.workspace_dir = workspace_dir
        self._processes: dict[str, _MCPProcess] = {}
        # bare tool name → list of servers offering it. Multi-valued because
        # different MCP servers can advertise tools with the same name; callers
        # should pass `server` to disambiguate when needed.
        self._tool_servers: dict[str, list[str]] = defaultdict(list)
        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                self._tool_servers[tool_info["name"]].append(server)

    async def start(self):
        """Launch and initialize all needed MCP servers."""
        for yaml_path in sorted(CONFIGS_DIR.glob("*.yaml")):
            with open(yaml_path) as f:
                cfg = yaml.safe_load(f)
            if not cfg:
                continue
            name = cfg.get("name", yaml_path.stem)
            if name not in self.needed_servers:
                continue

            params = cfg.get("params", {})
            command = _resolve(params.get("command", ""), self.workspace_dir)
            args = [_resolve(a, self.workspace_dir) for a in params.get("args", [])]
            env_vars = {k: _resolve(v, self.workspace_dir) for k, v in params.get("env", {}).items()}
            cwd = _resolve(params.get("cwd", self.workspace_dir), self.workspace_dir)

            # For "uv run <script>" without --directory, infer cwd from script path
            if command == "uv" and "run" in args and "cwd" not in params:
                run_idx = args.index("run")
                if run_idx + 1 < len(args):
                    script_path = args[run_idx + 1]
                    if os.path.isfile(script_path):
                        cwd = os.path.dirname(script_path)

            # Override PG_HOST to localhost since DB runs in same container
            full_env = {**os.environ, **env_vars, "PG_HOST": "localhost", "PGHOST": "localhost"}
            os.makedirs(cwd, exist_ok=True)

            try:
                proc = await asyncio.create_subprocess_exec(
                    command, *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=full_env,
                    cwd=cwd,
                )
                mcp_proc = _MCPProcess(name, proc)
                await asyncio.sleep(1.5)  # let server boot

                if await mcp_proc.initialize():
                    self._processes[name] = mcp_proc
                else:
                    print(f"[MCPBridge] WARNING: Failed to initialize {name}")
                    await mcp_proc.close()
            except Exception as e:
                print(f"[MCPBridge] WARNING: Failed to launch {name}: {e}")

    async def call_tool(self, tool_name: str, arguments: dict, *, server: str | None = None) -> str:
        candidates = self._tool_servers.get(tool_name) or []
        if not candidates:
            return f"Error: Unknown tool '{tool_name}'"
        if server is not None:
            if server not in candidates:
                return f"Error: Tool '{tool_name}' is not provided by server '{server}'"
            chosen = server
        else:
            chosen = candidates[0]  # ambiguous: first registered wins
        proc = self._processes.get(chosen)
        if not proc:
            return f"Error: Server '{chosen}' is not running"
        return await proc.call_tool(tool_name, arguments)

    async def close(self):
        for proc in self._processes.values():
            await proc.close()
        self._processes.clear()


# ── Input models ─────────────────────────────────────────────────────────────

class PythonExecuteInput(BaseModel):
    code: str = Field(description="Python code to execute")


class GetToolDetailsInput(BaseModel):
    name: str = Field(
        description=(
            "Fully qualified tool name as listed in the system-prompt catalog, "
            "formatted as `server.tool_name` (e.g. `notion.search`)."
        ),
    )


class CallToolInput(BaseModel):
    name: str = Field(
        description=(
            "Fully qualified tool name as listed in the system-prompt catalog, "
            "formatted as `server.tool_name` (e.g. `notion.search`). The prefix "
            "identifies which MCP server provides the tool."
        ),
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON object of arguments matching the tool's input_schema.",
    )


# ── Toolathlon Gym Environment ───────────────────────────────────────────────

class ToolathlonGym(Environment):

    def __init__(self, task_spec: dict = {}, secrets: dict[str, str] = {}):
        super().__init__(task_spec, secrets)
        self.task_name: str = task_spec.get("task_name", "")
        self.task_dir = TASKS_ROOT / self.task_name
        self.workspace_dir = Path(f"/tmp/workspaces/{uuid4()}")
        self._task_config: dict = {}
        self._launch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._launch_time_display = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
        self._mcp_bridge: MCPBridge | None = None
        # Build set of MCP tool names for routing in _call_tool. Keys are the
        # bare (unprefixed) tool names that MCPBridge dispatches against.
        self._mcp_tool_names: set[str] = set()
        # Per-task catalog of tool entries, populated in setup(). Each entry:
        # {name, bare_name, server, description, input_schema}. `name` is the
        # canonical `server.bare_name` form used by call_tool / get_tool_details.
        self._task_tool_index: list[dict] = []
        self._task_tool_by_name: dict[str, dict] = {}

    async def setup(self):
        # Load task config
        config_path = self.task_dir / "task_config.json"
        if config_path.exists():
            self._task_config = json.loads(config_path.read_text())

        needed_servers = self._task_config.get("needed_mcp_servers", [])

        # Build the per-task tool index used by both the system-prompt catalog
        # and the meta-tools. Every tool is exposed with a `server.bare`
        # prefix so the source MCP server is always explicit and dispatch is
        # unambiguous even when bare names collide.
        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                bare = tool_info["name"]
                entry = {
                    "name": f"{server}.{bare}",
                    "bare_name": bare,
                    "server": server,
                    "description": tool_info.get("description", "") or "",
                    "input_schema": tool_info.get("inputSchema") or {},
                }
                self._task_tool_index.append(entry)
                self._task_tool_by_name[entry["name"]] = entry
                self._mcp_tool_names.add(bare)

        # Create workspace
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Copy initial workspace files
        initial_ws = self.task_dir / "initial_workspace"
        if initial_ws.exists():
            for item in initial_ws.iterdir():
                dest = self.workspace_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        # Create special directories
        for subdir in ["arxiv_local_storage", "memory", ".playwright_output"]:
            (self.workspace_dir / subdir).mkdir(exist_ok=True)

        # Run preprocess script
        # Note: some preprocess scripts spawn background servers (e.g. http.server)
        # that inherit pipes, so we must not use communicate() which waits for EOF.
        # Instead, use DEVNULL and wait with a timeout.
        preprocess = self.task_dir / "preprocess" / "main.py"
        if preprocess.exists():
            proc = await asyncio.create_subprocess_exec(
                "python3", str(preprocess),
                "--agent_workspace", str(self.workspace_dir),
                "--launch_time", self._launch_time,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self.task_dir / "preprocess"),
                env={**os.environ, "PGHOST": "localhost"},
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
                if proc.returncode != 0:
                    print(f"[setup] Preprocess warning (rc={proc.returncode})", flush=True)
            except asyncio.TimeoutError:
                print("[setup] Preprocess timed out (30s), continuing anyway", flush=True)

        # Start MCP servers
        if needed_servers:
            self._mcp_bridge = MCPBridge(needed_servers, str(self.workspace_dir))
            await self._mcp_bridge.start()

    def get_prompt(self) -> Sequence[TextBlock]:
        parts = []

        # Read agent system prompt
        sys_prompt_path = self.task_dir / "docs" / "agent_system_prompt.md"
        if sys_prompt_path.exists():
            sys_prompt = sys_prompt_path.read_text()
            sys_prompt = sys_prompt.replace(
                "!!<<<<||||workspace_dir||||>>>>!!", str(self.workspace_dir)
            )
            sys_prompt = sys_prompt.replace(
                "!!<<<<||||time||||>>>>!!", self._launch_time_display
            )
            parts.append(sys_prompt)

        # The agent only ever sees the get_tool_details / call_tool meta-tools,
        # so we inline the per-task tool catalog up front — otherwise it has
        # no way to know what MCP tools exist.
        parts.append(self._tool_catalog_block())

        # Read task description
        task_md_path = self.task_dir / "docs" / "task.md"
        if task_md_path.exists():
            parts.append(task_md_path.read_text())

        # Append completion instruction
        parts.append("\nWhen you have completed the task, call the `claim_done` tool.")

        return [TextBlock(text="\n\n".join(parts))]

    @staticmethod
    def _short_desc(text: str, max_chars: int = CATALOG_DESC_MAX_CHARS) -> str:
        """Collapse whitespace and truncate so each catalog entry fits one line."""
        flat = " ".join((text or "").split())
        if len(flat) <= max_chars:
            return flat
        return flat[: max_chars - 1].rstrip() + "…"

    def _tool_catalog_block(self) -> str:
        by_server: dict[str, list[dict]] = defaultdict(list)
        for entry in self._task_tool_index:
            by_server[entry["server"]].append(entry)

        lines: list[str] = [
            "## Available MCP tools",
            "",
            f"This task exposes {len(self._task_tool_index)} MCP tools across "
            f"{len(by_server)} servers, listed below as `server.tool_name — "
            "short description`. Two meta-tools mediate access:",
            "",
            "- `get_tool_details(name)` — return the full `input_schema` (and "
            "untruncated description) for one tool. Pass the exact "
            "`server.tool_name` shown in the catalog.",
            "- `call_tool(name, arguments)` — invoke a tool. `name` is again "
            "the exact `server.tool_name`; `arguments` must conform to the "
            "tool's `input_schema`.",
            "",
            "Typical workflow: scan the catalog → `get_tool_details` for the "
            "tool(s) you want to call → `call_tool`. The shared "
            "`python_execute` and `claim_done` tools remain directly callable.",
            "",
        ]
        for server in sorted(by_server):
            lines.append(f"### {server}")
            for entry in sorted(by_server[server], key=lambda e: e["name"]):
                desc = self._short_desc(entry["description"])
                if desc:
                    lines.append(f"- `{entry['name']}` — {desc}")
                else:
                    lines.append(f"- `{entry['name']}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def list_task_tools(self) -> ListToolsOutput:
        return ListToolsOutput(tools=[
            ToolSpec(
                name="get_tool_details",
                description=(
                    "Return the full input_schema and description for one MCP "
                    "tool, looked up by its `server.tool_name` identifier "
                    "(as listed in the system-prompt catalog)."
                ),
                input_schema=GetToolDetailsInput.model_json_schema(),
            ),
            ToolSpec(
                name="call_tool",
                description=(
                    "Invoke an MCP tool. Pass the `server.tool_name` from the "
                    "system-prompt catalog and an `arguments` object matching "
                    "that tool's input_schema (fetch via get_tool_details)."
                ),
                input_schema=CallToolInput.model_json_schema(),
            ),
        ])

    async def _call_tool(self, name: str, input: dict) -> RunToolOutput:
        # Handle MCP tool names that arrive directly (some inference clients
        # may flatten the catalog). Prefixed `server.tool` form disambiguates
        # collisions; without a prefix we let the bridge pick.
        explicit_server: str | None = None
        mcp_tool_name = name
        if "." in name:
            head, tail = name.split(".", 1)
            mcp_tool_name = tail
            explicit_server = head

        if mcp_tool_name in self._mcp_tool_names and self._mcp_bridge:
            try:
                result_text = await self._mcp_bridge.call_tool(
                    mcp_tool_name, input, server=explicit_server
                )
                # Truncate very long outputs
                if len(result_text) > 50000:
                    result_text = result_text[:50000] + "\n... (output truncated)"
                return RunToolOutput(RunToolSuccess(
                    output=ToolOutput(blocks=[TextBlock(text=result_text)])
                ))
            except Exception as e:
                return RunToolOutput(RunToolError(error=f"MCP tool error: {e}"))

        # Fall through to built-in tools (claim_done, python_execute,
        # get_tool_details, call_tool).
        return await super()._call_tool(name, input)

    @tool(shared=False)
    async def get_tool_details(self, params: GetToolDetailsInput) -> ToolOutput:
        """Return the full schema and description for one MCP tool."""
        entry = self._task_tool_by_name.get(params.name)
        if entry is None:
            return ToolOutput(blocks=[TextBlock(text=(
                f"Error: unknown tool '{params.name}'. Pass the exact "
                "`server.tool_name` shown in the system-prompt catalog."
            ))])
        payload = {
            "name": entry["name"],
            "server": entry["server"],
            "description": entry["description"],
            "input_schema": entry["input_schema"],
        }
        return ToolOutput(blocks=[TextBlock(text=json.dumps(payload, indent=2))])

    @tool(shared=False)
    async def call_tool(self, params: CallToolInput) -> ToolOutput:
        """Invoke an MCP tool by name, dispatching through the running bridge."""
        if self._mcp_bridge is None:
            return ToolOutput(blocks=[TextBlock(
                text="Error: MCP bridge is not running for this task."
            )])

        name = params.name
        entry = self._task_tool_by_name.get(name)
        if entry is None:
            return ToolOutput(blocks=[TextBlock(text=(
                f"Error: unknown tool '{name}'. Pass the exact "
                "`server.tool_name` shown in the system-prompt catalog."
            ))])

        try:
            result_text = await self._mcp_bridge.call_tool(
                entry["bare_name"], dict(params.arguments), server=entry["server"]
            )
        except Exception as e:
            return ToolOutput(blocks=[TextBlock(text=f"MCP tool error: {e}")])

        if len(result_text) > 50000:
            result_text = result_text[:50000] + "\n... (output truncated)"
        return ToolOutput(blocks=[TextBlock(text=result_text)])

    @tool
    async def claim_done(self) -> ToolOutput:
        """Signal that the task is complete and trigger evaluation."""
        eval_script = self.task_dir / "evaluation" / "main.py"
        if not eval_script.exists():
            return ToolOutput(
                blocks=[TextBlock(text="No evaluation script found.")],
                reward=0.0,
                finished=True,
            )

        res_log = self.workspace_dir / "eval_result.json"
        groundtruth = self.task_dir / "groundtruth_workspace"

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(eval_script),
                "--agent_workspace", str(self.workspace_dir),
                "--groundtruth_workspace", str(groundtruth),
                "--launch_time", self._launch_time,
                "--res_log_file", str(res_log),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.task_dir / "evaluation"),
                env={**os.environ, "PGHOST": "localhost"},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode() + stderr.decode()

            if proc.returncode == 0:
                return ToolOutput(
                    blocks=[TextBlock(text=f"PASS\n{output}")],
                    reward=1.0,
                    finished=True,
                )
            else:
                return ToolOutput(
                    blocks=[TextBlock(text=f"FAIL\n{output}")],
                    reward=0.0,
                    finished=True,
                )
        except asyncio.TimeoutError:
            return ToolOutput(
                blocks=[TextBlock(text="Evaluation timed out after 120s")],
                reward=0.0,
                finished=True,
            )
        except Exception as e:
            return ToolOutput(
                blocks=[TextBlock(text=f"Evaluation error: {e}")],
                reward=0.0,
                finished=True,
            )

    @tool
    async def python_execute(self, params: PythonExecuteInput) -> ToolOutput:
        """Execute Python code in the workspace and return stdout/stderr."""
        code_file = self.workspace_dir / f"_exec_{uuid4().hex[:8]}.py"
        code_file.write_text(params.code)

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(code_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
                env={**os.environ, "PGHOST": "localhost"},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode()
            errors = stderr.decode()
            result = ""
            if output:
                result += f"stdout:\n{output}\n"
            if errors:
                result += f"stderr:\n{errors}\n"
            if not result:
                result = "(no output)"
            return ToolOutput(blocks=[TextBlock(text=result)])
        except asyncio.TimeoutError:
            return ToolOutput(blocks=[TextBlock(text="Execution timed out after 60s")])
        except Exception as e:
            return ToolOutput(blocks=[TextBlock(text=f"Execution error: {e}")])
        finally:
            code_file.unlink(missing_ok=True)

    async def teardown(self):
        if self._mcp_bridge:
            await self._mcp_bridge.close()
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    @classmethod
    def list_splits(cls) -> list[str]:
        return ["train"]

    @classmethod
    def list_tasks(cls, split: str) -> list[dict]:
        if split != "train":
            return []
        return [
            {"task_name": d.name}
            for d in sorted(TASKS_ROOT.iterdir())
            if d.is_dir() and (d / "task_config.json").exists()
        ]


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    Server([ToolathlonGym]).run()
