"""
Cline Hermes Bridge - MCP Server
=================================
An MCP (Model Context Protocol) server that exposes Cline session history
as read-only resources and provides async process-control tools for
Hermes Agent to monitor and manage Cline CLI workers.

Uses the mcp 2.x low-level API (constructor callback registration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.types import CallToolResult, TextContent, TextResourceContents

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)
_logger.addHandler(logging.StreamHandler())
_logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STDOUT_BUFFER_MAX = 2048
PROMPT_MAX_LENGTH = 4096
KILL_GRACE_PERIOD = 0.5
MAX_CLINE_RETRIES = 3
STATUS_EXCERPT_MAX = 1024
CHAT_TEXT_MAX = 4096

# ---------------------------------------------------------------------------
# Subprocess / stdio helpers
# ---------------------------------------------------------------------------


async def _read_json_file_safe(path: Path) -> Optional[Any]:
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

    Supports overriding via CLINE_HISTORY_DIR environment variable.
    """
    override = os.environ.get("CLINE_HISTORY_DIR")
    if override:
        return Path(override)

    system = platform.system().lower()
    if system == "win32":
        return (
            Path(os.environ.get("APPDATA", str(Path.home())))
            / ".cline"
            / "data"
            / "sessions"
        )
    return Path(os.environ.get("HOME", str(Path.home()))) / ".cline" / "data" / "sessions"


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _extract_text_fragments(content: Any) -> List[str]:
    """Normalize Cline message content into plain text fragments."""
    fragments: List[str] = []
    if isinstance(content, str):
        if content.strip():
            fragments.append(content.strip()[:CHAT_TEXT_MAX])
        return fragments

    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            txt = item.get("text")
            if isinstance(txt, str) and txt.strip():
                fragments.append(txt.strip()[:CHAT_TEXT_MAX])
                continue
            # Tool-result style blocks may nest content as string.
            nested = item.get("content")
            if isinstance(nested, str) and nested.strip():
                fragments.append(nested.strip()[:CHAT_TEXT_MAX])
    return fragments


def _extract_initial_task(session: dict) -> str:
    """Heuristic: grab the first user / human message content."""
    messages = session.get("messages", session.get("history", []))
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role in ("user", "human"):
            parts = _extract_text_fragments(msg.get("content", ""))
            if parts:
                return parts[0][:200]
    return ""


