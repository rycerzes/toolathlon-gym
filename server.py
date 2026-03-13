"""
Toolathlon Gym — OpenReward Standard environment server.

Exposes 503 multi-tool tasks via ORS. Each task's MCP tools are surfaced as
first-class tools through list_task_tools(). The ORS server, PostgreSQL, and
MCP server subprocesses all run inside the same Docker container.
"""
import asyncio
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence
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
        # Build tool_name → server_name mapping from pre-discovered schemas
        self._tool_to_server: dict[str, str] = {}
        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                self._tool_to_server[tool_info["name"]] = server

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

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            return f"Error: Unknown tool '{tool_name}'"
        proc = self._processes.get(server_name)
        if not proc:
            return f"Error: Server '{server_name}' is not running"
        return await proc.call_tool(tool_name, arguments)

    async def close(self):
        for proc in self._processes.values():
            await proc.close()
        self._processes.clear()


# ── Input models ─────────────────────────────────────────────────────────────

class PythonExecuteInput(BaseModel):
    code: str = Field(description="Python code to execute")


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
        # Build set of MCP tool names for routing in _call_tool
        self._mcp_tool_names: set[str] = set()

    async def setup(self):
        # Load task config
        config_path = self.task_dir / "task_config.json"
        if config_path.exists():
            self._task_config = json.loads(config_path.read_text())

        needed_servers = self._task_config.get("needed_mcp_servers", [])

        # Build MCP tool name set for _call_tool routing
        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                self._mcp_tool_names.add(tool_info["name"])

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

        # Read task description
        task_md_path = self.task_dir / "docs" / "task.md"
        if task_md_path.exists():
            parts.append(task_md_path.read_text())

        # Append completion instruction
        parts.append("\nWhen you have completed the task, call the `claim_done` tool.")

        return [TextBlock(text="\n\n".join(parts))]

    def list_task_tools(self) -> ListToolsOutput:
        needed_servers = self._task_config.get("needed_mcp_servers", [])
        tools: list[ToolSpec] = []

        # Detect name collisions across servers
        name_counts: dict[str, int] = defaultdict(int)
        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                name_counts[tool_info["name"]] += 1

        has_collisions = any(c > 1 for c in name_counts.values())

        for server in needed_servers:
            for tool_info in ALL_TOOL_SCHEMAS.get(server, []):
                name = tool_info["name"]
                if has_collisions and name_counts[name] > 1:
                    name = f"{server}__{name}"
                tools.append(ToolSpec(
                    name=name,
                    description=tool_info.get("description", ""),
                    input_schema=tool_info.get("inputSchema"),
                ))

        return ListToolsOutput(tools=tools)

    async def _call_tool(self, name: str, input: dict) -> RunToolOutput:
        # Check if this is an MCP tool
        # Handle prefixed names (server__tool) by stripping prefix
        mcp_tool_name = name
        if "__" in name:
            parts = name.split("__", 1)
            mcp_tool_name = parts[1]

        if mcp_tool_name in self._mcp_tool_names and self._mcp_bridge:
            try:
                result_text = await self._mcp_bridge.call_tool(mcp_tool_name, input)
                # Truncate very long outputs
                if len(result_text) > 50000:
                    result_text = result_text[:50000] + "\n... (output truncated)"
                return RunToolOutput(RunToolSuccess(
                    output=ToolOutput(blocks=[TextBlock(text=result_text)])
                ))
            except Exception as e:
                return RunToolOutput(RunToolError(error=f"MCP tool error: {e}"))

        # Fall through to built-in tools (claim_done, python_execute)
        return await super()._call_tool(name, input)

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
