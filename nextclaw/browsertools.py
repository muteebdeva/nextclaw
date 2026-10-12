"""Browser research tools: fetch-only vertical slice.

``browser.fetch`` retrieves a page over HTTP(S) and returns structured,
verifier-inspectable output (title, text, links). It is deliberately
read-only: no JavaScript execution, no clicking, no form submission, no
screenshots. Interactive browsing requires a working headless Chromium,
which is not available in all environments (see LIMITATIONS.md).

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
