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
import os
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.lowlevel.server import Server
import mcp.server.stdio
from mcp.types import CallToolResult, TextContent

# ---------------------------------------------------------------------------
# Subprocess / stdio helpers
# ---------------------------------------------------------------------------


async def _read_json_file_safe(path: Path) -> Optional[dict]:
    """Asynchronously read a JSON file, returning None on I/O or parse errors."""
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, path.read_text, "utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cline history path resolution
# ---------------------------------------------------------------------------

HISTORY_SUBPATH = "User/globalStorage/saoudrizwan.claude-dev/history"

ROOTS: Dict[str, str] = {
    "darwin": "Library/Application Support",
    "win32": r"AppData\Roaming",
    "linux": ".config",
}


def resolve_cline_history_dir() -> Path:
    """Return the Cline session-history directory for the current OS.

    Supports overriding via CLINE_HISTORY_DIR environment variable.
    """
    override = os.environ.get("CLINE_HISTORY_DIR")
    if override:
        return Path(override)

    system = platform.system().lower()
    root_env = "APPDATA" if system == "win32" else "HOME"
    base = Path(os.environ.get(root_env, str(Path.home())))

    if system == "darwin":
        return base / ROOTS["darwin"] / HISTORY_SUBPATH
    if system == "win32":
        return base / ROOTS["win32"] / HISTORY_SUBPATH
    return base / ROOTS["linux"] / HISTORY_SUBPATH


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
        r"^session_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        r"^chat_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    ]:
        m = re.match(pattern, name, re.IGNORECASE)
        if m:
            return m.group(1) if m.lastindex else name
    return None


def _build_session_index(history_dir: Path) -> List[Dict[str, Any]]:
    """Walk history_dir and return a list of session dicts sorted newest-first."""
    summaries: List[Dict[str, Any]] = []
    if not history_dir.is_dir():
        return summaries

    files = [f for f in history_dir.iterdir() if f.suffix == ".json"]

    for fp in files:
        session = asyncio.run(_read_json_file_safe(fp))
        if session is None:
            continue
        sid = _parse_filename_session_id(fp.name)
        if sid is None:
            sid = fp.stem

        created = session.get("createdAt", session.get("timestamp", ""))
        if isinstance(created, (int, float)):
            try:
                created = datetime.fromtimestamp(
                    created, tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError):
                created = ""

        initial_task = _extract_initial_task(session)

        msg_count = 0
        err_flag = False
        for m in session.get("messages", session.get("history", [])):
            role = m.get("role", "")
            if role in ("user", "human"):
                msg_count += 1
            if m.get("error") or m.get("isError"):
                err_flag = True

        summaries.append({
            "session_id": sid,
            "initial_task": initial_task,
            "created_at": str(created),
            "message_count": msg_count,
            "has_errors": err_flag,
        })

    summaries.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return summaries


# ---------------------------------------------------------------------------
# Runtime-process tracker
# ---------------------------------------------------------------------------

_RUNTIME: Dict[int, Dict[str, Any]] = {}
_RUNTIME_LOCK = asyncio.Lock()


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# MCP Server instance & handler registration
# ---------------------------------------------------------------------------

