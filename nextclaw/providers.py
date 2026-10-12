"""Universal AI provider system for NextClaw.

A modular, provider-agnostic model layer. Any adapter speaks the same
normalized interface; the agent loop never touches vendor-specific code.

Adapters (stdlib only, no LiteLLM — see docs/PROVIDERS.md for the rationale):
- ``openai_compat``: any OpenAI-compatible ``/v1/chat/completions`` endpoint.
  One adapter covers OpenAI, Azure OpenAI, Groq, OpenRouter, DeepSeek,
  Mistral, xAI, Together, Fireworks, Cerebras, GitHub Models, Hugging Face
  router, LM Studio, llama.cpp server, LocalAI, vLLM, and custom endpoints.
- ``anthropic``: native Anthropic Messages API (tool_use).
- ``gemini``: native Google Generative Language API (function calling).
- ``ollama``: native Ollama ``/api/chat`` (tools).
- ``bedrock``: AWS Bedrock Converse API with SigV4 signing (stdlib).

Provider configurations live in ``providers.db`` (0600) in the data dir.
Secrets are resolved from the environment when ``api_key_env`` is set
(preferred); stored keys are never returned in full by the API and are
redacted from logs. Custom base URLs pass an SSRF guard.

Only adapters implemented AND unit-tested here are advertised. A provider
name appearing in the preset list means "we know how to talk to it", and
every adapter has mocked tests proving the wire format.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .llm import (  # noqa: F401  (re-exported for a single import site)
    LLMResponse,
    NotConfigured,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    ToolCall,
    _http_post_json,
    _jsonable,
    _looks_like_quota,
)


class TransientError(ProviderError):
    """Network failures, timeouts, 5xx: safe to retry with backoff."""


class AuthError(ProviderError):
    """401/403: permanent for these credentials. Never retry the same config."""


class InvalidModelError(ProviderError):
    """404 / model_not_found: permanent for this model id. Never retry."""


def classify_http(status: int, body_text: str, provider: str) -> ProviderError:
    """Map an HTTP failure to the error taxonomy (never guesses)."""
    snippet = (body_text or "")[:300]
    if status in (401, 403):
        return AuthError(f"{provider}: authentication failed (http {status}): {snippet}")
    if status == 404:
        return InvalidModelError(f"{provider}: not found (http {status}): {snippet}")
    if status == 429:
        if _looks_like_quota(body_text or ""):
            return QuotaExhausted(f"{provider}: quota exhausted: {snippet}")
        return RateLimited(f"{provider}: rate limited (429): {snippet}")
    if status in (500, 502, 503, 504):
        return TransientError(f"{provider}: server error (http {status}): {snippet}")
    return ProviderError(f"{provider}: http {status}: {snippet}")


def _http_get(url: str, timeout_s: int, headers: Optional[Dict[str, str]] = None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "nextclaw/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body = ""
        return exc.code, body, dict(exc.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"connection failed for {url.split('/')[2]}: {exc}")


def _http_post(url: str, payload: Dict[str, Any], timeout_s: int,
               headers: Optional[Dict[str, str]] = None):
    """POST JSON; raises the classified error on HTTP failure."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": "nextclaw/0.3"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body = ""
        raise classify_http(exc.code, body, url.split("/")[2])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"connection failed for {url.split('/')[2]}: {exc}")


# ---------------------------------------------------------------------------
# Secrets: masking and redaction
# ---------------------------------------------------------------------------

def mask_secret(value: str) -> str:
    """'sk-abc...xyz' -> 'sk-...wxyz'. Empty -> ''."""
    if not value:
        return ""
    v = str(value)
    if len(v) <= 8:
        return "••••"
    return f"{v[:3]}…{v[-4:]}"


def redact_secrets(text: str, secrets: List[str]) -> str:
    """Replace known secret values with [redacted] (for logs / errors)."""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 8 and s in out:
            out = out.replace(s, "[redacted]")
    return out


# ---------------------------------------------------------------------------
# SSRF guard for user-supplied base URLs
# ---------------------------------------------------------------------------

_METADATA_HOSTS = {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}


def validate_base_url(url: str, allow_private: bool = False) -> str:
    """Validate a user-supplied provider base URL. Returns normalized URL.

    Always blocks cloud metadata endpoints. Blocks private/loopback/link-local
    IPs unless ``allow_private`` is explicitly set (for LAN-local inference).
    Raises ProviderError on any violation.
    """
    if not url or len(url) > 2048:
        raise ProviderError("base_url must be a non-empty http(s) URL under 2048 chars")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ProviderError(f"base_url is not a valid URL: {exc}")
    if parts.scheme not in ("http", "https"):
        raise ProviderError("base_url must use http or https")
    if parts.username or parts.password:
        raise ProviderError("base_url must not embed credentials; use the api key field")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ProviderError("base_url has no hostname")
    if host in _METADATA_HOSTS:
        raise ProviderError(f"base_url host {host} is a cloud metadata endpoint: blocked")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ProviderError(f"base_url IP {host} is not routable: blocked")
        if (ip.is_private or ip.is_loopback) and not allow_private:
            raise ProviderError(
                f"base_url resolves to a private/loopback address ({host}); "
                "set allow_private explicitly for LAN-local inference servers"
            )
    # NOTE: hostname-based check only; DNS rebinding is a documented limitation.
    return f"{parts.scheme}://{parts.netloc}".rstrip("/")


_VALID_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+\Z")


