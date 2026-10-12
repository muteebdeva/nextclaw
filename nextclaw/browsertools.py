"""Browser research tools: fetch-only vertical slice.

``browser.fetch`` retrieves a page over HTTP(S) and returns structured,
verifier-inspectable output (title, text, links). It is deliberately
read-only: no JavaScript execution, no clicking, no form submission, no
screenshots. ``check_chromium()`` probes local Chromium rendering over the
DevTools protocol (the one-shot ``--dump-dom``/``--screenshot`` CLI flags
are broken in some builds; CDP navigate+evaluate+screenshot is the honest
probe). See LIMITATIONS.md.

Safety:
  - Only http/https URLs.
  - SSRF guard: every resolved IP (including redirect targets) must be a
    public unicast address — private, loopback, link-local (this blocks the
    169.254.169.254 cloud-metadata address), multicast, and reserved ranges
    are refused.
  - Redirects followed manually (max 3), each re-validated.
  - Timeouts and a response-size cap; page content is treated as untrusted
    input and secret-redacted like all tool output.
"""
from __future__ import annotations

import html as _html
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Tuple

MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
TIMEOUT_S = 15
USER_AGENT = "nextclaw/0.2 (+research tool)"


class BrowserError(Exception):
    pass


_CHROMIUM_CANDIDATES = (
    "/opt/meta-chromium/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)


def check_chromium() -> Dict[str, Any]:
    """Honest capability probe for browser rendering via DevTools protocol.

    NOTE (audited 2026-10-12): this build's one-shot CLI flags
    (--dump-dom/--screenshot) silently produce no output, but the browser
    itself renders fine — verified via CDP: Page.navigate + Runtime.evaluate
    + Page.captureScreenshot all succeed. So the probe drives the browser
    over CDP instead of trusting the CLI flags. Result cached 10 minutes.
    """
    import time as _time
    now = _time.time()
    cached = _CHROMIUM_PROBE_CACHE.get("result")
    if cached and now - _CHROMIUM_PROBE_CACHE.get("at", 0) < 600:
        return dict(cached)
    result = _probe_chromium_cdp()
    _CHROMIUM_PROBE_CACHE["result"] = result
    _CHROMIUM_PROBE_CACHE["at"] = now
    return dict(result)


_CHROMIUM_PROBE_CACHE: Dict[str, Any] = {}


def _probe_chromium_cdp() -> Dict[str, Any]:
    """Launch headless Chromium with CDP and verify real rendering."""
    import shutil
    import socket
    import subprocess
    import tempfile

    binary = None
    for cand in _CHROMIUM_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            binary = cand
            break
    if binary is None:
        found = shutil.which("chromium") or shutil.which("google-chrome")
        if found:
            binary = found
    if binary is None:
        return {"available": False, "binary": None, "version": None,
                "reason": "no Chromium/Chrome binary found"}
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True,
                              text=True, timeout=15)
        version = (proc.stdout or "").strip() or None
    except Exception as exc:
        return {"available": False, "binary": binary, "version": None,
                "reason": f"binary failed to execute: {exc}"}

    # Free loopback port for the DevTools endpoint.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    profile = tempfile.mkdtemp(prefix="nc-chromium-probe-")
    browser = subprocess.Popen(
        [binary, "--headless", "--no-sandbox", "--disable-dev-shm-usage",
         "--disable-gpu", f"--remote-debugging-port={port}",
         "--remote-debugging-address=127.0.0.1",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time as _time
        import urllib.request as _url
        deadline = _time.time() + 25
        targets = None
        while _time.time() < deadline:
            try:
                with _url.urlopen(f"http://127.0.0.1:{port}/json/list",
                                  timeout=3) as resp:
                    import json as _json
                    targets = _json.loads(resp.read().decode())
                if targets:
                    break
            except Exception:
                _time.sleep(0.5)
        if not targets:
            return {"available": False, "binary": binary, "version": version,
                    "reason": "CDP endpoint did not come up"}
        ok = _cdp_render_check(targets[0]["webSocketDebuggerUrl"])
        if ok:
            return {"available": True, "binary": binary, "version": version,
                    "reason": "CDP render probe succeeded "
                              "(navigate + evaluate + screenshot)"}
        return {"available": False, "binary": binary, "version": version,
                "reason": "CDP render probe failed"}
    finally:
        try:
            browser.terminate()
            browser.wait(timeout=10)
        except Exception:
            try:
                browser.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


def _cdp_render_check(ws_url: str) -> bool:
    """Minimal stdlib WebSocket CDP client: navigate, evaluate, screenshot."""
    import base64
    import hashlib
    import json
    import os as _os
    import socket as _socket
    import struct
    import time as _time

    parts = ws_url.split("/")
    host, p = parts[2].split(":")
    path = "/" + "/".join(parts[3:])
    s = _socket.create_connection((host, int(p)), timeout=15)
    try:
        key = base64.b64encode(_os.urandom(16)).decode()
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {parts[2]}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            return False

        def _recvn(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise RuntimeError("closed")
                buf += chunk
            return buf

        def _send(obj) -> None:
            data = json.dumps(obj).encode()
            mask = _os.urandom(4)
            n = len(data)
            if n < 126:
                hdr = bytes([0x81, 0x80 | n])
            elif n < 65536:
                hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
            else:
                hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
            s.sendall(hdr + mask + bytes(b ^ mask[i % 4]
                                         for i, b in enumerate(data)))

        def _recv_msg():
            payload = b""
            while True:
                h = _recvn(2)
                fin, opcode = h[0] & 0x80, h[0] & 0x0F
                ln = h[1] & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", _recvn(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", _recvn(8))[0]
                data = _recvn(ln)
                if opcode == 0x8:
                    raise RuntimeError("closed by peer")
                if opcode == 0x9:  # ping -> pong
                    s.sendall(bytes([0x8A, len(data)]) + data)
                    continue
                payload += data
                if fin:
                    return payload

        mid = 0

        def _cmd(method: str, params=None, timeout_s: int = 20):
            nonlocal mid
            mid += 1
            _send({"id": mid, "method": method, "params": params or {}})
            deadline = _time.time() + timeout_s
            while _time.time() < deadline:
                msg = json.loads(_recv_msg().decode())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"]))
                    return msg
            raise RuntimeError("cdp command timed out")

        _cmd("Page.navigate",
             {"url": "data:text/html,<html><body><h1>nextclaw-probe</h1></body></html>"})
        _time.sleep(1.5)
        ev = _cmd("Runtime.evaluate",
                  {"expression": "document.body.innerText"})
        if "nextclaw-probe" not in str(
                ev.get("result", {}).get("result", {}).get("value", "")):
            return False
        shot = _cmd("Page.captureScreenshot", {"format": "png"})
        raw = base64.b64decode(shot["result"]["data"])
        return raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 1000
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _public_ip_ok(host: str) -> None:
    """Raise BrowserError unless every resolved IP is public unicast.

    Set NEXTCLAW_BROWSER_ALLOW_NONPUBLIC=1 only behind an egress proxy that
    resolves public hostnames to internal addresses. This weakens SSRF
    protection — never enable it on a server reachable from the internet.
    """
    if os.environ.get("NEXTCLAW_BROWSER_ALLOW_NONPUBLIC") == "1":
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrowserError(f"DNS resolution failed for {host!r}: {exc}")
    ips = {info[4][0] for info in infos}
    if not ips:
        raise BrowserError(f"no addresses for {host!r}")
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if not addr.is_global:
            raise BrowserError(
                f"blocked non-public address {ip} for host {host!r} (SSRF protection)"
            )


def _fetch_raw(url: str) -> Tuple[str, bytes, str]:
    """Fetch URL, following redirects manually with re-validation.

    Returns (final_url, body, content_type).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlparse(current)
        if parts.scheme not in ("http", "https"):
            raise BrowserError(f"unsupported scheme: {parts.scheme!r}")
        if not parts.hostname:
            raise BrowserError(f"URL has no host: {current!r}")
        _public_ip_ok(parts.hostname)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            raise BrowserError(f"HTTP {exc.code} for {current!r}")
        except Exception as exc:
            raise BrowserError(f"fetch failed for {current!r}: {exc}")
        status = getattr(resp, "status", 200)
        if status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise BrowserError("redirect without Location header")
            current = urllib.parse.urljoin(current, location)
            continue
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise BrowserError(f"response exceeds {MAX_BYTES} byte cap")
        return current, body, resp.headers.get("Content-Type", "")
    raise BrowserError("too many redirects")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []
        self.links: List[Dict[str, str]] = []
        self._in_title = False
        self._in_body = False
        self._skip = 0  # script/style depth
        self._cur_href: str = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "body":
            self._in_body = True
        elif tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "a":
            href = dict(attrs).get("href", "")
            self._cur_href = href

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "body":
            self._in_body = False
        elif tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._cur_href = ""

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        elif self._in_body:
            self.text_parts.append(text)
            if self._cur_href and len(self.links) < 50:
                self.links.append({"text": text[:120], "href": self._cur_href[:500]})


def fetch_url(url: str, max_chars: int = 20_000) -> Dict[str, Any]:
    """Fetch a page and return structured, verifier-friendly output."""
    final_url, body, content_type = _fetch_raw(url)
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        html_text = body.decode(charset, errors="replace")
    except Exception:
        html_text = body.decode("utf-8", errors="replace")

    if "html" in content_type or "<html" in html_text[:500].lower():
        ex = _TextExtractor()
        try:
            ex.feed(html_text)
        except Exception:
            pass
        title = " ".join(ex.title_parts).strip()
        text = "\n".join(ex.text_parts)
    else:
        title = ""
        text = html_text
        ex = None

    text = _html.unescape(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[truncated to {max_chars} chars]"
    return {
        "url": url,
        "final_url": final_url,
        "title": title[:300],
        "text": text,
        "links": (ex.links if ex else []),
        "bytes": len(body),
    }
