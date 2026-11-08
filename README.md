# cline-hermes-bridge

**An MCP (Model Context Protocol) server that exposes Cline session history as read-only resources and provides asynchronous process-control tools for Hermes Agent to monitor, analyze, and control Cline CLI workers.**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

- [Architecture](#architecture)
- [Installation](#installation)
- [Cline CLI Path Configuration](#cline-cli-path-configuration)
- [Running the Server](#running-the-server)
- [MCP Resources (Read-Only Monitoring)](#mcp-resources--read-only-monitoring)
- [MCP Tools (Process Control)](#mcp-tools--process-control)
- [Hermes Agent Integration](#hermes-agent-integration)
- [Error Handling](#error-handling)
- [Contributing](#contributing)

---

## Architecture

```
[Hermes Agent]
     │
     ▼  (JSON-RPC 2.0 over stdio)
[cline-hermes-bridge MCP Server]
     │
     ├─► Reads Cline session JSON files from local history
     ├─► Spawns / monitors / kills `cline` CLI subprocesses
     └─► Returns structured JSON through MCP stdio transport
```

The bridge operates as a **pure stdio MCP server**. It reads Cline session history from the
file system and spawns Cline CLI processes via `asyncio.subprocess`, keeping the MCP
communication channel entirely non-blocking.

---

## Installation

### Prerequisites

- **Python 3.10+**
- **Node.js** and the global `cline` CLI package
  - Install with: `npm install -g @anthropic/cline`

### Setup

```bash
cd cline-hermes-bridge
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Cline CLI Path Configuration

The server auto-detects the host operating system and resolves Cline CLI's session history
directory:

| OS    | Session Directory                                    |
|-------|------------------------------------------------------|
| macOS | `~/.cline/data/sessions/`                            |
| Windows | `%APPDATA%\.cline\data\sessions\`                  |
| Linux | `~/.cline/data/sessions/`                            |

> **Override:** If your Cline CLI install uses a non-default sessions directory, set
> the environment variable `CLINE_HISTORY_DIR` to point to the correct path. The server
> will use that value instead of the auto-detected path.

Each session directory contains two files:
- `<session_id>.json` — session metadata (prompt, status, PID, etc.)
- `<session_id>.messages.json` — full chat log with all messages

---

## Running the Server

```bash
python server.py
```

The server communicates via **stdin/stdout using JSON-RPC 2.0**. It will keep running until
killed (Ctrl+C).

### Docker (optional)

```bash
docker build -t cline-hermes-bridge .
docker run -i --rm cline-hermes-bridge
```

---

## MCP Resources (Read-Only Monitoring)

### `cline://sessions`

Returns a JSON array of all available sessions:

```json
[
  {
    "session_id": "a1b2c3d4-...",
    "initial_task": "Refactor the auth module...",
    "created_at": "2025-01-15T10:30:00+00:00",
    "status": "completed",
    "interactive": true,
    "pid": 12345
  }
]
```

### `cline://sessions/{session_id}/chat`

Returns the full message history for a specific session:

```json
{
  "session_id": "a1b2c3d4-...",
  "messages": [
    {
      "role": "user",
      "text": "Refactor the auth module..."
    },
    {
      "role": "assistant",
      "text": "I'll refactor the auth module..."
    }
  ]
}
```

### `cline://agent/status`

Returns the aggregate status of all tracked Cline CLI processes:

```json
{
  "status": "RUNNING",
  "active_processes": 2,
  "pids": [51944, 52001]
}
```

---

## MCP Tools (Process Control)

### `list_cline_sessions`

**Description:** Lists all Cline CLI sessions sorted by newest first.

```json
// Input
{}

// Output
[
  {
    "session_id": "session_1794143655625_7idx7",
    "initial_task": "Fix the Cline CLI integration...",
    "created_at": "2026-11-08T13:14:15.681Z",
    "status": "running",
    "interactive": true,
    "pid": 51944
  }
]
```

### `command_cline_agent`

**Description:** Start a new Cline CLI task or resume an existing one.

**Parameters:**

| Name                | Type   | Required | Description                                            |
|---------------------|--------|----------|--------------------------------------------------------|
| `prompt`            | string | Yes      | The prompt/instructions to pass to Cline.               |
| `resume_session_id` | string | No       | Session ID to resume. If provided, continues that session. |

```json
// New session
{ "prompt": "Refactor the auth module" }

// Resume session
{ "prompt": "Continue the refactoring", "resume_session_id": "session_1794143655625_7idx7" }
```

The server uses `cline --id <sid> "prompt"` to resume and `cline "prompt"` for new sessions.

**Output:**

```json
{
  "pid": 51944,
  "status": "spawning",
  "prompt_length": 42
}
```

### `get_agent_runtime_status`

**Description:** Checks whether a specific Cline CLI task is active, stalled, or
finished.

**Parameters:**

| Name  | Type   | Required | Description                          |
|-------|--------|----------|--------------------------------------|
| `pid` | integer| Yes      | Process ID returned by `command_cline_agent`. |

```json
// Input
{ "pid": 12345 }

// Output
{
  "pid": 12345,
  "alive": true,
  "started_at": "2026-11-08T13:14:15.681Z",
  "runtime_seconds": 15.3,
  "stdout_excerpt": "...",
  "stderr_excerpt": "...",
  "return_code": null
}
```

### `stop_cline_agent`

**Description:** Forcefully terminates a running Cline subprocess. Sends `SIGTERM` and
escalates to `SIGKILL` after 0.5s if the process does not respond.

**Parameters:**

| Name  | Type   | Required | Description              |
|-------|--------|----------|--------------------------|
| `pid` | integer| Yes      | The process ID to terminate. |

```json
// Input
{ "pid": 12345 }

// Output
{
  "pid": 12345,
  "killed": true,
  "status": "terminated"
}
```

---

## Hermes Agent Integration

There are two supported ways to embed this MCP server in Hermes Agent configuration.

### Option A: `config.yaml` (preferred)

Edit `~/.hermes/config.yaml`:

```yaml
cline-hermes-bridge:
  command: python
  args:
    - /path/to/cline-hermes-bridge/server.py
  env: {}
  disabled: false
  timeout: 300
```

### Option B: Environment Variable for Custom History Path

```bash
export CLINE_HISTORY_DIR="/custom/path/to/cline/history"
```

Or inline:

```bash
CLINE_HISTORY_DIR="/custom/path" python server.py
```

---

## Error Handling

The server gracefully handles the following edge cases:

| Scenario                        | Behavior                                              |
|--------------------------------|-------------------------------------------------------|
| `sessions/` directory missing   | Returns `[]` for session lists; no crash.              |
| Corrupted / unreadable JSON     | Skips the file, logs silently.                       |
| `cline` CLI not in PATH         | Returns a structured JSON error from `command_cline_agent`. |
| Process not tracked by `pid`    | Returns `"error": "Process not tracked"` from `get_agent_runtime_status`. |
| Kill command fails              | Returns `"killed": false` with the exception message.  |

All errors are returned as **structured JSON** inside the MCP `TextContent` envelope.

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Commit your changes: `git commit -m 'feat: add my feature'`
4. Push: `git push origin feat/my-feature`
5. Open a Pull Request

---

## License

This project is licensed under the [MIT License](LICENSE).
