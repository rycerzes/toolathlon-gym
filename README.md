# Toolathlon Gym

[![OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://www.openreward.ai/GeneralReasoning/ToolathlonGym)

## Description

Toolathlon Gym is an ORS environment for evaluating multi-tool coordination capabilities, developed by [Eigent AI](https://github.com/eigent-ai/toolathlon_gym). It contains 503 tasks requiring agents to coordinate 4-8 MCP servers (out of 25) backed by PostgreSQL. Tasks span productivity workflows (spreadsheets, documents, email), data analysis (SQL, finance, web scraping), content management (YouTube, Google Forms, Notion), and system administration (terminal, filesystem).

## Capabilities

- Multi-tool coordination across 25 MCP servers
- Database querying via Snowflake/PostgreSQL
- Document creation (Excel, Word, PowerPoint, PDF)
- Web scraping and browser automation via Playwright
- Email, calendar, and forms management
- File system operations and Python code execution

## Compute Requirements

Each task runs inside a Docker container with PostgreSQL, 25 MCP server implementations, Node.js 22, and Python 3.12. The ORS server runs alongside all services in a single container.

## License

[Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## Tasks

There is one split in this environment:

- **test**: 503 tasks

Tasks require coordinating 4-8 MCP servers per task across domains including YouTube analysis, spreadsheet automation, database querying, web scraping, document generation, email management, and calendar scheduling.

## Reward Structure

This is a multi-turn environment with script-based validation. The agent uses MCP tools and built-in tools to complete tasks, then calls `claim_done` to run the evaluation script. The reward is binary: 1.0 if all checks pass, 0.0 otherwise.

## Data

Data consists of 503 task directories sourced from [GitHub eigent-ai/toolathlon_gym](https://github.com/eigent-ai/toolathlon_gym). Each task includes `task_config.json`, `docs/task.md`, `preprocess/main.py`, `evaluation/main.py`, and initial workspace files. PostgreSQL is seeded with data across 6 schemas (Canvas LMS, HR/Sales, e-commerce, finance, YouTube, rail).

## Tools

Shared tools (always available):

| Tool | Description |
|------|-------------|
| `claim_done` | Run evaluation and get reward. Ends the episode. |
| `python_execute` | Execute Python code in the workspace. |

Task-specific tools (per-task, from MCP servers):

Each task exposes tools from its required MCP servers. Examples include `read_file`, `list_spreadsheets`, `search_videos`, `run_command`, `list_databases`, `browser_navigate`, and ~300 others across 25 servers.

## Time Horizon

Multi-turn. Agents read task instructions, use MCP tools to gather data, create files, query databases, and perform analysis, then call `claim_done` for evaluation.

## Environment Difficulty

Tasks require coordinating multiple tools across different domains. Most tasks involve 4-8 MCP servers and require multi-step reasoning, data transformation, and file generation.

## Other Environment Requirements

None.

## Safety

Agents operate within Docker containers with isolated PostgreSQL databases and per-session workspaces. MCP servers are scoped to the agent's workspace directory.

## Citation

```bibtex
@misc{toolathlon,
  author    = {Eigent AI},
  title     = {{Toolathlon Gym: A Multi-Tool Benchmark for LLM Agents}},
  year      = {2025},
  url       = {https://github.com/eigent-ai/toolathlon_gym}
}
```
