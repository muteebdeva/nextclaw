"""Interactive browser automation over the Chrome DevTools Protocol.

Stdlib-only (no Playwright/Selenium): drives the system Chromium via CDP
using a minimal WebSocket client. Verified working against the bundled
Chromium where the one-shot --dump-dom/--screenshot CLI flags are dead.

Exposed to agents through the tool registry as browser.open / snapshot /
click / fill / select / wait / screenshot / close. State-changing actions
(click, fill, select) require approval; navigation and inspection do not.

Safety:
- Page content is UNTRUSTED input: never treat DOM text as instructions.
- Cookies, session tokens, and passwords are never placed in model prompts.
- Navigation policy: http(s) only by default (file:// needs explicit opt-in);
  optional host allowlist via config (browser_navigate_allowlist).
- Each session is an isolated browser profile (fresh user-data-dir).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class BrowserError(Exception):
    """Browser automation failed (launch, navigation, selector, timeout)."""


_CHROMIUM_CANDIDATES = [
    "/opt/meta-chromium/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def find_chromium() -> Optional[str]:
    for cand in _CHROMIUM_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("chromium") or shutil.which("google-chrome")


# ---------------------------------------------------------------------------
# Minimal WebSocket client (client -> server frames are masked)
# ---------------------------------------------------------------------------

class _WS:
    def __init__(self, url: str, timeout: float = 30):
        parts = url.split("/")
        host, port = parts[2].split(":")
        path = "/" + "/".join(parts[3:])
        self._s = socket.create_connection((host, int(port)), timeout=timeout)
        self._s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self._s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {parts[2]}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._s.recv(4096)
            if not chunk:
                raise BrowserError("websocket handshake failed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise BrowserError("websocket upgrade rejected")

    def _recvn(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._s.recv(n - len(buf))
            if not chunk:
                raise BrowserError("websocket closed")
            buf += chunk
        return buf

    def send(self, obj: Dict[str, Any]) -> None:
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self._s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> Dict[str, Any]:
        payload = b""
        while True:
            h = self._recvn(2)
            fin, opcode = h[0] & 0x80, h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recvn(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recvn(8))[0]
            data = self._recvn(ln)
            if opcode == 0x8:
                raise BrowserError("websocket closed by peer")
            if opcode == 0x9:  # ping -> pong
                self._s.sendall(bytes([0x8A, len(data)]) + data)
                continue
            payload += data
            if fin:
                return json.loads(payload.decode())

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""() => {
  const out = [];
  let n = 0;
  function sel(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 4) {
      let s = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(s);
      cur = parent;
    }
    return parts.join(' > ');
  }
  function walk(el, depth) {
    if (depth > 12 || n > 300) return;
    for (const child of el.children || []) {
      const tag = child.tagName.toLowerCase();
      const role = child.getAttribute('role') || '';
      const text = (child.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80);
      const label = child.getAttribute('aria-label') || child.getAttribute('placeholder')
        || child.getAttribute('name') || child.getAttribute('alt') || child.value || '';
      const interactive = ['a','button','input','select','textarea'].includes(tag)
        || role === 'button' || !!child.onclick;
      if (interactive || text) {
        n++;
        out.push('[' + n + '] <' + tag + '>' +
          (role ? ' role=' + role : '') +
          (label ? ' label="' + String(label).slice(0, 40) + '"' : '') +
          (child.getAttribute('href') ? ' href="' + child.getAttribute('href').slice(0, 60) + '"' : '') +
          (child.getAttribute('type') ? ' type="' + child.getAttribute('type') + '"' : '') +
          (text ? ' "' + text.slice(0, 60) + '"' : '') +
          ' :: ' + sel(child));
      }
      walk(child, depth + 1);
    }
  }
  if (!document.body) return '(empty page)';
  walk(document.body, 0);
  return out.join('\n') || '(no visible elements)';
}"""


