"""
Cline Hermes Bridge - MCP Server
=================================
An MCP (Model Context Protocol) server that exposes Cline session history
as read-only resources and provides async process-control tools for
Hermes Agent to monitor and manage Cline CLI workers.
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
from dataclasses import dataclass

from mcp.server import Server
import mcp.server.stdio
from mcp.types import (
    TextContent,
    Tool,
)

# ---------------------------------------------------------------------------
# Subprocess / stdio helpers
# ---------------------------------------------------------------------------

async def _read_json_file_safe(path: Path) -> Optional[dict]:
    """Asynchronously read a JSON file, returning None on I/O or parse errors."""
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, path.read_text, encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
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
    """Return the Cline session-history directory for the current OS."""
    system = platform.system().lower()
    root_env = "APPDATA" if system == "win32" else "HOME"
    base = Path(os.environ.get(root_env, str(Path.home())))

    if system == "darwin":
        return base / ROOTS["darwin"] / HISTORY_SUBPATH
    if system == "win32":
        return base / ROOTS["win32"] / HISTORY_SUBPATH
    # Linux
    return base / ROOTS["linux"] / HISTORY_SUBPATH


# ---------------------------------------------------------------------------
# Session data models
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    session_id: str
    initial_task: str
    created_at: str  # ISO-8601


@dataclass
class SessionSummary:
    metadata: SessionMeta
    message_count: int
    has_errors: bool


# ---------------------------------------------------------------------------
# Session index builder
# ---------------------------------------------------------------------------

def _parse_filename_session_id(filename: str) -> Optional[str]:
    """Extract session ID from a filename like '<uuid>.json' or 'session_<uuid>.json'."""
    name = Path(filename).stem
    # Try direct UUID, then prefix patterns
    for pattern in [
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        r"^session_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        r"^chat_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    ]:
        m = re.match(pattern, name, re.IGNORECASE)
        if m:
            return m.group(1) if m.lastindex else name
    return None


def _extract_initial_task(session: dict) -> str:
    """Heuristic: grab the first user / human message."""
    messages = session.get("messages", session.get("history", []))
    for msg in messages:
        role = msg.get("role", "")
        if role in ("user", "human"):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content[:200]
            if isinstance(content, list):
                for item in content:
                    txt = item.get("text", "")
                    if txt and txt.strip():
                        return txt[:200]
    return ""


def build_session_index(history_dir: Path) -> List[SessionSummary]:
    """Walk *history_dir* and return a list of SessionSummary objects sorted newest-first."""
    summaries: List[SessionSummary] = []
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
                created = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
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

        summaries.append(SessionSummary(
            metadata=SessionMeta(
                session_id=sid,
                initial_task=initial_task,
                created_at=str(created),
            ),
            message_count=msg_count,
            has_errors=err_flag,
        ))

    summaries.sort(key=lambda s: s.metadata.created_at, reverse=True)
    return summaries


# ---------------------------------------------------------------------------
# Runtime-process tracker
# ---------------------------------------------------------------------------

@dataclass
class RuntimeEntry:
    pid: int
    started_at: float
    alive: bool
    stdout_buf: str = ""
    stderr_buf: str = ""
    return_code: Optional[int] = None


_RUNTIME: Dict[int, RuntimeEntry] = {}


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# MCP Server instance
# ---------------------------------------------------------------------------

server = Server("cline-hermes-bridge")


# ---------------------------------------------------------------------------
# Register tools and resources
# ---------------------------------------------------------------------------

def _register_tools() -> None:
    """Register all MCP tools and resources with the server."""

    @server.call_tool()
    async def _call_handler(name: str, arguments: Any) -> List[TextContent]:
        if name == "list_cline_sessions":
            history = resolve_cline_history_dir()
            summaries = build_session_index(history)
            result = [
                {
                    "session_id": s.metadata.session_id,
                    "initial_task": s.metadata.initial_task,
                    "created_at": s.metadata.created_at,
                    "message_count": s.message_count,
                    "has_errors": s.has_errors,
                }
                for s in summaries
            ]
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "command_cline_agent":
            prompt: str = arguments.get("prompt", "")
            resume_id: Optional[str] = arguments.get("resume_session_id")
            if not prompt:
                return [TextContent(type="text", text=json.dumps({"error": "Parameter 'prompt' is required."}))]

            try:
                pid = await _spawn_cline_task(prompt, resume_id)
            except RuntimeError as exc:
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

            return [TextContent(
                type="text",
                text=json.dumps({"pid": pid, "status": "spawning", "prompt_length": len(prompt)}),
            )]

        if name == "get_agent_runtime_status":
            pid: int = int(arguments.get("pid", 0))
            return await _query_runtime_status(pid)

        if name == "stop_cline_agent":
            pid: int = int(arguments.get("pid", 0))
            return await _kill_cline_task(pid)

        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    @server.read_resource()
    async def _resource_handler(uri: str) -> str:
        if uri == "cline://sessions":
            history = resolve_cline_history_dir()
            summaries = build_session_index(history)
            result = [
                {
                    "session_id": s.metadata.session_id,
                    "initial_task": s.metadata.initial_task,
                    "created_at": s.metadata.created_at,
                    "message_count": s.message_count,
                    "has_errors": s.has_errors,
                }
                for s in summaries
            ]
            return json.dumps(result, indent=2)

        m = re.match(r"cline://sessions/([^/]+)/chat", uri)
        if m:
            session_id = m.group(1)
            history = resolve_cline_history_dir()

            # Try direct path first
            session = await _read_json_file_safe(history / f"{session_id}.json")
            if session is None:
                # Try alternate names
                for candidate in [f"chat_{session_id}.json", f"session_{session_id}.json",
                                  f"{session_id}.json"]:
                    alt = history / candidate
                    if alt.exists():
                        session = await _read_json_file_safe(alt)
                        if session is not None:
                            break

            if session is None:
                raise ValueError(f"No session found with id '{session_id}'")

            messages = session.get("messages", session.get("history", []))
            chat_log: List[Dict[str, str]] = []
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    chat_log.append({"role": role, "text": content.strip()[:4096]})
                elif isinstance(content, list):
                    for item in content:
                        txt = item.get("text", "")
                        if txt and txt.strip():
                            chat_log.append({"role": role, "text": txt.strip()[:4096]})

            return json.dumps({"session_id": session_id, "messages": chat_log}, indent=2)

        if uri == "cline://agent/status":
            active = [pid for e in _RUNTIME.values() if e.alive]
            return json.dumps({
                "status": "RUNNING" if active else "IDLE",
                "active_processes": len(active),
                "pids": active,
            })

        raise ValueError(f"Unknown resource URI: {uri}")


# ---------------------------------------------------------------------------
# Async subprocess helpers
# ---------------------------------------------------------------------------

async def _collect_stream(stream: asyncio.StreamReader, entry: RuntimeEntry, label: str) -> None:
    """Read stdout/stderr lines and accumulate into entry buffers."""
    try:
        async for line in iter(stream.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            if label == "stdout":
                entry.stdout_buf = entry.stdout_buf[-2048:] + text + "\n"
            else:
                entry.stderr_buf = entry.stderr_buf[-2048:] + text + "\n"
    except Exception:
        pass


async def _poll_liveness(proc: asyncio.subprocess.Process, entry: RuntimeEntry) -> None:
    """Keep entry alive while the process runs; mark finished when done."""
    await proc.wait()
    entry.alive = False
    entry.return_code = proc.returncode


async def _spawn_cline_task(prompt: str, resume_session_id: Optional[str] = None) -> int:
    """Spawn ``cline new`` or ``cline resume`` via asyncio; track PID in _RUNTIME."""
    if resume_session_id:
        cmd = ["cline", "resume", resume_session_id, "--prompt", prompt]
    else:
        cmd = ["cline", "new", prompt]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    entry = RuntimeEntry(pid=proc.pid, started_at=_now(), alive=True)
    _RUNTIME[proc.pid] = entry

    # Non-blocking stream collectors
    asyncio.ensure_future(_collect_stream(proc.stdout, entry, "stdout"))
    asyncio.ensure_future(_collect_stream(proc.stderr, entry, "stderr"))
    asyncio.ensure_future(_poll_liveness(proc, entry))

    return proc.pid


# ---------------------------------------------------------------------------
# Runtime status / control
# ---------------------------------------------------------------------------

async def _query_runtime_status(pid: int) -> List[TextContent]:
    if pid not in _RUNTIME:
        return [TextContent(type="text", text=json.dumps({
            "pid": pid, "alive": False, "error": "Process not tracked"
        }, indent=2))]

    entry = _RUNTIME[pid]
    rt: Dict[str, Any] = {
        "pid": pid,
        "alive": entry.alive,
        "started_at": datetime.fromtimestamp(entry.started_at, tz=timezone.utc).isoformat(),
        "runtime_seconds": round(_now() - entry.started_at, 1),
        "stdout_excerpt": entry.stdout_buf[-1024:] if entry.stdout_buf else "",
        "stderr_excerpt": entry.stderr_buf[-1024:] if entry.stderr_buf else "",
        "return_code": entry.return_code,
    }
    return [TextContent(type="text", text=json.dumps(rt, indent=2))]


async def _kill_cline_task(pid: int) -> List[TextContent]:
    """Kill a process via kill command; escalate to SIGKILL if SIGTERM fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kill", "-TERM", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        # Give it a moment; if still alive, escalate to SIGKILL
        await asyncio.sleep(0.5)

        entry = _RUNTIME.get(pid)
        if entry and entry.alive:
            await asyncio.create_subprocess_exec(
                "kill", "-KILL", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if entry:
                entry.alive = False

        if entry:
            entry.alive = False

        return [TextContent(type="text", text=json.dumps({
            "pid": pid, "killed": True, "status": "terminated"
        }, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({
            "pid": pid, "killed": False, "error": str(exc)
        }, indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_server() -> None:
    """Entry-point: run the MCP server over stdio."""
    _register_tools()

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.initialization_options())


if __name__ == "__main__":
    asyncio.run(run_server())