def validate_extra_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Allowlist header names; forbid security-sensitive overrides."""
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        name = str(k)
        if not _VALID_HEADER_NAME.match(name):
            raise ProviderError(f"invalid header name: {k!r}")
        low = name.lower()
        if low in ("authorization", "host", "content-length", "x-amz-date",
                   "x-amz-content-sha256", "x-api-key"):
            raise ProviderError(f"header {name!r} is managed by the provider adapter")
        val = str(v)
        if len(val) > 1024 or "\n" in val or "\r" in val:
            raise ProviderError(f"invalid header value for {name!r}")
        out[name] = val
    return out

# ---------------------------------------------------------------------------
# Normalized provider interface
# ---------------------------------------------------------------------------

@dataclass
class Capabilities:
    tools: bool = False
    streaming: bool = False
    vision: bool = False
    json_mode: bool = False
    note: str = ""


class ChatProvider:
    """One configured model endpoint. All adapters share this contract."""

    adapter = "base"

    # Normalized chat: system prompt + portable history + tool declarations.
    # history entries: {"role": "user"|"assistant"|"tool", "text": str,
    #                   "tool_calls": [{"id","name","args"}], "name": str,
    #                   "output": Any, "tool_call_id": str}
    def chat(self, system: str, history: List[Dict[str, Any]],
             declarations: List[Dict[str, Any]], timeout_s: int = 60) -> LLMResponse:
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def test_connection(self, timeout_s: int = 10) -> Dict[str, Any]:
        """Lightweight reachability/auth check. Never fabricates success."""
        raise NotImplementedError

    def list_models(self, timeout_s: int = 15) -> List[str]:
        """Best-effort model discovery. Returns [] when unsupported."""
        return []

    # -- shared helpers ----------------------------------------------------
    @staticmethod
    def _tool_result_text(output: Any) -> str:
        if isinstance(output, dict) and "error" in output and len(output) == 1:
            return f"error: {output['error']}"
        try:
            return json.dumps(_jsonable(output))[:4000]
        except Exception:
            return str(output)[:4000]


class OpenAICompatProvider(ChatProvider):
    """OpenAI-compatible /v1/chat/completions (covers ~15 vendors + local)."""

    adapter = "openai_compat"

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 extra_headers: Optional[Dict[str, str]] = None,
                 extra_params: Optional[Dict[str, Any]] = None,
                 capabilities_note: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.extra_headers = extra_headers or {}
        self.extra_params = extra_params or {}
        self._caps_note = capabilities_note

    def _headers(self) -> Dict[str, str]:
        h = dict(self.extra_headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _messages(self, system: str, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for m in history:
            role = m.get("role")
            if role == "tool":
                messages.append({
                    "role": "tool",
                    # Fall back to name when an old turn lacks the call id.
                    "tool_call_id": m.get("tool_call_id") or f"call_{m.get('name', '')}",
                    "content": self._tool_result_text(m.get("output")),
                })
            elif role == "assistant":
                msg: Dict[str, Any] = {"role": "assistant",
                                       "content": m.get("text", "") or None}
                tcs = []
                for tc in m.get("tool_calls", []):
                    tcs.append({
                        "type": "function",
                        "id": tc.get("id") or f"call_{tc.get('name', '')}",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(_jsonable(tc.get("args", {}))),
                        },
                    })
                if tcs:
                    msg["tool_calls"] = tcs
                messages.append(msg)
            else:
                messages.append({"role": "user", "content": m.get("text", "")})
        return messages

    def _payload(self, system, history, declarations, stream=False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, history),
            "temperature": 0.2,
        }
        if declarations:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": d["name"], "description": d["description"],
                              "parameters": d["parameters"]}}
                for d in declarations
            ]
        if stream:
            payload["stream"] = True
        payload.update(self.extra_params)
        return payload

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        if not self.model:
            raise NotConfigured("openai_compat provider needs a model id")
        started = time.time()
        try:
            status, body, headers = _http_post_json(
                f"{self.base_url}/v1/chat/completions",
                self._payload(system, history, declarations),
                timeout_s, headers=self._headers())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientError(f"openai_compat connection failed: {exc}")
        latency = int((time.time() - started) * 1000)
        if status != 200 or "_http_error" in body:
            raise classify_http(status, body.get("_http_error", json.dumps(body)[:300]),
                                "openai_compat")
        return _parse_openai_response(body, latency)

    def chat_stream(self, system, history, declarations, timeout_s=60):
        """Yield (text_delta, tool_call_deltas) tuples from SSE. Tested, opt-in."""
        if not self.model:
            raise NotConfigured("openai_compat provider needs a model id")
        data = json.dumps(self._payload(system, history, declarations, stream=True)).encode()
        hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        hdrs.update(self._headers())
        req = urllib.request.Request(f"{self.base_url}/v1/chat/completions",
                                     data=data, headers=hdrs)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            raise classify_http(exc.code, exc.read().decode("utf-8", "replace")[:500],
                                "openai_compat")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientError(f"openai_compat stream failed: {exc}")
        tool_buf: Dict[int, Dict[str, Any]] = {}
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_s = line[5:].strip()
                if data_s == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_s)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices", [{}])[0].get("delta", {}))
                if delta.get("content"):
                    yield ("text", delta["content"])
                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    buf = tool_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                    fn = tc.get("function", {})
                    buf["id"] += tc.get("id", "") or ""
                    buf["name"] += fn.get("name", "") or ""
                    buf["args"] += fn.get("arguments", "") or ""
            for idx in sorted(tool_buf):
                b = tool_buf[idx]
                try:
                    args = json.loads(b["args"]) if b["args"] else {}
                except json.JSONDecodeError:
                    args = {"_raw": b["args"][:2000]}
                yield ("tool", ToolCall(id=b["id"] or f"call_{idx}",
                                        name=b["name"], args=args))
        finally:
            resp.close()

    def capabilities(self) -> Capabilities:
        import dataclasses
        base = ADAPTER_CAPABILITIES["openai_compat"]
        return dataclasses.replace(base, note=self._caps_note or base.note)

    def test_connection(self, timeout_s=10) -> Dict[str, Any]:
        if not self.api_key and "127.0.0.1" not in self.base_url \
                and "localhost" not in self.base_url:
            return {"ok": False, "error": "no API key configured"}
        try:
            status, body_text, _ = _http_get(f"{self.base_url}/v1/models",
                                             timeout_s, headers=self._headers())
        except TransientError as exc:
            return {"ok": False, "error": str(exc)}
        if status == 200:
            try:
                ids = [m.get("id", "") for m in json.loads(body_text).get("data", [])][:8]
            except Exception:
                ids = []
            return {"ok": True, "models_sample": ids}
        if status in (401, 403):
            return {"ok": False, "error": "key rejected by endpoint (invalid key)"}
        return {"ok": False, "error": f"HTTP {status}: {body_text[:200]}"}

    def list_models(self, timeout_s=15) -> List[str]:
        try:
            status, body_text, _ = _http_get(f"{self.base_url}/v1/models",
                                             timeout_s, headers=self._headers())
        except TransientError:
            return []
        if status != 200:
            return []
        try:
            return [m.get("id", "") for m in json.loads(body_text).get("data", []) if m.get("id")]
        except Exception:
            return []


def _parse_openai_response(body: Dict[str, Any], latency: int) -> LLMResponse:
    choices = body.get("choices", [])
    if not choices:
        raise ProviderError(f"openai_compat: no choices in response: {str(body)[:200]}")
    msg = choices[0].get("message", {})
    calls = []
    for i, tc in enumerate(msg.get("tool_calls", []) or []):
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": (fn.get("arguments", "") or "")[:2000]}
        calls.append(ToolCall(id=tc.get("id") or f"call_{i}",
                              name=fn.get("name", ""), args=args))
    usage = body.get("usage", {}) or {}
    return LLMResponse(
        text=(msg.get("content") or "").strip(), tool_calls=calls,
        finish_reason=choices[0].get("finish_reason", ""), latency_ms=latency,
        # usage is carried for the dashboard; LLMResponse ignores extras safely
    )


class AnthropicProvider(ChatProvider):
    """Native Anthropic Messages API with tool_use."""

    adapter = "anthropic"
    DEFAULT_BASE = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: str = "", model: str = "",
                 base_url: str = DEFAULT_BASE,
                 extra_headers: Optional[Dict[str, str]] = None,
                 extra_params: Optional[Dict[str, Any]] = None):
        self.api_key = api_key or ""
        self.model = model
        self.base_url = (base_url or self.DEFAULT_BASE).rstrip("/")
        self.extra_headers = extra_headers or {}
        self.extra_params = extra_params or {}

    def _headers(self) -> Dict[str, str]:
        h = {"x-api-key": self.api_key, "anthropic-version": self.API_VERSION}
        h.update(self.extra_headers)
        return h

    def _to_messages(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Map portable history to Anthropic blocks; merge tool results."""
        messages: List[Dict[str, Any]] = []
        pending_tools: List[Dict[str, Any]] = []

        def flush_tools():
            if pending_tools:
                messages.append({"role": "user", "content": pending_tools[:]})
                pending_tools.clear()

        for m in history:
            role = m.get("role")
            if role == "tool":
                pending_tools.append({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or f"call_{m.get('name', '')}",
                    "content": self._tool_result_text(m.get("output")),
                })
            elif role == "assistant":
                flush_tools()
                content: List[Dict[str, Any]] = []
                if m.get("text"):
                    content.append({"type": "text", "text": m["text"]})
                for tc in m.get("tool_calls", []):
                    content.append({
                        "type": "tool_use",
                        "id": tc.get("id") or f"call_{tc.get('name', '')}",
                        "name": tc.get("name", ""),
                        "input": _jsonable(tc.get("args", {})),
                    })
                messages.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
            else:
                flush_tools()
                messages.append({"role": "user",
                                 "content": [{"type": "text", "text": m.get("text", "")}]})
        flush_tools()
        # Anthropic requires the first message to be user.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        if not self.api_key:
            raise NotConfigured("anthropic provider needs an API key")
        if not self.model:
            raise NotConfigured("anthropic provider needs a model id")
        payload: Dict[str, Any] = {
            "model": self.model, "max_tokens": 2048,
            "messages": self._to_messages(history),
        }
        if system:
            payload["system"] = system
        if declarations:
            payload["tools"] = [
                {"name": d["name"], "description": d["description"],
                 "input_schema": d["parameters"]}
                for d in declarations
            ]
        payload.update(self.extra_params)
        started = time.time()
        try:
            status, body, _ = _http_post(f"{self.base_url}/v1/messages",
                                        payload, timeout_s, headers=self._headers())
        except ProviderError:
            raise
        latency = int((time.time() - started) * 1000)
        return _parse_anthropic_response(body, latency)

    def capabilities(self) -> Capabilities:
        return ADAPTER_CAPABILITIES["anthropic"]

    def test_connection(self, timeout_s=10) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "error": "no API key configured"}
        try:
            status, body_text, _ = _http_get(f"{self.base_url}/v1/models",
                                             timeout_s, headers=self._headers())
        except TransientError as exc:
            return {"ok": False, "error": str(exc)}
        if status == 200:
            try:
                ids = [m.get("id", "") for m in json.loads(body_text).get("data", [])][:8]
            except Exception:
                ids = []
            return {"ok": True, "models_sample": ids}
        if status in (401, 403):
            return {"ok": False, "error": "key rejected by Anthropic (invalid key)"}
        return {"ok": False, "error": f"HTTP {status}: {body_text[:200]}"}

    def list_models(self, timeout_s=15) -> List[str]:
        try:
            status, body_text, _ = _http_get(f"{self.base_url}/v1/models",
                                             timeout_s, headers=self._headers())
        except TransientError:
            return []
        if status != 200:
            return []
        try:
            return [m.get("id", "") for m in json.loads(body_text).get("data", []) if m.get("id")]
        except Exception:
            return []


