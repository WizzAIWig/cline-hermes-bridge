"""
Cline Hermes Bridge - MCP Server
=================================
An MCP (Model Context Protocol) server that exposes Cline session history
as read-only resources and provides async process-control tools for
Hermes Agent to monitor and manage Cline CLI workers.

Uses the mcp 2.x low-level API (callback-based handler registration).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import mcp.server.stdio
from mcp.server.lowlevel.server import Server
import mcp_types as mcp_types_module
from mcp.types import CallToolResult, TextContent, TextResourceContents

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)
_logger.addHandler(logging.StreamHandler())
_logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max trailing characters to retain from stdout/stderr buffers.
STDOUT_BUFFER_MAX = 2048
# Maximum allowed prompt length (characters).
PROMPT_MAX_LENGTH = 4096
# Grace period before escalating SIGTERM → SIGKILL (seconds).
KILL_GRACE_PERIOD = 0.5
# Maximum retry attempts for spawning Cline CLI.
MAX_CLINE_RETRIES = 3


# ---------------------------------------------------------------------------
# Subprocess / stdio helpers
# ---------------------------------------------------------------------------


async def _read_json_file_safe(path: Path) -> Optional[dict]:
    """Asynchronously read a JSON file, returning None on I/O or parse errors."""
    try:
        text = await asyncio.get_running_loop().run_in_executor(
            None, path.read_text, "utf-8"
        )
        return json.loads(text)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cline history path resolution (CLI sessions: ~/.cline/data/sessions)
# ---------------------------------------------------------------------------


def resolve_cline_history_dir() -> Path:
    """Return the Cline CLI sessions directory for the current OS.

    Cline CLI stores each session as a directory under ~/.cline/data/sessions/.
    Each session directory contains a <session_id>.json metadata file and
    a <session_id>.messages.json file.

    Supports overriding via CLINE_HISTORY_DIR environment variable.
    """
    override = os.environ.get("CLINE_HISTORY_DIR")
    if override:
        return Path(override)

    system = platform.system().lower()
    if system == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / ".cline" / "data" / "sessions"
    return Path(os.environ.get("HOME", str(Path.home()))) / ".cline" / "data" / "sessions"


# ---------------------------------------------------------------------------
# Session data models
# ---------------------------------------------------------------------------


def _extract_initial_task(session: dict) -> str:
    """Heuristic: grab the first user / human message content."""
    messages = session.get("messages", session.get("history", []))
    for msg in messages:
        role = msg.get("role", "")
        if role in ("user", "human"):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content[:200]
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        txt = item.get("text", "")
                        if txt and txt.strip():
                            return txt[:200]
    return ""


def _parse_filename_session_id(filename: str) -> Optional[str]:
    """Extract session ID from a filename like '<uuid>.json'."""
    name = Path(filename).stem
    for pattern in [
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        r"^session_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        r"^chat_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    ]:
        m = re.match(pattern, name)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    # Fallback: use filename without extension as session ID.
    if re.match(r"^[0-9a-fA-F]+$", name):
        return name
    return None


def _parse_session_file_datetime(filename: str) -> datetime:
    """Return last-modified time as a fallback 'created_at' timestamp."""
    return datetime.fromtimestamp(
        Path(filename).stat().st_mtime, tz=timezone.utc
    )


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

# Maps tracked PIDs to their runtime metadata.
_RUNTIME: Dict[int, Dict[str, Any]] = {}
_RUNTIME_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Internal time helper (UTC epoch seconds)
# ---------------------------------------------------------------------------

def _now() -> float:
    """Current UTC timestamp as a float."""
    return time.time()


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def _create_server() -> Server:
    """Build the MCP server and attach all tool / resource handlers.

    Uses the MCP 2.x constructor-based API: callbacks are passed as named
    keyword arguments (on_call_tool, on_list_tools, on_list_resources,
    on_read_resource) and invoked by the runner.
    """
    # ---- Tool callbacks ----

    async def _on_call_tool(
        name: str, arguments: Optional[dict]
    ) -> CallToolResult:
        if name == "list_cline_sessions":
            return await _get_session_list()

        if name == "command_cline_agent":
            return await _spawn_cline_task(
                prompt=arguments["prompt"] if arguments else "",
                resume_session_id=(
                    arguments.get("resume_session_id", "")
                    if arguments
                    else ""
                ),
            )

        if name == "get_agent_runtime_status":
            pid = int(arguments.get("pid", 0)) if arguments else 0
            return await _query_runtime_status(pid)

        if name == "stop_cline_agent":
            pid = int(arguments.get("pid", 0)) if arguments else 0
            return await _kill_cline_task(pid)

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Unknown tool: {name}"
                }, indent=2)
            )],
            is_error=True
        )

    async def _on_list_tools() -> List[mcp_types_module.Tool]:
        """Return the tool list that Cline will expose to clients."""
        return [
            mcp_types_module.Tool(
                name="list_cline_sessions",
                description=(
                    "List all Cline CLI sessions sorted newest first. "
                    "Returns session metadata including initial task, status, "
                    "created_at, and process ID."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            mcp_types_module.Tool(
                name="command_cline_agent",
                description=(
                    "Start a new Cline CLI session or resume an existing one "
                    "by its session ID. Returns the new process PID and status."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The task prompt for Cline CLI. "
                                "Mandatory – must be a non-empty string."
                            ),
                        },
                        "resume_session_id": {
                            "type": "string",
                            "description": (
                                "Optional session ID to resume. "
                                "When omitted a fresh session is created."
                            ),
                        },
                    },
                    "required": ["prompt"],
                },
            ),
            mcp_types_module.Tool(
                name="get_agent_runtime_status",
                description=(
                    "Check whether a tracked Cline CLI process is still "
                    "alive. Returns the PID, alive flag, start time, "
                    "runtime duration, and stdout/stderr excerpts."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": "PID as returned by command_cline_agent.",
                        },
                    },
                    "required": ["pid"],
                },
            ),
            mcp_types_module.Tool(
                name="stop_cline_agent",
                description=(
                    "Terminate a running Cline CLI subprocess via the "
                    "kill command. Sends SIGTERM and escalates to SIGKILL "
                    "after a brief grace period if needed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": "PID of the process to terminate.",
                        },
                    },
                    "required": ["pid"],
                },
            ),
        ]

    async def _on_list_resources() -> List[Dict[str, Any]]:
        return [
            {
                "uri": "cline://agent/status",
                "name": "Agent Runtime Status",
                "description": "Current status of all tracked Cline CLI processes.",
                "mimeType": "application/json",
            },
            {
                "uri": "cline://sessions/{id}/chat",
                "name": "Cline Session Chat Log",
                "description": "Chat log for a specific Cline CLI session.",
                "mimeType": "application/json",
            },
        ]

    async def _on_read_resource(uri: str) -> Dict[str, Any]:
        """Handle resource reads for Cline session data and aggregate status."""
        # --- cline://agent/status ---
        uri_stripped = uri.rstrip("/")
        if uri_stripped in ("cline://agent/status", "cline://agent/status/"):
            return {"contents": [TextResourceContents(uri="cline://agent/status", mime_type="application/json", text=json.dumps(_aggregate_status(), indent=2))]}

        # --- cline://sessions/{id}/chat ---
        m = re.match(r"cline://sessions/([^/]+)/chat", uri)
        if m:
            session_id = m.group(1)
            history_dir = resolve_cline_history_dir()
            session_dir = history_dir / session_id
            chat_path = session_dir / f"{session_id}.messages.json"

            # Fallback: try with session_id as directory name
            if not chat_path.exists():
                alt_path = history_dir / f"{session_id}.messages.json"
                if alt_path.exists():
                    chat_path = alt_path

            chat_data = await _read_json_file_safe(chat_path)
            if chat_data is None:
                return {"contents": [TextResourceContents(uri=f"cline://sessions/{session_id}/chat", mime_type="application/json", text=json.dumps({
                        "error": f"Chat log not found for session {session_id}"
                    }))]}

            chat_log: List[Dict[str, Any]] = []
            messages = chat_data.get("messages", chat_data.get("history", []))
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    chat_log.append({
                        "role": role,
                        "text": content.strip()[:4096],
                    })
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            txt = item.get("text", "")
                            if txt and txt.strip():
                                chat_log.append({
                                    "role": role,
                                    "text": txt.strip()[:4096],
                                })

            return {"contents": [TextResourceContents(uri=f"cline://sessions/{session_id}/chat", mime_type="application/json", text=json.dumps({
                    "session_id": session_id,
                    "total_messages": len(messages),
                    "chat_log": chat_log
                }, indent=2))]}

        # --- Unknown URI ---
        raise ValueError(f"Unknown resource URI: {uri}")

    # ---- Build server ----

    srv = Server(
        name="cline-hermes-bridge",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )

    return srv


# ---------------------------------------------------------------------------
# Session resolution helper
# ---------------------------------------------------------------------------


async def _resolve_session_file(
    history_dir: Path, session_id: str
) -> Optional[Path]:
    """Locate a session metadata file by exact match or filename prefix."""
    # Exact match
    candidate = history_dir / f"{session_id}.json"
    if candidate.exists():
        return candidate

    # Prefix match: strip common prefixes
    clean = re.sub(r"^(session_|chat_)", "", session_id)
    candidate = history_dir / f"{clean}.json"
    if candidate.exists():
        return candidate

    return None


# ---------------------------------------------------------------------------
# Tool: list_cline_sessions
# ---------------------------------------------------------------------------


async def _get_session_list() -> CallToolResult:
    """Build a sorted list of all Cline CLI sessions with metadata."""

    history_dir = resolve_cline_history_dir()
    if not history_dir.is_dir():
        _logger.warning("Cline history directory not found: %s", history_dir)
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps([], indent=2)
            )]
        )

    sessions: List[dict] = []

    # Index active PIDs for enrichment.
    async with _RUNTIME_LOCK:
        pid_to_session: Dict[int, str] = {
            info["pid"]: sid for sid, info in _RUNTIME.items()
            if info.get("pid")
        }

    for entry in sorted(
        history_dir.iterdir(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if not entry.is_dir():
            continue
        metadata = await _read_json_file_safe(entry / f"{entry.name}.json")
        if metadata is None:
            metadata = await _read_json_file_safe(entry / f"{entry.name}/metadata.json")
            if metadata is None:
                continue

        sid = metadata.get("session_id", entry.name)
        initial_task = metadata.get("prompt", metadata.get("initial_task", ""))
        # _extract_initial_task is kept for backward compat but is not
        # used for the main listing (prompt field is used directly).
        status = metadata.get("status", "unknown")
        interactive = metadata.get("interactive", False)

        # Try to find a matching running PID.
        pid: Optional[int] = None
        for _pid, info in pid_to_session.items():
            if info.get("session_id") == sid:
                pid = _pid
                break

        sessions.append({
            "session_id": sid,
            "initial_task": initial_task[:200],
            "created_at": _parse_session_file_datetime(entry.name).isoformat(),
            "status": status,
            "interactive": interactive,
            "pid": pid,
        })

    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps(sessions, indent=2)
        )]
    )


# ---------------------------------------------------------------------------
# Tool: command_cline_agent (spawn / resume)
# ---------------------------------------------------------------------------


async def _spawn_cline_task(
    prompt: str,
    resume_session_id: str = "",
) -> CallToolResult:
    """Create a new Cline CLI session or resume an existing one."""

    if not prompt:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": "Parameter 'prompt' is required."
                })
            )],
            is_error=True
        )

    if len(prompt) > PROMPT_MAX_LENGTH:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Prompt exceeds maximum length of {PROMPT_MAX_LENGTH} characters."
                })
            )],
            is_error=True
        )

    pid = os.getpid()  # Placeholder — overwritten below

    # Validate resume_session_id if provided
    if resume_session_id:
        if len(resume_session_id) > PROMPT_MAX_LENGTH:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "error": "Session ID exceeds maximum length."
                    })
                )],
                is_error=True
            )

    # Verify Cline CLI is available before spawning
    proc = await asyncio.create_subprocess_exec(
        "cline", "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if proc.returncode != 0:
        pid = os.getpid()  # Still assign a value for the response
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "status": "error",
                    "error": "cline CLI not found in PATH. Install with: npm install -g @anthropic/cline"
                })
            )],
            is_error=True
        )

    # Build command
    if resume_session_id:
        cmd = ["cline", "--id", resume_session_id, prompt]
    else:
        cmd = ["cline", prompt]

    # Track across retries
    last_error: Optional[Exception] = None
    for attempt in range(MAX_CLINE_RETRIES):
        try:
            _logger.debug("Attempt %d: spawning cline with cmd: %s", attempt + 1, cmd)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            pid = proc.pid
            break
        except Exception as exc:
            last_error = exc
            _logger.warning(
                "Attempt %d failed: %s", attempt + 1, exc
            )
            await asyncio.sleep(1)

    else:
        if last_error:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Failed to start Cline CLI after {MAX_CLINE_RETRIES} attempts: {last_error}"
                    })
                )],
                is_error=True
            )
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": "Failed to start Cline CLI"
                })
            )],
            is_error=True
        )

    # Collect streams and detect process exit.
    async def _collect_streams() -> None:
        """Read stdout/stderr, update buffer, and mark process finished."""
        await asyncio.gather(
            _read_stream(proc.stdout, "stdout_buf"),
            _read_stream(proc.stderr, "stderr_buf"),
        )
        async with _RUNTIME_LOCK:
            entry = _RUNTIME.get(pid)
            if entry:
                entry["alive"] = False
                entry["return_code"] = proc.returncode

    async def _read_stream(stream: asyncio.StreamReader, buf_key: str) -> None:
        """Read one stream to EOF, appending to the buffer."""
        while True:
            try:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                async with _RUNTIME_LOCK:
                    entry = _RUNTIME.get(pid)
                    if entry:
                        buf = entry.get(buf_key, "") or ""
                        entry[buf_key] = (buf + text)[-STDOUT_BUFFER_MAX:]
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception:
                break

    # Register the process immediately so status queries don't show "not tracked"
    async with _RUNTIME_LOCK:
        _RUNTIME[pid] = {
            "pid": pid,
            "alive": True,
            "started_at": _now(),
            "stdout_buf": "",
            "stderr_buf": "",
            "prompt": prompt[:200],
            "return_code": None,
        }

    asyncio.create_task(_collect_streams())

    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps({
                "pid": pid,
                "status": "spawning",
                "prompt_length": len(prompt)
            }, indent=2)
        )]
    )


# ---------------------------------------------------------------------------
# Poll liveness — background task for completed processes
# ---------------------------------------------------------------------------


async def _poll_liveness(pid: int) -> None:
    """Check process liveness asynchronously. This is a no-op placeholder."""
    pass


# ---------------------------------------------------------------------------
# Runtime status / control
# ---------------------------------------------------------------------------


async def _query_runtime_status(pid: int) -> CallToolResult:
    """Query the runtime status of a tracked Cline process."""

    async with _RUNTIME_LOCK:
        entry = _RUNTIME.get(pid)

    if entry is None:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "alive": False,
                    "error": "Process not tracked"
                }, indent=2)
            )],
            is_error=True
        )

    rt: Dict[str, Any] = {
        "pid": pid,
        "alive": entry.get("alive", False),
        "started_at": datetime.fromtimestamp(
            entry.get("started_at", 0), tz=timezone.utc
        ).isoformat() if entry.get("started_at") else None,
        "runtime_seconds": round(_now() - entry.get("started_at", _now()), 1) if entry.get("started_at") else 0,
        "stdout_excerpt": entry.get("stdout_buf", "")[-1024:] or "",
        "stderr_excerpt": entry.get("stderr_buf", "")[-1024:] or "",
        "return_code": entry.get("return_code"),
    }
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps(rt, indent=2)
        )]
    )


async def _kill_cline_task(pid: int) -> CallToolResult:
    """Kill a process via kill command; escalate to SIGKILL if SIGTERM fails."""
    try:
        term_proc = await asyncio.create_subprocess_exec(
            "kill", "-TERM", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await term_proc.wait()

        await asyncio.sleep(KILL_GRACE_PERIOD)

        # Re-check liveness after grace period; escalate if still alive.
        still_alive = False
        async with _RUNTIME_LOCK:
            entry = _RUNTIME.get(pid)
            still_alive = entry and entry.get("alive")

        if still_alive:
            kill_proc = await asyncio.create_subprocess_exec(
                "kill", "-KILL", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await kill_proc.wait()

        async with _RUNTIME_LOCK:
            entry = _RUNTIME.get(pid)
            if entry:
                entry["alive"] = False

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "killed": True,
                    "status": "terminated"
                }, indent=2)
            )]
        )
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "killed": False,
                    "error": str(exc)
                }, indent=2)
            )],
            is_error=True
        )


# ---------------------------------------------------------------------------
# Resource handlers
# ---------------------------------------------------------------------------


async def _on_read_resource(uri: str) -> dict:
    """Handle resource reads for Cline session data and aggregate status."""
    # --- cline://agent/status ---
    uri_stripped = uri.rstrip("/")
    if uri_stripped in ("cline://agent/status", "cline://agent/status/"):
        return {"contents": [TextResourceContents(uri="cline://agent/status", mime_type="application/json", text=json.dumps(_aggregate_status(), indent=2))]}

    # --- cline://sessions/{id}/chat ---
    m = re.match(r"cline://sessions/([^/]+)/chat", uri)
    if m:
        session_id = m.group(1)
        history_dir = resolve_cline_history_dir()
        session_dir = history_dir / session_id
        chat_path = session_dir / f"{session_id}.messages.json"

        # Fallback: try with session_id as directory name
        if not chat_path.exists():
            alt_path = history_dir / f"{session_id}.messages.json"
            if alt_path.exists():
                chat_path = alt_path

        chat_data = await _read_json_file_safe(chat_path)
        if chat_data is None:
            return {"contents": [TextResourceContents(uri=f"cline://sessions/{session_id}/chat", mime_type="application/json", text=json.dumps({
                    "error": f"Chat log not found for session {session_id}"
                }))]}

        chat_log: List[Dict[str, Any]] = []
        messages = chat_data.get("messages", chat_data.get("history", []))
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                chat_log.append({
                    "role": role,
                    "text": content.strip()[:4096],
                })
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        txt = item.get("text", "")
                        if txt and txt.strip():
                            chat_log.append({
                                "role": role,
                                "text": txt.strip()[:4096],
                            })

        return {"contents": [TextResourceContents(uri=f"cline://sessions/{session_id}/chat", mime_type="application/json", text=json.dumps({
                "session_id": session_id,
                "total_messages": len(messages),
                "chat_log": chat_log
            }, indent=2))]}

    # --- Unknown URI ---
    raise ValueError(f"Unknown resource URI: {uri}")


# ---------------------------------------------------------------------------
# Aggregate status
# ---------------------------------------------------------------------------


def _aggregate_status() -> Dict[str, Any]:
    """Compute aggregate status from active runtime entries."""
    return {
        "total_sessions": len(_RUNTIME),
        "active_processes": sum(
            1 for info in _RUNTIME.values() if info.get("alive")
        ),
        "total_stdout_bytes": sum(
            len(info.get("stdout_buf", "")) for info in _RUNTIME.values()
        ),
        "total_stderr_bytes": sum(
            len(info.get("stderr_buf", "")) for info in _RUNTIME.values()
        ),
        "sessions": {
            str(pid): {
                "alive": info.get("alive"),
                "runtime_seconds": round(
                    _now() - info.get("started_at", 0), 1
                ),
            }
            for pid, info in _RUNTIME.items()
        },
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_server() -> None:
    """Entry-point: run the MCP server over stdio."""
    srv = _create_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await srv.run(
            read_stream,
            write_stream,
            srv.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(run_server())
