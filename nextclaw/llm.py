"""Real LLM provider abstraction with function (tool) calling.

Providers:
- ``gemini``: Google Generative Language API (function calling verified
  working). API key comes ONLY from the ``NEXTCLAW_GEMINI_API_KEY``
  environment variable - never from chat, files, or logs.
- ``ollama``: local Ollama ``/api/chat`` with tools. Implemented for
  environments where local models are feasible; fails closed with a clear
  error when unreachable.

No new dependencies: stdlib ``urllib`` only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ProviderError(Exception):
    pass


class NotConfigured(ProviderError):
    pass


class RateLimited(ProviderError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]
    # Gemini 3.x returns a thoughtSignature with each functionCall; it MUST be
    # echoed back in later turns or the API rejects the history (400).
    thought_signature: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    latency_ms: int = 0


# Minimal JSON schemas for the tools we expose to the model.
# Kept here (not in tools.py) so the working registry stays untouched.
PARAM_SPECS: Dict[str, Dict[str, Any]] = {
    "fs.list": {
        "description": "List files under a workspace-relative path matching a glob pattern.",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory, e.g. 'src'"},
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
        },
        "required": ["path"],
    },
    "fs.read": {
        "description": "Read a text file inside the workspace.",
        "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
        "required": ["path"],
    },
    "fs.write": {
        "description": "Write a text file inside the workspace (creates parent dirs).",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        "required": ["path", "content"],
    },
    "fs.hash": {
        "description": "Compute the sha256 hash of a workspace file.",
        "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
        "required": ["path"],
    },
    "fs.measure": {
        "description": "Count lines and bytes for files matching a glob (no shell).",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory"},
            "pattern": {"type": "string", "description": "Glob pattern"},
        },
        "required": ["path"],
    },
    "verify.file_report": {
        "description": "Recompute file measurements and verify a report's claimed totals.",
        "properties": {
            "report_path": {"type": "string", "description": "Workspace-relative report path"},
            "root": {"type": "string", "description": "Workspace-relative directory measured"},
            "pattern": {"type": "string", "description": "Glob pattern measured"},
        },
        "required": ["report_path"],
    },
    "exec.run": {
        "description": "HIGH-RISK: run a command without a shell, confined to the workspace. Requires explicit human approval.",
        "properties": {
            "command": {"type": "string", "description": "Command to run, e.g. 'python3 --version'"},
            "cwd": {"type": "string", "description": "Workspace-relative working directory"},
        },
        "required": ["command"],
    },
}


def build_declarations(allowed_tools) -> List[Dict[str, Any]]:
    """Build compact function declarations for the tools the policy allows."""
    decls = []
    for name in allowed_tools:
        spec = PARAM_SPECS.get(name)
        if not spec:
            continue
        decls.append(
            {
                "name": name,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": spec["properties"],
                    "required": spec.get("required", []),
                },
            }
        )
    return decls


class LLMProvider:
    name = "base"

    def chat(
        self,
        system: str,
        history: List[Dict[str, Any]],
        declarations: List[Dict[str, Any]],
        timeout_s: int = 60,
    ) -> LLMResponse:
        """One model turn.

        history: list of {"role": "user"|"assistant"|"tool", "text": str,
                           "tool_calls": [...], "name": str, "output": Any}
        """
        raise NotImplementedError


def _http_post_json(url: str, payload: Dict[str, Any], timeout_s: int, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=headers or {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")[:500]
        except Exception:
            body = ""
        return exc.code, {"_http_error": body}


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3.7-flash"):
        self.api_key = api_key or os.environ.get("NEXTCLAW_GEMINI_API_KEY", "")
        if not self.api_key:
            raise NotConfigured(
                "Gemini provider needs NEXTCLAW_GEMINI_API_KEY in the environment."
            )
        self.model = model
        self.base = "https://generativelanguage.googleapis.com/v1beta"

    def _to_contents(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        contents = []
        for m in history:
            role = m["role"]
            if role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": m["name"],
                                    "response": {"result": _jsonable(m.get("output"))},
                                }
                            }
                        ],
                    }
                )
            elif role == "assistant":
                parts = []
                if m.get("text"):
                    parts.append({"text": m["text"]})
                for tc in m.get("tool_calls", []):
                    fc: Dict[str, Any] = {
                        "name": tc["name"],
                        "args": _jsonable(tc["args"]),
                    }
                    part: Dict[str, Any] = {"functionCall": fc}
                    if tc.get("thought_signature"):
                        # thoughtSignature is a sibling of functionCall, not nested.
                        part["thoughtSignature"] = tc["thought_signature"]
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append({"role": "user", "parts": [{"text": m.get("text", "")}]})
        return contents

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        started = time.time()
        url = f"{self.base}/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": self._to_contents(history),
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        }
        if declarations:
            payload["tools"] = [{"function_declarations": declarations}]
        status, body = _http_post_json(url, payload, timeout_s)
        latency = int((time.time() - started) * 1000)
        if status in (429, 503):
            raise RateLimited(f"Gemini retryable (http {status}): {body.get('_http_error', '')[:200]}")
        if status != 200 or "_http_error" in body:
            raise ProviderError(f"Gemini error (http {status}): {str(body)[:300]}")
        return _parse_gemini_response(body, latency)


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)[:4000]


def _parse_gemini_response(body: Dict[str, Any], latency: int) -> LLMResponse:
    cands = body.get("candidates", [])
    if not cands:
        feedback = body.get("promptFeedback", {})
        raise ProviderError(f"Gemini returned no candidates: {str(feedback)[:200]}")
    cand = cands[0]
    text_parts = []
    calls = []
    for p in cand.get("content", {}).get("parts", []):
        if "text" in p:
            text_parts.append(p["text"])
        if "functionCall" in p:
            fc = p["functionCall"]
            calls.append(
                ToolCall(
                    id=f"call_{len(calls)}",
                    name=fc.get("name", ""),
                    args=dict(fc.get("args", {}) or {}),
                    # thoughtSignature arrives as a sibling of functionCall.
                    thought_signature=p.get("thoughtSignature", "") or "",
                )
            )
    return LLMResponse(
        text="".join(text_parts).strip(),
        tool_calls=calls,
        finish_reason=cand.get("finishReason", ""),
        latency_ms=latency,
    )


class OllamaProvider(LLMProvider):
    """Local Ollama with tool calling. Fails closed when unreachable."""

    name = "ollama"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self.base_url = base_url or os.environ.get("NEXTCLAW_OLLAMA_URL", "http://127.0.0.1:11434")
        self.model = model or os.environ.get("NEXTCLAW_OLLAMA_MODEL", "")
        if not self.model:
            raise NotConfigured("Ollama provider needs NEXTCLAW_OLLAMA_MODEL in the environment.")

    def chat(self, system, history, declarations, timeout_s=120) -> LLMResponse:
        import urllib.error as _e  # local import keeps module importable without ollama

        started = time.time()
        messages = [{"role": "system", "content": system}]
        for m in history:
            if m["role"] == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps({"result": _jsonable(m.get("output"))})[:4000],
                    }
                )
            elif m["role"] == "assistant":
                messages.append({"role": "assistant", "content": m.get("text", "")})
            else:
                messages.append({"role": "user", "content": m.get("text", "")})
        payload = {"model": self.model, "messages": messages, "stream": False}
        if declarations:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": d["name"],
                        "description": d["description"],
                        "parameters": d["parameters"],
                    },
                }
                for d in declarations
            ]
        try:
            status, body = _http_post_json(f"{self.base_url}/api/chat", payload, timeout_s)
        except _e.URLError as exc:
            raise NotConfigured(f"Ollama not reachable at {self.base_url}: {exc}")
        latency = int((time.time() - started) * 1000)
        if status != 200:
            raise ProviderError(f"Ollama error (http {status}): {str(body)[:300]}")
        msg = body.get("message", {})
        calls = [
            ToolCall(id=f"call_{i}", name=tc["function"]["name"],
                     args=dict(tc["function"].get("arguments", {}) or {}))
            for i, tc in enumerate(msg.get("tool_calls", []) or [])
        ]
        return LLMResponse(text=(msg.get("content") or "").strip(), tool_calls=calls,
                           finish_reason=body.get("done_reason", ""), latency_ms=latency)


def llm_provider(cfg) -> LLMProvider:
    """Select the LLM provider from config. Fails closed when unusable."""
    name = (getattr(cfg, "llm_provider", "gemini") or "gemini").lower()
    if name == "gemini":
        return GeminiProvider(model=getattr(cfg, "llm_model", "gemini-3.7-flash"))
    if name == "ollama":
        return OllamaProvider(model=getattr(cfg, "llm_model", "") or None)
    raise ProviderError(f"unknown llm_provider '{name}'")