def _parse_created_at(metadata: dict, session_dir: Path) -> str:
    """Prefer metadata timestamps; fall back to directory mtime."""
    for key in ("started_at", "created_at", "ended_at"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        return datetime.fromtimestamp(
            session_dir.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        return datetime.now(tz=timezone.utc).isoformat()


def _session_meta_path(session_dir: Path) -> Path:
    return session_dir / f"{session_dir.name}.json"


def _session_messages_path(session_dir: Path) -> Path:
    return session_dir / f"{session_dir.name}.messages.json"


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

# Maps tracked PIDs to their runtime metadata.
_RUNTIME: Dict[int, Dict[str, Any]] = {}
_RUNTIME_LOCK = asyncio.Lock()


def _now() -> float:
    """Current UTC timestamp as a float."""
    return time.time()


def _process_exists(pid: int) -> bool:
    """Return True if the OS still has a process with this PID."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it.
        return True
    except OSError:
        return False


def _aggregate_status() -> Dict[str, Any]:
    """Compute aggregate status from active runtime entries."""
    active = [pid for pid, info in _RUNTIME.items() if info.get("alive")]
    return {
        "status": "RUNNING" if active else "IDLE",
        "total_sessions": len(_RUNTIME),
        "active_processes": len(active),
        "pids": active,
        "sessions": {
            str(pid): {
                "alive": info.get("alive"),
                "session_id": info.get("session_id"),
                "runtime_seconds": round(
                    _now() - info.get("started_at", _now()), 1
                ),
            }
            for pid, info in _RUNTIME.items()
        },
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _get_session_list() -> CallToolResult:
    """Build a sorted list of all Cline CLI sessions with metadata."""
    history_dir = resolve_cline_history_dir()
    if not history_dir.is_dir():
        _logger.warning("Cline history directory not found: %s", history_dir)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps([], indent=2))]
        )

    async with _RUNTIME_LOCK:
        runtime_by_session = {
            info.get("session_id"): info["pid"]
            for info in _RUNTIME.values()
            if info.get("session_id") and info.get("alive")
        }

    sessions: List[dict] = []
    try:
        entries = sorted(
            history_dir.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({"error": str(exc)}, indent=2),
            )],
            is_error=True,
        )

    for entry in entries:
        if not entry.is_dir():
            continue
        metadata = await _read_json_file_safe(_session_meta_path(entry))
        if not isinstance(metadata, dict):
            continue

        sid = str(metadata.get("session_id") or entry.name)
        prompt = metadata.get("prompt", metadata.get("initial_task", ""))
        if not isinstance(prompt, str) or not prompt.strip():
            # Fall back to messages file for older/empty metadata.
            messages_data = await _read_json_file_safe(_session_messages_path(entry))
            if isinstance(messages_data, dict):
                prompt = _extract_initial_task(messages_data)
            else:
                prompt = ""

        meta_pid = metadata.get("pid")
        tracked_pid = runtime_by_session.get(sid)
        status = str(metadata.get("status", "unknown")).lower()
        pid: Optional[int]
        if isinstance(tracked_pid, int):
            pid = tracked_pid
        elif (
            isinstance(meta_pid, int)
            and status in {"running", "active", "in_progress", "spawning"}
            and _process_exists(meta_pid)
        ):
            # Only trust metadata PIDs while the session claims to be active;
            # idle/completed metadata can still contain stale PIDs that were
            # reused by unrelated processes (e.g. the Cline hub daemon).
            pid = meta_pid
        else:
            pid = None

        sessions.append({
            "session_id": sid,
            "initial_task": prompt.strip()[:200],
            "created_at": _parse_created_at(metadata, entry),
            "status": metadata.get("status", "unknown"),
            "interactive": bool(metadata.get("interactive", False)),
            "pid": pid,
        })

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(sessions, indent=2))]
    )


async def _spawn_cline_task(
    prompt: str,
    resume_session_id: str = "",
) -> CallToolResult:
    """Create a new Cline CLI session or resume an existing one."""
    if not prompt:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({"error": "Parameter 'prompt' is required."}),
            )],
            is_error=True,
        )

    if len(prompt) > PROMPT_MAX_LENGTH:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": (
                        f"Prompt exceeds maximum length of "
                        f"{PROMPT_MAX_LENGTH} characters."
                    ),
                }),
            )],
            is_error=True,
        )

    if resume_session_id and len(resume_session_id) > PROMPT_MAX_LENGTH:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({"error": "Session ID exceeds maximum length."}),
            )],
            is_error=True,
        )

    if resume_session_id:
        history_dir = resolve_cline_history_dir()
        session_dir = history_dir / resume_session_id
        if not session_dir.is_dir():
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Session not found: {resume_session_id}"
                    }),
                )],
                is_error=True,
            )

    if shutil.which("cline") is None:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "error",
                    "error": (
                        "cline CLI not found in PATH. "
                        "Install with: npm install -g @anthropic/cline"
                    ),
                }),
            )],
            is_error=True,
        )

    if resume_session_id:
        cmd = ["cline", "--id", resume_session_id, prompt]
    else:
        cmd = ["cline", prompt]

    last_error: Optional[Exception] = None
    proc: Optional[asyncio.subprocess.Process] = None
    for attempt in range(MAX_CLINE_RETRIES):
        try:
            _logger.debug("Attempt %d: spawning %s", attempt + 1, cmd)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            break
        except Exception as exc:
            last_error = exc
            _logger.warning("Attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(1)

    if proc is None or proc.pid is None:
        detail = str(last_error) if last_error else "unknown error"
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "error": (
                        f"Failed to start Cline CLI after "
                        f"{MAX_CLINE_RETRIES} attempts: {detail}"
                    ),
                }),
            )],
            is_error=True,
        )

    pid = int(proc.pid)

    async def _read_stream(
        stream: Optional[asyncio.StreamReader], buf_key: str
    ) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                async with _RUNTIME_LOCK:
                    entry = _RUNTIME.get(pid)
                    if entry is not None:
                        buf = entry.get(buf_key, "") or ""
                        entry[buf_key] = (buf + text)[-STDOUT_BUFFER_MAX:]
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception:
                break

    async def _collect_streams() -> None:
        await asyncio.gather(
            _read_stream(proc.stdout, "stdout_buf"),
            _read_stream(proc.stderr, "stderr_buf"),
        )
        return_code = await proc.wait()
        async with _RUNTIME_LOCK:
            entry = _RUNTIME.get(pid)
            if entry is not None:
                entry["alive"] = False
                entry["return_code"] = return_code

    async with _RUNTIME_LOCK:
        _RUNTIME[pid] = {
            "pid": pid,
            "alive": True,
            "started_at": _now(),
            "stdout_buf": "",
            "stderr_buf": "",
            "prompt": prompt[:200],
            "session_id": resume_session_id or None,
            "return_code": None,
        }

    asyncio.create_task(_collect_streams())

    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps({
                "pid": pid,
                "status": "spawning",
                "prompt_length": len(prompt),
                "resume_session_id": resume_session_id or None,
            }, indent=2),
        )]
    )


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
                        "error": "Process not tracked",
                    }, indent=2),
                )],
                is_error=True,
            )
        # Refresh liveness against the OS if our bookkeeping still says alive.
        if entry.get("alive") and not _process_exists(pid):
            entry["alive"] = False

        started_at = entry.get("started_at")
        rt: Dict[str, Any] = {
            "pid": pid,
            "alive": bool(entry.get("alive")),
            "session_id": entry.get("session_id"),
            "started_at": (
                datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
                if isinstance(started_at, (int, float)) and started_at > 0
                else None
            ),
            "runtime_seconds": (
                round(_now() - started_at, 1)
                if isinstance(started_at, (int, float)) and started_at > 0
                else 0
            ),
            "stdout_excerpt": (entry.get("stdout_buf", "") or "")[-STATUS_EXCERPT_MAX:],
            "stderr_excerpt": (entry.get("stderr_buf", "") or "")[-STATUS_EXCERPT_MAX:],
            "return_code": entry.get("return_code"),
        }

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(rt, indent=2))]
    )


async def _kill_cline_task(pid: int) -> CallToolResult:
    """Terminate a tracked Cline process with SIGTERM → SIGKILL escalation."""
    async with _RUNTIME_LOCK:
        entry = _RUNTIME.get(pid)
        if entry is None:
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "pid": pid,
                        "killed": False,
                        "error": "Process not tracked",
                    }, indent=2),
                )],
                is_error=True,
            )

    try:
        if _process_exists(pid):
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(KILL_GRACE_PERIOD)
            if _process_exists(pid):
                os.kill(pid, signal.SIGKILL)
                await asyncio.sleep(0.1)

        async with _RUNTIME_LOCK:
            tracked = _RUNTIME.get(pid)
            if tracked is not None:
                tracked["alive"] = False
                if tracked.get("return_code") is None:
                    tracked["return_code"] = -signal.SIGTERM

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "killed": True,
                    "status": "terminated",
                }, indent=2),
            )]
        )
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "killed": False,
                    "error": str(exc),
                }, indent=2),
            )],
            is_error=True,
        )


async def _read_session_chat(session_id: str) -> Dict[str, Any]:
    """Load and normalize a session chat log."""
    history_dir = resolve_cline_history_dir()
    chat_path = history_dir / session_id / f"{session_id}.messages.json"
    if not chat_path.exists():
        alt = history_dir / f"{session_id}.messages.json"
        if alt.exists():
            chat_path = alt

    chat_data = await _read_json_file_safe(chat_path)
    if chat_data is None:
        return {
            "error": f"Chat log not found for session {session_id}",
        }

    if isinstance(chat_data, list):
        messages = chat_data
    elif isinstance(chat_data, dict):
        messages = chat_data.get("messages", chat_data.get("history", []))
    else:
        messages = []
    if not isinstance(messages, list):
        messages = []

    chat_log: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "unknown")
        for text in _extract_text_fragments(msg.get("content", "")):
            chat_log.append({"role": role, "text": text})

    return {
        "session_id": session_id,
        "total_messages": len(messages),
        "chat_log": chat_log,
    }


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def _tool_definitions() -> List[types.Tool]:
    return [
        types.Tool(
            name="list_cline_sessions",
            description=(
                "List all Cline CLI sessions sorted newest first. "
                "Returns session metadata including initial task, status, "
                "created_at, and process ID."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="command_cline_agent",
            description=(
                "Start a new Cline CLI session or resume an existing one "
                "by its session ID. Returns the new process PID and status."
            ),
            inputSchema={
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
        types.Tool(
            name="get_agent_runtime_status",
            description=(
                "Check whether a tracked Cline CLI process is still alive. "
                "Returns the PID, alive flag, start time, runtime duration, "
                "and stdout/stderr excerpts."
            ),
            inputSchema={
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
        types.Tool(
            name="stop_cline_agent",
            description=(
                "Terminate a tracked Cline CLI subprocess. Sends SIGTERM and "
                "escalates to SIGKILL after a brief grace period if needed."
            ),
            inputSchema={
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


def _create_server() -> Server:
    """Build the MCP server with mcp 2.x constructor callbacks."""

    async def on_list_tools(
        ctx: Any, params: Optional[types.PaginatedRequestParams]
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions())

    async def on_call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}

        if name == "list_cline_sessions":
            return await _get_session_list()

        if name == "command_cline_agent":
            prompt = str(arguments.get("prompt", "") or "")
            resume = str(arguments.get("resume_session_id", "") or "")
            return await _spawn_cline_task(prompt=prompt, resume_session_id=resume)

        if name == "get_agent_runtime_status":
            try:
                pid = int(arguments.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            return await _query_runtime_status(pid)

        if name == "stop_cline_agent":
            try:
                pid = int(arguments.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            return await _kill_cline_task(pid)

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2),
            )],
            is_error=True,
        )

    async def on_list_resources(
        ctx: Any, params: Optional[types.PaginatedRequestParams]
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri="cline://agent/status",
                    name="Agent Runtime Status",
                    description="Current status of all tracked Cline CLI processes.",
                    mimeType="application/json",
                ),
                types.Resource(
                    uri="cline://sessions",
                    name="Cline Sessions",
                    description="List of all Cline CLI sessions.",
                    mimeType="application/json",
                ),
            ]
        )

    async def on_list_resource_templates(
        ctx: Any, params: Optional[types.PaginatedRequestParams]
    ) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    uriTemplate="cline://sessions/{id}/chat",
                    name="Cline Session Chat Log",
                    description="Chat log for a specific Cline CLI session.",
                    mimeType="application/json",
                )
            ]
        )

    async def on_read_resource(
        ctx: Any, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        uri_stripped = uri.rstrip("/")

        if uri_stripped in ("cline://agent/status", "cline://agent/status/"):
            payload = _aggregate_status()
            return types.ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri="cline://agent/status",
                        mimeType="application/json",
                        text=json.dumps(payload, indent=2),
                    )
                ]
            )

        if uri_stripped in ("cline://sessions", "cline://sessions/"):
            result = await _get_session_list()
            text = result.content[0].text if result.content else "[]"
            return types.ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri="cline://sessions",
                        mimeType="application/json",
                        text=text,
                    )
                ]
            )

        match = re.match(r"cline://sessions/([^/]+)/chat/?$", uri_stripped)
        if match:
            session_id = match.group(1)
            payload = await _read_session_chat(session_id)
            return types.ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=f"cline://sessions/{session_id}/chat",
                        mimeType="application/json",
                        text=json.dumps(payload, indent=2),
                    )
                ]
            )

        raise ValueError(f"Unknown resource URI: {uri}")

    return Server(
        name="cline-hermes-bridge",
        version="1.1.0",
        description=(
            "MCP bridge exposing Cline CLI session history and process "
            "control for Hermes Agent."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
    )


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