def _create_server() -> Server:
    """Create and fully configure the MCP server instance.

    Uses the mcp 2.x low-level API where callbacks are passed to
    Server.__init__ and invoked as handler(context, params).
    """

    # ---- Tool callbacks ----

    async def _on_call_tool(context, params) -> CallToolResult:
        """Handle tool call requests.

        params is a CallToolRequestParams with .name (str) and
        .arguments (dict or None).
        """
        name = params.name  # type: ignore[union-attr]
        arguments = params.arguments or {}  # type: ignore[union-attr]

        if name == "list_cline_sessions":
            try:
                result = _build_session_index(resolve_cline_history_dir())
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2)
                        )
                    ]
                )
            except Exception as exc:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)})
                    )],
                    is_error=True
                )

        if name == "command_cline_agent":
            prompt: str = arguments.get("prompt", "")
            resume_id: Optional[str] = arguments.get("resume_session_id")
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

            try:
                pid = await _spawn_cline_task(prompt, resume_id)
            except RuntimeError as exc:
                return CallToolResult(
                    content=[TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)})
                    )],
                    is_error=True
                )

            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({
                            "pid": pid,
                            "status": "spawning",
                            "prompt_length": len(prompt)
                        })
                    )
                ]
            )

        if name == "get_agent_runtime_status":
            pid: int = int(arguments.get("pid", 0))
            return await _query_runtime_status(pid)

        if name == "stop_cline_agent":
            pid: int = int(arguments.get("pid", 0))
            return await _kill_cline_task(pid)

        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"})
            )],
            is_error=True
        )

    async def _on_list_tools(context, params) -> List[Dict[str, Any]]:
        """Return the tool list that Cline will expose to clients."""
        return [
            {
                "name": "list_cline_sessions",
                "title": "list_cline_sessions",
                "description": (
                    "List all local Cline sessions sorted by newest first. "
                    "Returns session metadata."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "command_cline_agent",
                "title": "command_cline_agent",
                "description": (
                    "Start a new Cline task or resume an existing one "
                    "asynchronously. Returns the PID for status tracking."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The prompt or instructions to pass to Cline."
                            )
                        },
                        "resume_session_id": {
                            "type": "string",
                            "description": (
                                "Optional session ID to resume. If provided, "
                                "the task will continue that session."
                            )
                        }
                    },
                    "required": ["prompt"]
                }
            },
            {
                "name": "get_agent_runtime_status",
                "title": "get_agent_runtime_status",
                "description": (
                    "Check whether a specific Cline CLI task is active, "
                    "stalled, or finished. Returns process life status, "
                    "runtime, and stdout/stderr fragments."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": (
                                "The process ID returned by command_cline_agent."
                            )
                        }
                    },
                    "required": ["pid"]
                }
            },
            {
                "name": "stop_cline_agent",
                "title": "stop_cline_agent",
                "description": (
                    "Forcefully terminate a running Cline subprocess. "
                    "Sends SIGTERM, escalates to SIGKILL if unresponsive."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": "The process ID to terminate."
                        }
                    },
                    "required": ["pid"]
                }
            }
        ]

    async def _on_list_resources(context, params) -> List[Dict[str, Any]]:
        return [
            {
                "uri": "cline://sessions",
                "name": "cline://sessions",
                "description": "List of all Cline session URIs."
            }
        ]

    async def _on_read_resource(context, params) -> List[Any]:
        """Handle resource read requests.

        params is a ReadResourceRequestParams with attribute .uri.
        """
        uri = params.uri  # type: ignore[union-attr]

        if uri == "cline://sessions":
            result = _build_session_index(resolve_cline_history_dir())
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        m = re.match(r"cline://sessions/([^/]+)/chat", uri)
        if m:
            session_id = m.group(1)
            history = resolve_cline_history_dir()

            session = await _read_json_file_safe(
                history / f"{session_id}.json")
            if session is None:
                for candidate in [
                    f"chat_{session_id}.json",
                    f"session_{session_id}.json",
                ]:
                    alt = history / candidate
                    if alt.exists():
                        session = await _read_json_file_safe(alt)
                        if session is not None:
                            break

            if session is None:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"No session found with id '{session_id}'"
                    })
                )]

            messages = session.get("messages", session.get("history", []))
            chat_log: List[Dict[str, str]] = []
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    chat_log.append({
                        "role": role,
                        "text": content.strip()[:4096]
                    })
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            txt = item.get("text", "")
                            if txt and txt.strip():
                                chat_log.append({
                                    "role": role,
                                    "text": txt.strip()[:4096]
                                })

            return [TextContent(
                type="text",
                text=json.dumps(
                    {
                        "session_id": session_id,
                        "messages": chat_log
                    },
                    indent=2
                )
            )]

        if uri == "cline://agent/status":
            active = []
            async with _RUNTIME_LOCK:
                for entry in _RUNTIME.values():
                    if entry.get("alive"):
                        active.append(entry["pid"])
            status = "RUNNING" if active else "IDLE"
            return [TextContent(
                type="text",
                text=json.dumps({
                    "status": status,
                    "active_processes": len(active),
                    "pids": active
                })
            )]

        raise ValueError(f"Unknown resource URI: {uri}")

    # ---- Build server ----

    srv = Server(
        name="cline-hermes-bridge",
        version="1.0.0",
        description=(
            "MCP bridge exposing Cline session history and process "
            "control. Allows Hermes Agent to monitor and manage Cline "
            "CLI workers."
        ),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )

    return srv


# ---------------------------------------------------------------------------
# Async subprocess helpers
# ---------------------------------------------------------------------------


async def _collect_stream(
    stream: asyncio.StreamReader,
    entry: Dict[str, Any],
    label: str,
) -> None:
    """Read stdout/stderr lines and accumulate into entry buffers."""
    try:
        async for line in iter(stream.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            key = f"{label}_buf"
            existing = entry.get(key, "")
            entry[key] = (existing + text + "\n")[-2048:]
    except Exception:
        pass


async def _poll_liveness(
    proc: asyncio.subprocess.Process,
    entry: Dict[str, Any],
) -> None:
    """Keep entry alive while process runs; mark finished when done."""
    await proc.wait()
    entry["alive"] = False
    entry["return_code"] = proc.returncode


async def _spawn_cline_task(
    prompt: str,
    resume_session_id: Optional[str] = None,
) -> int:
    """Spawn ``cline new`` or ``cline resume`` via asyncio; track PID."""
    if resume_session_id:
        cmd = ["cline", "resume", resume_session_id, "--prompt", prompt]
    else:
        cmd = ["cline", "new", prompt]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    entry = {
        "pid": proc.pid,
        "started_at": _now(),
        "alive": True,
        "stdout_buf": "",
        "stderr_buf": "",
        "return_code": None,
    }
    async with _RUNTIME_LOCK:
        _RUNTIME[proc.pid] = entry

    asyncio.ensure_future(_collect_stream(proc.stdout, entry, "stdout"))
    asyncio.ensure_future(_collect_stream(proc.stderr, entry, "stderr"))
    asyncio.ensure_future(_poll_liveness(proc, entry))

    return proc.pid


# ---------------------------------------------------------------------------
# Runtime status / control
# ---------------------------------------------------------------------------


async def _query_runtime_status(pid: int) -> CallToolResult:
    """Query the runtime status of a tracked Cline process."""
    if pid not in _RUNTIME:
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=json.dumps({
                    "pid": pid,
                    "alive": False,
                    "error": "Process not tracked"
                }, indent=2)
            )]
        )

    entry = _RUNTIME[pid]
    rt: Dict[str, Any] = {
        "pid": pid,
        "alive": entry["alive"],
        "started_at": datetime.fromtimestamp(
            entry["started_at"], tz=timezone.utc
        ).isoformat(),
        "runtime_seconds": round(_now() - entry["started_at"], 1),
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
        proc = await asyncio.create_subprocess_exec(
            "kill", "-TERM", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        await asyncio.sleep(0.5)

        async with _RUNTIME_LOCK:
            entry = _RUNTIME.get(pid)
            if entry and entry.get("alive"):
                await asyncio.create_subprocess_exec(
                    "kill", "-KILL", str(pid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if entry:
                    entry["alive"] = False
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
# Entry point
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
