"""Tool registry, permission system, and sandboxed execution.

Design notes (MVP):
- Tools are explicit, named, and permission-checked before execution.
- Filesystem tools are confined to ``workspace_root``; path traversal is denied.
- ``exec.run`` never uses a shell (``shell=False``) and is denylisted for
  destructive patterns. It is high-risk and requires explicit approval.
- Tool outputs are redacted for secret-like patterns before being returned.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple


class ToolError(Exception):
    """Base class for tool failures."""


class PermissionDenied(ToolError):
    """Raised when a tool call violates the permission policy."""


class ApprovalRequired(ToolError):
    """Raised when a high-risk tool needs explicit human approval."""

    def __init__(self, tool_name: str):
        super().__init__(f"approval required for tool '{tool_name}'")
        self.tool_name = tool_name


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str = ""
    duration_ms: int = 0


@dataclass
class Tool:
    name: str
    description: str
    risk: str  # low | medium | high
    requires_approval: bool
    handler: Callable[[Dict[str, Any], Any], Any]


_REGISTRY: Dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ToolError(f"unknown tool '{name}'")


def list_tools():
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def tool(name: str, description: str, risk: str = "low", requires_approval: bool = False):
    def deco(fn: Callable[[Dict[str, Any], Any], Any]):
        register(Tool(name, description, risk, requires_approval, fn))
        return fn

    return deco


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9-_]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9_]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?"),
]


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    redacted = text
    for pat in _SECRET_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    return redacted


def _redact_output(output: Any) -> Any:
    if isinstance(output, str):
        return redact_secrets(output)
    if isinstance(output, dict):
        return {k: _redact_output(v) for k, v in output.items()}
    if isinstance(output, list):
        return [_redact_output(v) for v in output]
    return output


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def resolve_within_root(root: str, path: str) -> str:
    """Resolve ``path`` and ensure it stays inside ``root``."""
    root_real = os.path.realpath(root)
    candidate = path if os.path.isabs(path) else os.path.join(root_real, path or ".")
    real = os.path.realpath(candidate)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise PermissionDenied(f"path escapes workspace root: {path!r}")
    return real


def relative_to_root(root: str, real_path: str) -> str:
    root_real = os.path.realpath(root)
    return os.path.relpath(os.path.realpath(real_path), root_real)


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------

_PATH_ARGS = {"path", "report_path", "root", "cwd"}

_DENY_COMMAND = [
    re.compile(r":\(\)\s*\{", re.IGNORECASE),  # fork bomb
    re.compile(r"\brm\s+.*-\s*r", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.IGNORECASE),
    re.compile(r"\bdd\b.*\bof=\s*/dev/", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|hd|nvme)", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"\bwget\b.*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\s+/", re.IGNORECASE),
]


def check_permission(tool_name: str, args: Dict[str, Any], cfg, approvals: Tuple[str, ...] = ()) -> None:
    tool = get_tool(tool_name)
    if tool.name not in cfg.allowed_tools:
        raise PermissionDenied(f"tool '{tool.name}' is not in the allowlist")

    for key, value in args.items():
        if key in _PATH_ARGS and isinstance(value, str) and value != "":
            resolve_within_root(cfg.workspace_root, value)

    if tool.name == "exec.run":
        command = str(args.get("command", ""))
        for pat in _DENY_COMMAND:
            if pat.search(command):
                raise PermissionDenied(f"exec.run denied by policy: {command!r}")

    if tool.requires_approval and tool.name not in approvals:
        raise ApprovalRequired(tool.name)


def execute(tool_name: str, args: Dict[str, Any], cfg, approvals: Tuple[str, ...] = ()) -> ToolResult:
    tool = get_tool(tool_name)
    check_permission(tool_name, args, cfg, approvals)
    started = time.time()
    try:
        output = tool.handler(dict(args), cfg)
    except (PermissionDenied, ApprovalRequired):
        raise
    except Exception as exc:  # tool failures are reported, not raised as policy errors
        duration = int((time.time() - started) * 1000)
        return ToolResult(ok=False, output=None, error=str(exc), duration_ms=duration)
    duration = int((time.time() - started) * 1000)
    return ToolResult(ok=True, output=_redact_output(output), error="", duration_ms=duration)


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------


@tool("fs.list", "List files under a workspace-relative path matching a glob.", risk="low")
def _fs_list(args: Dict[str, Any], cfg) -> Any:
    base = resolve_within_root(cfg.workspace_root, str(args.get("path", ".")))
    pattern = str(args.get("pattern", "*"))
    matches = glob.glob(os.path.join(base, pattern), recursive=True)
    files = sorted(
        relative_to_root(cfg.workspace_root, m)
        for m in matches
        if os.path.isfile(m)
    )
    return files


@tool("fs.read", "Read a text file inside the workspace.", risk="low")
def _fs_read(args: Dict[str, Any], cfg) -> Any:
    real = resolve_within_root(cfg.workspace_root, str(args["path"]))
    if not os.path.isfile(real):
        raise ToolError(f"not a file: {args['path']!r}")
    with open(real, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(cfg.max_report_chars + 1)
    if len(text) > cfg.max_report_chars:
        text = text[: cfg.max_report_chars] + "\n…[truncated]"
    return text


@tool("fs.write", "Write a text file inside the workspace (creates parents).", risk="medium")
def _fs_write(args: Dict[str, Any], cfg) -> Any:
    real = resolve_within_root(cfg.workspace_root, str(args["path"]))
    os.makedirs(os.path.dirname(real), exist_ok=True)
    content = str(args.get("content", ""))
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": relative_to_root(cfg.workspace_root, real), "bytes": len(content.encode("utf-8"))}


@tool("fs.hash", "Hash a workspace file (default sha256).", risk="low")
def _fs_hash(args: Dict[str, Any], cfg) -> Any:
    real = resolve_within_root(cfg.workspace_root, str(args["path"]))
    algo = str(args.get("algo", "sha256"))
    h = hashlib.new(algo)
    with open(real, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return {"path": relative_to_root(cfg.workspace_root, real), "algo": algo, "digest": h.hexdigest()}


@tool("fs.measure", "Count lines/bytes for files matching a glob (no shell).", risk="low")
def _fs_measure(args: Dict[str, Any], cfg) -> Any:
    base = resolve_within_root(cfg.workspace_root, str(args.get("path", ".")))
    pattern = str(args.get("pattern", "*"))
    matches = glob.glob(os.path.join(base, pattern), recursive=True)
    per_file = {}
    total_lines = 0
    total_bytes = 0
    for m in sorted(matches):
        if not os.path.isfile(m):
            continue
        size = os.path.getsize(m)
        lines = 0
        with open(m, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                lines += chunk.count(b"\n")
        rel = relative_to_root(cfg.workspace_root, m)
        per_file[rel] = {"lines": lines, "bytes": size}
        total_lines += lines
        total_bytes += size
    return {
        "files": sorted(per_file.keys()),
        "per_file": per_file,
        "total_files": len(per_file),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
    }


@tool(
    "verify.file_report",
    "Recompute file measurements and verify a report's claimed totals.",
    risk="low",
)
def _verify_file_report(args: Dict[str, Any], cfg) -> Any:
    report_real = resolve_within_root(cfg.workspace_root, str(args["report_path"]))
    if not os.path.isfile(report_real):
        raise ToolError(f"report not found: {args['report_path']!r}")
    with open(report_real, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    measure = _fs_measure({"path": str(args.get("root", ".")), "pattern": str(args.get("pattern", "*"))}, cfg)
    expected_files = f"Files: {measure['total_files']}"
    expected_lines = f"Total lines: {measure['total_lines']}"
    ok = expected_files in text and expected_lines in text
    return {
        "ok": ok,
        "expected_files": measure["total_files"],
        "expected_lines": measure["total_lines"],
        "report_path": relative_to_root(cfg.workspace_root, report_real),
        "detail": "claimed totals match recomputed measurements" if ok else "claimed totals DO NOT match",
    }


@tool(
    "verify.file_write",
    "Recompute a file's sha256 and verify it matches the expected digest.",
    risk="low",
)
def _verify_file_write(args: Dict[str, Any], cfg) -> Any:
    import hashlib

    expected = str(args.get("sha256", "")).lower()
    if not expected:
        raise ToolError("verify.file_write needs 'sha256'")
    real = resolve_within_root(cfg.workspace_root, str(args["path"]))
    if not os.path.isfile(real):
        raise ToolError(f"file not found: {args['path']!r}")
    h = hashlib.sha256()
    with open(real, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    ok = actual == expected
    return {
        "ok": ok,
        "path": relative_to_root(cfg.workspace_root, real),
        "expected_sha256": expected,
        "actual_sha256": actual,
        "detail": "written bytes match expected digest" if ok else "DIGEST MISMATCH",
    }


@tool(
    "browser.fetch",
    "Fetch a web page (read-only) and return title, text, and links. SSRF-guarded.",
    risk="medium",
)
def _browser_fetch(args: Dict[str, Any], cfg) -> Any:
    from .browsertools import BrowserError, fetch_url

    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("browser.fetch needs 'url'")
    max_chars = int(args.get("max_chars", 20000))
    try:
        return fetch_url(url, max_chars=max(1000, min(max_chars, 100000)))
    except BrowserError as exc:
        raise ToolError(str(exc))


@tool(
    "exec.run",
    "Run a command without a shell, confined to the workspace (high-risk).",
    risk="high",
    requires_approval=True,
)
def _exec_run(args: Dict[str, Any], cfg) -> Any:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("empty command")
    cwd = resolve_within_root(cfg.workspace_root, str(args.get("cwd", ".")))
    timeout = int(args.get("timeout", cfg.step_timeout_s))
    argv = shlex.split(command)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": redact_secrets(proc.stdout[-8000:]),
        "stderr": redact_secrets(proc.stderr[-8000:]),
    }