def _parse_anthropic_response(body: Dict[str, Any], latency: int) -> LLMResponse:
    if body.get("type") == "error":
        err = body.get("error", {})
        raise classify_http(400, err.get("message", ""), "anthropic")
    text_parts, calls = [], []
    for b in body.get("content", []) or []:
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append(ToolCall(id=b.get("id", f"call_{len(calls)}"),
                                  name=b.get("name", ""),
                                  args=dict(b.get("input", {}) or {})))
    return LLMResponse(text="".join(text_parts).strip(), tool_calls=calls,
                       finish_reason=body.get("stop_reason", ""), latency_ms=latency)

# ---------------------------------------------------------------------------
# AWS Bedrock (Converse API + SigV4, stdlib only)
# ---------------------------------------------------------------------------

def _sigv4_headers(method: str, url: str, region: str, service: str,
                   access_key: str, secret_key: str, session_token: str,
                   payload_bytes: bytes,
                   extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build AWS Signature Version 4 authorization headers."""
    now = time.gmtime()
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
    date_stamp = time.strftime("%Y%m%d", now)
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    query = parts.query
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    if extra:
        headers.update({k.lower(): v for k, v in extra.items()})
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canonical = "\n".join([method.upper(), path, query, canonical_headers,
                           signed, payload_hash])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical.encode()).hexdigest()])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _h(("AWS4" + secret_key).encode(), date_stamp)
    k = _h(k, region)
    k = _h(k, service)
    k = _h(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


class BedrockProvider(ChatProvider):
    """AWS Bedrock Converse API. Credentials via env or stored key pair."""

    adapter = "bedrock"

    def __init__(self, model: str, region: str = "us-east-1",
                 access_key: str = "", secret_key: str = "",
                 session_token: str = "",
                 extra_params: Optional[Dict[str, Any]] = None):
        self.model = model
        self.region = region
        self.access_key = access_key or os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = session_token or os.environ.get("AWS_SESSION_TOKEN", "")
        self.extra_params = extra_params or {}
        if not self.access_key or not self.secret_key:
            raise NotConfigured(
                "bedrock needs AWS credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY")

    def _endpoint(self) -> str:
        model_id = urllib.parse.quote(self.model, safe="")
        return (f"https://bedrock-runtime.{self.region}.amazonaws.com"
                f"/model/{model_id}/converse")

    def _to_messages(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []

        def flush():
            if pending:
                messages.append({"role": "user", "content": pending[:]})
                pending.clear()

        for m in history:
            role = m.get("role")
            if role == "tool":
                pending.append({"toolResult": {
                    "toolUseId": m.get("tool_call_id") or f"call_{m.get('name', '')}",
                    "content": {"text": self._tool_result_text(m.get("output"))}}})
            elif role == "assistant":
                flush()
                content: List[Dict[str, Any]] = []
                if m.get("text"):
                    content.append({"text": m["text"]})
                for tc in m.get("tool_calls", []):
                    content.append({"toolUse": {
                        "toolUseId": tc.get("id") or f"call_{tc.get('name', '')}",
                        "name": tc.get("name", ""),
                        "input": _jsonable(tc.get("args", {}))}})
                messages.append({"role": "assistant", "content": content})
            else:
                flush()
                messages.append({"role": "user",
                                 "content": [{"text": m.get("text", "")}]})
        flush()
        return messages

    def _request(self, payload: Dict[str, Any], timeout_s: int) -> Tuple[int, Dict[str, Any]]:
        raw = json.dumps(payload).encode()
        url = self._endpoint()
        headers = _sigv4_headers("POST", url, self.region, "bedrock",
                                 self.access_key, self.secret_key,
                                 self.session_token, raw)
        req = urllib.request.Request(url, data=raw, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")[:1000]
            except Exception:
                body = ""
            raise classify_http(exc.code, body, "bedrock")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientError(f"bedrock connection failed: {exc}")

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        if not self.model:
            raise NotConfigured("bedrock provider needs a model id")
        payload: Dict[str, Any] = {
            "messages": self._to_messages(history),
            "inferenceConfig": {"maxTokens": 2048, "temperature": 0.2},
        }
        if system:
            payload["system"] = [{"text": system}]
        if declarations:
            payload["toolConfig"] = {"tools": [
                {"toolSpec": {
                    "name": d["name"], "description": d["description"],
                    "inputSchema": {"json": d["parameters"]}}}
                for d in declarations]}
        payload.update(self.extra_params)
        started = time.time()
        _status, body = self._request(payload, timeout_s)
        return _parse_bedrock_response(body, int((time.time() - started) * 1000))

    def capabilities(self) -> Capabilities:
        return ADAPTER_CAPABILITIES["bedrock"]

    def test_connection(self, timeout_s=15) -> Dict[str, Any]:
        # No cheap ping exists; a 1-token converse is the honest check.
        try:
            _status, body = self._request(
                {"messages": [{"role": "user", "content": [{"text": "ping"}]}],
                 "inferenceConfig": {"maxTokens": 1}}, timeout_s)
        except ProviderError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        out = (body.get("output", {}).get("message", {}).get("content", []) or [{}])[0]
        return {"ok": True, "models_sample": [self.model],
                "note": f"1-token converse ok (reply: {out.get('text', '')[:40]!r})"}


def _parse_bedrock_response(body: Dict[str, Any], latency: int) -> LLMResponse:
    msg = (body.get("output", {}) or {}).get("message", {}) or {}
    text_parts, calls = [], []
    for b in msg.get("content", []) or []:
        if "text" in b:
            text_parts.append(b["text"])
        if "toolUse" in b:
            tu = b["toolUse"]
            calls.append(ToolCall(id=tu.get("toolUseId", f"call_{len(calls)}"),
                                  name=tu.get("name", ""),
                                  args=dict(tu.get("input", {}) or {})))
    usage = body.get("usage", {}) or {}
    return LLMResponse(text="".join(text_parts).strip(), tool_calls=calls,
                       finish_reason=body.get("stopReason", ""), latency_ms=latency)


# ---------------------------------------------------------------------------
# Wrappers around the existing native adapters (no rewrites)
# ---------------------------------------------------------------------------

class GeminiAdapter(ChatProvider):
    adapter = "gemini"

    def __init__(self, api_key: str = "", model: str = "gemini-3.7-flash"):
        from .llm import GeminiProvider as _G
        self._inner = _G(api_key=api_key or None, model=model)
        self.model = model

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        return self._inner.chat(system, history, declarations, timeout_s)

    def capabilities(self) -> Capabilities:
        return ADAPTER_CAPABILITIES["gemini"]

    def test_connection(self, timeout_s=10) -> Dict[str, Any]:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models"
               f"?key={urllib.parse.quote(self._inner.api_key)}")
        try:
            status, body_text, _ = _http_get(url, timeout_s)
        except TransientError as exc:
            return {"ok": False, "error": str(exc)}
        if status == 200:
            try:
                ids = [m.get("name", "").split("/")[-1]
                       for m in json.loads(body_text).get("models", [])][:8]
            except Exception:
                ids = []
            return {"ok": True, "models_sample": ids}
        if status in (400, 403):
            return {"ok": False, "error": "key rejected by Google (invalid key)"}
        return {"ok": False, "error": f"HTTP {status}: {body_text[:200]}"}

    def list_models(self, timeout_s=15) -> List[str]:
        r = self.test_connection(timeout_s)
        return r.get("models_sample", []) if r.get("ok") else []


class OllamaAdapter(ChatProvider):
    adapter = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = ""):
        from .llm import OllamaProvider as _O
        self._inner = _O(base_url=base_url or None, model=model or None)
        self.model = model
        self.base_url = base_url

    def chat(self, system, history, declarations, timeout_s=120) -> LLMResponse:
        return self._inner.chat(system, history, declarations, timeout_s)

    def capabilities(self) -> Capabilities:
        return Capabilities(tools=True, streaming=True, vision=False,
                            json_mode=False,
                            note="tool calling depends on the local model")

    def test_connection(self, timeout_s=10) -> Dict[str, Any]:
        try:
            status, body_text, _ = _http_get(f"{self.base_url.rstrip('/')}/api/tags",
                                             timeout_s)
        except TransientError as exc:
            return {"ok": False, "error": str(exc)}
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}: {body_text[:200]}"}
        try:
            models = [m.get("name") for m in json.loads(body_text).get("models", [])]
        except Exception:
            models = []
        ok = (not self.model) or any((self.model in (m or "")) for m in models)
        return {"ok": ok, "models": models,
                "error": "" if ok else f"model '{self.model}' not found locally"}

    def list_models(self, timeout_s=15) -> List[str]:
        r = self.test_connection(timeout_s)
        return r.get("models", []) if r.get("ok") else []


ADAPTERS: Dict[str, Any] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiAdapter,
    "ollama": OllamaAdapter,
    "bedrock": BedrockProvider,
}

# Static capability facts per adapter (no secrets or network needed).
ADAPTER_CAPABILITIES: Dict[str, Capabilities] = {
    "openai_compat": Capabilities(tools=True, streaming=True, vision=False,
                                  json_mode=True),
    "anthropic": Capabilities(tools=True, streaming=True, vision=True,
                              json_mode=False),
    "gemini": Capabilities(tools=True, streaming=False, vision=True,
                           json_mode=False),
    "ollama": Capabilities(tools=True, streaming=True, vision=False,
                           json_mode=False,
                           note="tool calling depends on the local model"),
    "bedrock": Capabilities(tools=True, streaming=False, vision=True,
                            json_mode=False),
}


def adapter_capabilities(adapter: str, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Capabilities dict for the dashboard; override wins per key."""
    import dataclasses
    base = ADAPTER_CAPABILITIES.get(adapter, Capabilities())
    d = dataclasses.asdict(base)
    for k, v in (override or {}).items():
        if k in d and isinstance(v, type(d[k])):
            d[k] = v
    return d

# ---------------------------------------------------------------------------
# Presets: known-good starting points (adapter + endpoint + env var).
# A preset is a *configuration template*, not a claim of live verification.
# ---------------------------------------------------------------------------

PRESETS: List[Dict[str, Any]] = [
    # -- cloud, OpenAI-compatible -------------------------------------------
    {"id": "openai", "display": "OpenAI", "adapter": "openai_compat",
     "base_url": "https://api.openai.com", "api_key_env": "OPENAI_API_KEY",
     "default_model": "gpt-5-mini", "notes": "POST /v1/chat/completions"},
    {"id": "azure-openai", "display": "Azure OpenAI", "adapter": "openai_compat",
     "base_url": "https://YOUR-RESOURCE.openai.azure.com", "api_key_env": "AZURE_OPENAI_API_KEY",
     "default_model": "YOUR-DEPLOYMENT-NAME",
     "notes": "Set base_url to your resource; model = deployment name. "
              "Add extra param api-version, e.g. 2024-10-21.",
     "extra_params": {"api-version": "2024-10-21"}},
    {"id": "anthropic", "display": "Anthropic Claude", "adapter": "anthropic",
     "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY",
     "default_model": "claude-sonnet-4-6", "notes": "Native Messages API."},
    {"id": "gemini", "display": "Google Gemini / AI Studio", "adapter": "gemini",
     "base_url": "https://generativelanguage.googleapis.com",
     "api_key_env": "NEXTCLAW_GEMINI_API_KEY",
     "default_model": "gemini-3.7-flash", "notes": "Native function calling."},
    {"id": "bedrock", "display": "Amazon Bedrock", "adapter": "bedrock",
     "base_url": "", "api_key_env": "AWS_ACCESS_KEY_ID",
     "default_model": "anthropic.claude-sonnet-4-6-20250514-v1:0",
     "notes": "SigV4 via AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (+ optional "
              "AWS_SESSION_TOKEN). Set region in extra settings.",
     "extra_params": {"region": "us-east-1"}},
    {"id": "groq", "display": "Groq", "adapter": "openai_compat",
     "base_url": "https://api.groq.com/openai", "api_key_env": "GROQ_API_KEY",
     "default_model": "llama-3.3-70b-versatile"},
    {"id": "openrouter", "display": "OpenRouter", "adapter": "openai_compat",
     "base_url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY",
     "default_model": "openrouter/auto",
     "notes": "Add HTTP-Referer / X-Title via extra headers if desired."},
    {"id": "deepseek", "display": "DeepSeek", "adapter": "openai_compat",
     "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
     "default_model": "deepseek-chat"},
    {"id": "mistral", "display": "Mistral", "adapter": "openai_compat",
     "base_url": "https://api.mistral.ai", "api_key_env": "MISTRAL_API_KEY",
     "default_model": "mistral-large-latest"},
    {"id": "xai", "display": "xAI", "adapter": "openai_compat",
     "base_url": "https://api.x.ai", "api_key_env": "XAI_API_KEY",
     "default_model": "grok-4"},
    {"id": "together", "display": "Together AI", "adapter": "openai_compat",
     "base_url": "https://api.together.xyz", "api_key_env": "TOGETHER_API_KEY",
     "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
    {"id": "fireworks", "display": "Fireworks AI", "adapter": "openai_compat",
     "base_url": "https://api.fireworks.ai/inference", "api_key_env": "FIREWORKS_API_KEY",
     "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct"},
    {"id": "cerebras", "display": "Cerebras", "adapter": "openai_compat",
     "base_url": "https://api.cerebras.ai", "api_key_env": "CEREBRAS_API_KEY",
     "default_model": "llama-3.3-70b"},
    {"id": "github-models", "display": "GitHub Models", "adapter": "openai_compat",
     "base_url": "https://models.github.ai/inference", "api_key_env": "GITHUB_TOKEN",
     "default_model": "openai/gpt-5-mini"},
    {"id": "huggingface", "display": "Hugging Face Inference", "adapter": "openai_compat",
     "base_url": "https://router.huggingface.co/v1", "api_key_env": "HF_TOKEN",
     "default_model": "meta-llama/Llama-3.3-70B-Instruct"},
    # -- local ---------------------------------------------------------------
    {"id": "ollama", "display": "Ollama", "adapter": "ollama",
     "base_url": "http://127.0.0.1:11434", "api_key_env": "",
     "default_model": "", "local": True, "allow_private": True,
     "notes": "No key needed. Discover models via the Models button."},
    {"id": "lmstudio", "display": "LM Studio", "adapter": "openai_compat",
     "base_url": "http://127.0.0.1:1234", "api_key_env": "",
     "default_model": "", "local": True, "allow_private": True,
     "notes": "Enable the OpenAI-compatible server in LM Studio first."},
    {"id": "llamacpp", "display": "llama.cpp server", "adapter": "openai_compat",
     "base_url": "http://127.0.0.1:8080", "api_key_env": "",
     "default_model": "", "local": True, "allow_private": True,
     "notes": "Start with --chat-completion for tool support."},
    {"id": "localai", "display": "LocalAI", "adapter": "openai_compat",
     "base_url": "http://127.0.0.1:8080", "api_key_env": "",
     "default_model": "", "local": True, "allow_private": True},
    {"id": "vllm", "display": "vLLM", "adapter": "openai_compat",
     "base_url": "http://127.0.0.1:8000", "api_key_env": "",
     "default_model": "", "local": True, "allow_private": True},
    # -- custom --------------------------------------------------------------
    {"id": "custom", "display": "Custom OpenAI-compatible endpoint",
     "adapter": "openai_compat", "base_url": "", "api_key_env": "",
     "default_model": "", "notes": "Any /v1/chat/completions endpoint."},
]


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    for p in PRESETS:
        if p["id"] == preset_id:
            return dict(p)
    return None


# ---------------------------------------------------------------------------
# Provider configuration store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    adapter TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    auth_kind TEXT NOT NULL DEFAULT 'api_key',
    api_key TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '',
    aws_secret_key TEXT NOT NULL DEFAULT '',
    extra_headers TEXT NOT NULL DEFAULT '{}',
    extra_params TEXT NOT NULL DEFAULT '{}',
    timeout_s INTEGER NOT NULL DEFAULT 60,
    max_retries INTEGER NOT NULL DEFAULT 2,
    allow_private INTEGER NOT NULL DEFAULT 0,
    compat_json_tools INTEGER NOT NULL DEFAULT 0,
    capabilities_override TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_test_ok INTEGER,
    last_test_at REAL,
    last_test_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_ADAPTERS_REQUIRING_KEY = {"anthropic", "gemini", "bedrock", "openai_compat"}


class ProviderStore:
    """CRUD for provider configs + default/fallback routing. SQLite, 0600."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "providers.db")
        new = not os.path.exists(self.path)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- validation ---------------------------------------------------------
    @staticmethod
    def _validate(data: Dict[str, Any], for_update: bool = False) -> Dict[str, Any]:
        adapter = data.get("adapter", "")
        if not for_update or "adapter" in data:
            if adapter not in ADAPTERS:
                raise ProviderError(
                    f"unknown adapter '{adapter}'; choose from {sorted(ADAPTERS)}")
        name = (data.get("name") or "").strip()
        if not for_update or "name" in data:
            if not name or len(name) > 64 or not re.match(r"^[A-Za-z0-9 _-]+$", name):
                raise ProviderError("name must be 1-64 chars: letters, digits, space, _ -")
        base_url = (data.get("base_url") or "").strip()
        allow_private = bool(int(data.get("allow_private", 0)))
        if base_url:
            base_url = validate_base_url(base_url, allow_private=allow_private)
        model = (data.get("model") or "").strip()
        if len(model) > 256:
            raise ProviderError("model id too long")
        timeout_s = int(data.get("timeout_s", 60))
        if not 5 <= timeout_s <= 600:
            raise ProviderError("timeout_s must be 5..600")
        max_retries = int(data.get("max_retries", 2))
        if not 0 <= max_retries <= 10:
            raise ProviderError("max_retries must be 0..10")
        for key in ("extra_headers", "extra_params", "capabilities_override"):
            raw = data.get(key, "{}")
            if isinstance(raw, dict):
                continue
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError:
                raise ProviderError(f"{key} must be valid JSON")
            if not isinstance(parsed, dict):
                raise ProviderError(f"{key} must be a JSON object")
        if "extra_headers" in data:
            validate_extra_headers(data["extra_headers"] if isinstance(
                data["extra_headers"], dict) else json.loads(data["extra_headers"] or "{}"))
        out = dict(data)
        out["base_url"] = base_url
        return out

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = self._validate(data)
        import uuid as _uuid
        pid = f"pc_{_uuid.uuid4().hex[:12]}"
        now = time.time()
        row = {
            "id": pid, "name": data["name"].strip(), "adapter": data["adapter"],
            "display_name": (data.get("display_name") or "").strip(),
            "base_url": data.get("base_url", ""), "model": (data.get("model") or "").strip(),
            "auth_kind": data.get("auth_kind", "api_key"),
            "api_key": data.get("api_key", "") or "",
            "api_key_env": (data.get("api_key_env") or "").strip(),
            "aws_secret_key": data.get("aws_secret_key", "") or "",
            "extra_headers": json.dumps(data.get("extra_headers") or {}),
            "extra_params": json.dumps(data.get("extra_params") or {}),
            "timeout_s": int(data.get("timeout_s", 60)),
            "max_retries": int(data.get("max_retries", 2)),
            "allow_private": 1 if data.get("allow_private") else 0,
            "compat_json_tools": 1 if data.get("compat_json_tools") else 0,
            "capabilities_override": json.dumps(data.get("capabilities_override") or {}),
            "enabled": 1, "created_at": now, "updated_at": now,
        }
        conn = self._connect()
        try:
            try:
                conn.execute(
                    """INSERT INTO provider_configs
                    (id,name,adapter,display_name,base_url,model,auth_kind,api_key,
                     api_key_env,aws_secret_key,extra_headers,extra_params,timeout_s,
                     max_retries,allow_private,compat_json_tools,capabilities_override,
                     enabled,created_at,updated_at)
                    VALUES (:id,:name,:adapter,:display_name,:base_url,:model,:auth_kind,
                     :api_key,:api_key_env,:aws_secret_key,:extra_headers,:extra_params,
                     :timeout_s,:max_retries,:allow_private,:compat_json_tools,
                     :capabilities_override,:enabled,:created_at,:updated_at)""", row)
                conn.commit()
            except sqlite3.IntegrityError:
                raise ProviderError(f"a provider named '{row['name']}' already exists")
        finally:
            conn.close()
        # First provider becomes the default automatically.
        if not self.get_setting("default_id"):
            self.set_setting("default_id", pid)
        return self.get(pid)

    def get(self, pid: str, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM provider_configs WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return self._public(dict(row), include_secret=include_secret)

    def list(self, include_secret: bool = False) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM provider_configs ORDER BY created_at").fetchall()
        finally:
            conn.close()
        return [self._public(dict(r), include_secret=include_secret) for r in rows]

    @staticmethod
    def _public(row: Dict[str, Any], include_secret: bool = False) -> Dict[str, Any]:
        row = dict(row)
        for k in ("extra_headers", "extra_params", "capabilities_override"):
            try:
                row[k] = json.loads(row.get(k) or "{}")
            except Exception:
                row[k] = {}
        for k in ("enabled", "allow_private", "compat_json_tools"):
            row[k] = bool(row.get(k))
        secret = row.pop("api_key", "")
        aws_secret = row.pop("aws_secret_key", "")
        row["api_key_masked"] = mask_secret(secret) if not row.get("api_key_env") else (
            f"env:{row['api_key_env']}")
        row["has_aws_secret"] = bool(aws_secret)
        if include_secret:
            row["api_key"] = secret
            row["aws_secret_key"] = aws_secret
        return row

    def update(self, pid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        cur = self.get(pid, include_secret=True)
        if not cur:
            raise ProviderError(f"unknown provider '{pid}'")
        data = self._validate(data, for_update=True)
        fields = []
        vals: Dict[str, Any] = {}
        for key in ("name", "adapter", "display_name", "base_url", "model",
                    "auth_kind", "api_key_env", "timeout_s", "max_retries"):
            if key in data:
                fields.append(f"{key}=:{key}")
                vals[key] = data[key].strip() if isinstance(data[key], str) else data[key]
        for key in ("api_key", "aws_secret_key"):
            if key in data:
                # Empty string clears; omitted key leaves the stored secret alone.
                fields.append(f"{key}=:{key}")
                vals[key] = data[key] or ""
        for key in ("extra_headers", "extra_params", "capabilities_override"):
            if key in data:
                v = data[key]
                fields.append(f"{key}=:{key}")
                vals[key] = json.dumps(v if isinstance(v, dict) else json.loads(v or "{}"))
        for key in ("allow_private", "compat_json_tools", "enabled"):
            if key in data:
                fields.append(f"{key}=:{key}")
                vals[key] = 1 if data[key] else 0
        if not fields:
            return self.get(pid)
        vals["id"] = pid
        vals["updated_at"] = time.time()
        fields.append("updated_at=:updated_at")
        conn = self._connect()
        try:
            try:
                conn.execute(
                    f"UPDATE provider_configs SET {', '.join(fields)} WHERE id=:id", vals)
                conn.commit()
            except sqlite3.IntegrityError:
                raise ProviderError("a provider with that name already exists")
        finally:
            conn.close()
        return self.get(pid)

    def delete(self, pid: str) -> None:
        if self.get_setting("default_id") == pid:
            raise ProviderError("cannot delete the default provider; set another default first")
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM provider_configs WHERE id=?", (pid,))
            conn.commit()
            if cur.rowcount == 0:
                raise ProviderError(f"unknown provider '{pid}'")
        finally:
            conn.close()
        # Drop from fallbacks if present.
        fb = self.get_fallbacks()
        if pid in fb:
            self.set_fallbacks([x for x in fb if x != pid])

    # -- routing settings ----------------------------------------------------
    def get_setting(self, k: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT v FROM provider_settings WHERE k=?", (k,)).fetchone()
        finally:
            conn.close()
        return row["v"] if row else ""

    def set_setting(self, k: str, v: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO provider_settings (k, v) VALUES (?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            conn.commit()
        finally:
            conn.close()

    def get_fallbacks(self) -> List[str]:
        raw = self.get_setting("fallback_ids")
        try:
            ids = json.loads(raw) if raw else []
        except Exception:
            ids = []
        return [i for i in ids if isinstance(i, str)]

    def set_fallbacks(self, ids: List[str]) -> None:
        known = {c["id"] for c in self.list()}
        bad = [i for i in ids if i not in known]
        if bad:
            raise ProviderError(f"unknown provider ids in fallbacks: {bad}")
        default = self.get_setting("default_id")
        if default in ids:
            raise ProviderError("the default provider cannot also be a fallback")
        self.set_setting("fallback_ids", json.dumps(list(ids)))

    def set_default(self, pid: str) -> None:
        cfg = self.get(pid)
        if not cfg:
            raise ProviderError(f"unknown provider '{pid}'")
        if not cfg["enabled"]:
            raise ProviderError("cannot set a disabled provider as default")
        self.set_setting("default_id", pid)
        fb = [i for i in self.get_fallbacks() if i != pid]
        self.set_fallbacks(fb)

    def record_test(self, pid: str, ok: bool, error: str = "") -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE provider_configs SET last_test_ok=?, last_test_at=?,"
                " last_test_error=?, updated_at=? WHERE id=?",
                (1 if ok else 0, time.time(), error[:500], time.time(), pid))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Building live providers from configs
# ---------------------------------------------------------------------------

def _resolve_key(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Return (api_key, aws_secret). Env reference wins over stored secret."""
    env_name = (cfg.get("api_key_env") or "").strip()
    key = ""
    if env_name:
        if not re.match(r"^[A-Z][A-Z0-9_]*$", env_name):
            raise ProviderError(f"invalid env var name: {env_name!r}")
        key = os.environ.get(env_name, "")
    if not key:
        key = cfg.get("api_key", "") or ""
    return key, cfg.get("aws_secret_key", "") or ""


def build_provider(cfg: Dict[str, Any]) -> ChatProvider:
    """Instantiate the adapter for a stored (secret-inclusive) config row."""
    adapter = cfg.get("adapter")
    cls = ADAPTERS.get(adapter)
    if cls is None:
        raise ProviderError(f"unknown adapter '{adapter}'")
    api_key, aws_secret = _resolve_key(cfg)
    extra_params = dict(cfg.get("extra_params") or {})
    model = cfg.get("model", "")
    if adapter == "openai_compat":
        if not api_key and not cfg.get("allow_private"):
            # Local servers need no key; remote ones do.
            raise NotConfigured(
                f"provider '{cfg.get('name')}' needs an API key (set api_key_env or paste a key)")
        return OpenAICompatProvider(
            base_url=cfg.get("base_url") or "https://api.openai.com",
            api_key=api_key, model=model,
            extra_headers=cfg.get("extra_headers") or {},
            extra_params=extra_params)
    if adapter == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model,
                                 base_url=cfg.get("base_url") or AnthropicProvider.DEFAULT_BASE,
                                 extra_headers=cfg.get("extra_headers") or {},
                                 extra_params=extra_params)
    if adapter == "gemini":
        return GeminiAdapter(api_key=api_key, model=model or "gemini-3.7-flash")
    if adapter == "ollama":
        return OllamaAdapter(base_url=cfg.get("base_url") or "http://127.0.0.1:11434",
                             model=model)
    if adapter == "bedrock":
        region = extra_params.pop("region", "us-east-1")
        return BedrockProvider(model=model, region=region,
                               access_key=api_key, secret_key=aws_secret,
                               extra_params=extra_params)
    raise ProviderError(f"adapter '{adapter}' has no builder")  # pragma: no cover


def resolve_chain(store: ProviderStore) -> List[Tuple[str, ChatProvider]]:
    """[(config_id, provider)] for default + enabled fallbacks, in order."""
    default_id = store.get_setting("default_id")
    chain: List[Tuple[str, ChatProvider]] = []
    seen = set()

    def _add(pid: str):
        if pid in seen:
            return
        seen.add(pid)
        row = store.get(pid, include_secret=True)
        if not row or not row["enabled"]:
            return
        try:
            chain.append((pid, build_provider(row)))
        except NotConfigured as exc:
            # Misconfigured provider: visible, never silently skipped... but a
            # broken default must not take down the whole chain; record it.
            chain.append((pid, _BrokenProvider(str(exc))))

    if default_id:
        _add(default_id)
    for pid in store.get_fallbacks():
        _add(pid)
    # Last resort: any other enabled config, so a fresh install with one
    # provider still works even if routing was never set.
    for row in store.list():
        _add(row["id"])
    return [(pid, p) for pid, p in chain if not isinstance(p, _BrokenProvider)] or chain


class _BrokenProvider(ChatProvider):
    """Placeholder that fails closed with the configuration error."""

    def __init__(self, reason: str):
        self._reason = reason

    def chat(self, system, history, declarations, timeout_s=60) -> LLMResponse:
        raise NotConfigured(self._reason)

    def test_connection(self, timeout_s=10) -> Dict[str, Any]:
        return {"ok": False, "error": self._reason}


# ---------------------------------------------------------------------------
# Legacy migration: seed provider configs from the old llm_* settings
# ---------------------------------------------------------------------------

def seed_from_legacy_config(store: ProviderStore, cfg) -> None:
    """One-time, idempotent: turn NEXTCLAW_GEMINI_API_KEY / ollama settings
    into provider configs so existing installs keep working."""
    if store.list():
        return  # already seeded or user-configured
    created = []
    gemini_key = os.environ.get("NEXTCLAW_GEMINI_API_KEY", "")
    legacy_provider = (getattr(cfg, "llm_provider", "gemini") or "gemini").lower()
    if gemini_key or legacy_provider == "gemini":
        created.append(store.create({
            "name": "gemini", "adapter": "gemini",
            "display_name": "Google Gemini (migrated)",
            "model": getattr(cfg, "llm_model", "gemini-3.7-flash"),
            "api_key_env": "NEXTCLAW_GEMINI_API_KEY" if gemini_key else "",
        }))
    ollama_model = getattr(cfg, "ollama_model", "") or ""
    if ollama_model or legacy_provider == "ollama":
        created.append(store.create({
            "name": "ollama-local", "adapter": "ollama",
            "display_name": "Ollama local (migrated)",
            "base_url": getattr(cfg, "ollama_url", "http://127.0.0.1:11434"),
            "model": ollama_model, "allow_private": True,
        }))
    if created:
        # Prefer the legacy default when it exists among the seeds.
        for c in created:
            if c["adapter"] == legacy_provider:
                store.set_default(c["id"])
                break


def llm_provider(cfg, store: Optional[ProviderStore] = None) -> ChatProvider:
    """Backwards-compatible entry point: returns the default provider.

    Old callers (and NEXTCLAW_OFFLINE=1) keep working; new code should use
    resolve_chain() for fallback support.
    """
    if os.environ.get("NEXTCLAW_OFFLINE", "") == "1":
        raise NotConfigured("offline mode (NEXTCLAW_OFFLINE=1): no model calls allowed")
    own = store is None
    store = store or ProviderStore(cfg.data_dir)
    seed_from_legacy_config(store, cfg)
    chain = resolve_chain(store)
    if not chain:
        raise NotConfigured(
            "no usable AI provider configured — add one in the dashboard"
            " (Providers) or set NEXTCLAW_GEMINI_API_KEY")
    _pid, provider = chain[0]
    return provider