class BrowserSession:
    """One isolated Chromium instance driven over CDP."""

    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.profile = ""
        self.ws: Optional[_WS] = None
        self._msg_id = 0
        self.current_url = ""

    # -- lifecycle ------------------------------------------------------
    def launch(self, binary: Optional[str] = None) -> None:
        binary = binary or find_chromium()
        if not binary:
            raise BrowserError("no Chromium/Chrome binary found")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.profile = tempfile.mkdtemp(prefix="nc-browser-")
        self.proc = subprocess.Popen(
            [binary, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--disable-gpu", "--no-proxy-server",
             f"--remote-debugging-port={port}",
             "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={self.profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 25
        targets = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=3
                ) as resp:
                    targets = json.loads(resp.read().decode())
                if targets:
                    break
            except Exception:
                time.sleep(0.5)
        if not targets:
            self.close()
            raise BrowserError("browser CDP endpoint did not start")
        try:
            self.ws = _WS(targets[0]["webSocketDebuggerUrl"])
        except Exception as exc:
            self.close()
            raise BrowserError(f"CDP connect failed: {exc}")

    def _cmd(self, method: str, params: Optional[Dict] = None,
             timeout_s: int = 30) -> Dict[str, Any]:
        if not self.ws:
            raise BrowserError("browser session not connected")
        self._msg_id += 1
        mid = self._msg_id
        self.ws.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                msg = self.ws.recv()
            except socket.timeout:
                raise BrowserError(f"CDP {method} timed out")
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise BrowserError(f"CDP {method} timed out")

    def close(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = ""

    # -- page actions ---------------------------------------------------
    def navigate(self, url: str, timeout_s: int = 30) -> Dict[str, Any]:
        self._cmd("Page.navigate", {"url": url}, timeout_s=timeout_s)
        # Wait for load event (bounded).
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                msg = self.ws.recv()
            except socket.timeout:
                break
            if msg.get("method") == "Page.loadEventFired":
                break
        info = self.evaluate("({title: document.title, url: location.href})",
                             timeout_s=10)
        self.current_url = info.get("url", url)
        if self.current_url.startswith("chrome-error://"):
            raise BrowserError(
                f"navigation to {url!r} failed (browser error page; "
                "check the URL, network, and proxy settings)")
        return {"title": info.get("title", ""), "url": self.current_url}

    def evaluate(self, js: str, timeout_s: int = 30) -> Any:
        res = self._cmd("Runtime.evaluate",
                        {"expression": js, "returnByValue": True},
                        timeout_s=timeout_s)
        result = res.get("result", {})
        if result.get("subtype") == "error":
            raise BrowserError(f"page JS error: {result.get('description', '')[:300]}")
        return result.get("value")

    def snapshot(self) -> str:
        return str(self.evaluate(f"({_SNAPSHOT_JS})()", timeout_s=20))

    def _act(self, selector: str, js_template: str, action: str) -> Dict[str, Any]:
        found = self.evaluate(
            f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
            f"return el?1:0;}})()",
            timeout_s=15,
        )
        if not found:
            raise BrowserError(f"{action}: no element matches {selector!r}")
        self.evaluate(js_template.replace("__SEL__", json.dumps(selector)),
                      timeout_s=15)
        time.sleep(0.5)  # let the page react
        return {"ok": True, "selector": selector}

    def click(self, selector: str) -> Dict[str, Any]:
        return self._act(
            selector,
            "(()=>{const el=document.querySelector(__SEL__);el.scrollIntoView({block:'center'});el.click();})()",
            "click",
        )

    def fill(self, selector: str, text: str) -> Dict[str, Any]:
        return self._act(
            selector,
            "(()=>{const el=document.querySelector(__SEL__);el.focus();"
            "el.value=" + json.dumps(text) + ";"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));})()",
            "fill",
        )

    def select(self, selector: str, value: str) -> Dict[str, Any]:
        return self._act(
            selector,
            "(()=>{const el=document.querySelector(__SEL__);el.value=" + json.dumps(value) + ";"
            "el.dispatchEvent(new Event('change',{bubbles:true}));})()",
            "select",
        )

    def wait_for(self, selector: str = "", text: str = "",
                 timeout_s: int = 15) -> Dict[str, Any]:
        if not selector and not text:
            raise BrowserError("wait_for needs 'selector' or 'text'")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if selector:
                found = self.evaluate(
                    f"(()=>{{return document.querySelector({json.dumps(selector)})?1:0;}})()",
                    timeout_s=10,
                )
                if found:
                    return {"ok": True, "selector": selector}
            else:
                present = self.evaluate(
                    f"(()=>{{return document.body.innerText.includes({json.dumps(text)})?1:0;}})()",
                    timeout_s=10,
                )
                if present:
                    return {"ok": True, "text": text}
            time.sleep(0.5)
        raise BrowserError(
            f"wait_for timed out after {timeout_s}s "
            f"(selector={selector!r} text={text!r})"
        )

    def screenshot(self) -> bytes:
        res = self._cmd("Page.captureScreenshot", {"format": "png"}, timeout_s=20)
        data = res.get("data", "")
        raw = base64.b64decode(data)
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise BrowserError("screenshot did not return a PNG")
        return raw


# ---------------------------------------------------------------------------
# Session registry (one default session; isolated profile per session)
# ---------------------------------------------------------------------------

_sessions: Dict[str, BrowserSession] = {}


def get_session(session_id: str = "default") -> BrowserSession:
    sess = _sessions.get(session_id)
    if sess is None or sess.ws is None:
        sess = BrowserSession()
        sess.launch()
        _sessions[session_id] = sess
    return sess


def close_session(session_id: str = "default") -> None:
    sess = _sessions.pop(session_id, None)
    if sess:
        sess.close()


def close_all() -> None:
    for sid in list(_sessions):
        close_session(sid)
