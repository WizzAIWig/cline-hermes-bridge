# cline-hermes-bridge

**An MCP (Model Context Protocol) server that exposes Cline session history as read-only resources and provides asynchronous process-control tools for Hermes Agent to monitor, analyze, and control Cline CLI workers.**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

- [Architecture](#architecture)
- [Installation](#installation)
- [OS-Specific Path Configuration](#os-specific-path-configuration)
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
[cine-hermes-bridge MCP Server]
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
- **Node.js** and the global `cline` CLI package (`@anthropic/cline` or equivalent)
  - Install with: `npm install -g @anthropic/cline`
- VS Code installed (for Cline session history access)

### Setup

```bash
cd cline-hermes-bridge
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## OS-Specific Path Configuration

The server auto-detects the host operating system and resolves Cline's session history
directory. The resolved paths are:

| OS    | Environment Variable  | Path Pattern |
|-------|-----------------------|------------------------------------|
| macOS | `HOME`                | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/history/` |
| Windows | `APPDATA`         | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\history\` |
| Linux | `HOME`                | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/history/` |

> **Override:** If your VS Code or Cline install uses a non-default user data directory, set
> the environment variable `CLINE_HISTORY_DIR` to point to the correct path. The server
> will use that value instead of the auto-detected path.

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
    "message_count": 42,
    "has_errors": false
  }
]
```

### `cline://sessions/{session_id}/chat`

Returns a cleaned-up chat log for a specific session:

```json
{
  "session_id": "a1b2c3d4-...",
  "messages": [
    {"role": "user", "text": "Refactor the auth module..."},
    {"role": "assistant", "text": "Here is the refactored code..."}
  ]
}
```

### `cline://agent/status`

Returns the current execution state of the Cline CLI worker manager:

```json
{
  "status": "RUNNING",
  "active_processes": 1,
  "pids": [12345]
}
```

Possible status values: `IDLE`, `RUNNING`, `AWAITING_INPUT`, `FAILED`.

---

## MCP Tools (Process Control)

### `list_cline_sessions`

**Description:** Fast lookup to get a parsed JSON array of all local Cline sessions,
sorted by timestamp (newest first).

**Parameters:** None.

```json
// Input
{}

// Output
[{"session_id": "uuid", "initial_task": "...", ...}]
```

### `command_cline_agent`

**Description:** Starts a new Cline task or resumes an existing one asynchronously
without blocking the MCP stdio pipe.

**Parameters:**

| Name               | Type   | Required | Description                           |
|--------------------|--------|----------|---------------------------------------|
| `prompt`           | string | Yes      | The prompt or instructions for Cline.   |
| `resume_session_id`| string | No       | Session ID to resume.                 |

```json
// Input
{
  "prompt": "Analyze the performance of the database module",
  "resume_session_id": "a1b2c3d4-..."
}

// Output
{
  "pid": 12345,
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

### Option A: `mcp_settings.json`

Create or edit `~/.hermes/mcp_settings.json`:

```json
{
  "mcpServers": {
    "cline-hermes-bridge": {
      "command": "python",
      "args": ["/Users/wgorter/Development/cline-hermes-bridge/server.py"],
      "env": {},
      "disabled": false,
      "timeout": 300
    }
  }
}
```

> **Windows path example:**
> ```json
> "args": ["C:\\Path\\To\\cline-hermes-bridge\\server.py"]
> ```

### Option B: `mcp_settings.yaml`

```yaml
mcpServers:
  cline-hermes-bridge:
    command: python
    args:
      - /Users/wgorter/Development/cline-hermes-bridge/server.py
    env: {}
    disabled: false
    timeout: 300
```

### Option C: Environment Variable for Custom History Path

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
| `history/` directory missing   | Returns `[]` for session lists; no crash.              |
| Corrupted / unreadable JSON    | Skips the file, logs silently.                       |
| `cline` CLI not in PATH        | Returns a structured JSON error from `command_cline_agent`. |
| Process not tracked by `pid`   | Returns `"error": "Process not tracked"` from `get_agent_runtime_status`. |
| Kill command fails             | Returns `"killed": false` with the exception message.  |

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
