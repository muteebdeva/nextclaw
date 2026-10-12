"""Tests for browser.fetch (SSRF guard + parsing). No external network."""
import os
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nextclaw.browsertools import BrowserError, fetch_url


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/redir-private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/")
            self.end_headers()
            return
        body = (b"<html><head><title>Local Test</title></head><body>"
                b"<p>Hello world</p><a href='/next'>next page</a>"
                b"<script>var x = 1;</script></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _start_server()
        cls.port = cls.srv.server_address[1]
        os.environ["NEXTCLAW_BROWSER_ALLOW_NONPUBLIC"] = "1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("NEXTCLAW_BROWSER_ALLOW_NONPUBLIC", None)

    def test_fetch_parses(self):
        r = fetch_url(f"http://127.0.0.1:{self.port}/", max_chars=5000)
        self.assertEqual(r["title"], "Local Test")
        self.assertIn("Hello world", r["text"])
        self.assertNotIn("var x = 1", r["text"])  # script stripped
        self.assertTrue(any(l["href"] == "/next" for l in r["links"]))

    def test_redirect_to_private_blocked(self):
        # Temporarily re-enable the guard: redirect target must be validated.
        os.environ.pop("NEXTCLAW_BROWSER_ALLOW_NONPUBLIC", None)
        try:
            with self.assertRaises(BrowserError):
                fetch_url(f"http://127.0.0.1:{self.port}/redir-private")
        finally:
            os.environ["NEXTCLAW_BROWSER_ALLOW_NONPUBLIC"] = "1"


class SsrfGuardTest(unittest.TestCase):
    def test_private_ranges_blocked(self):
        for url in ("http://127.0.0.1:8080/", "http://10.0.0.5/",
                    "http://169.254.169.254/latest/", "http://[::1]/"):
            with self.assertRaises(BrowserError, msg=url):
                fetch_url(url)

    def test_bad_scheme_blocked(self):
        with self.assertRaises(BrowserError):
            fetch_url("ftp://example.com/x")

    def test_unresolvable_blocked(self):
        with self.assertRaises(BrowserError):
            fetch_url("http://nonexistent.invalid abc/")


class VerifyFileWriteTest(unittest.TestCase):
    def test_tool_registered_and_verifies(self):
        import tempfile
        from nextclaw import tools
        from nextclaw.config import Config

        d = tempfile.mkdtemp()
        cfg = Config(data_dir=tempfile.mkdtemp(), workspace_root=d)
        res = tools.execute("fs.write", {"path": "a.txt", "content": "abc"}, cfg)
        self.assertTrue(res.ok)
        import hashlib
        digest = hashlib.sha256(b"abc").hexdigest()
        v = tools.execute("verify.file_write", {"path": "a.txt", "sha256": digest}, cfg)
        self.assertTrue(v.output["ok"])
        v2 = tools.execute("verify.file_write", {"path": "a.txt", "sha256": "0" * 64}, cfg)
        self.assertFalse(v2.output["ok"])


class ChromiumProbeTest(unittest.TestCase):
    def test_probe_result_cached(self):
        from nextclaw import browsertools
        calls = []
        fake = {"available": True, "binary": "/x/chrome", "version": "v",
                "reason": "probe ok"}

        def _fake_probe():
            calls.append(1)
            return dict(fake)

        with unittest.mock.patch.object(browsertools, "_probe_chromium_cdp",
                                        _fake_probe):
            browsertools._CHROMIUM_PROBE_CACHE.clear()
            r1 = browsertools.check_chromium()
            r2 = browsertools.check_chromium()
        self.assertEqual(r1, fake)
        self.assertEqual(r2, fake)
        self.assertEqual(len(calls), 1)  # second call served from cache
        browsertools._CHROMIUM_PROBE_CACHE.clear()

    def test_probe_shape(self):
        from nextclaw import browsertools
        with unittest.mock.patch.object(
                browsertools, "_probe_chromium_cdp",
                return_value={"available": False, "binary": None,
                              "version": None, "reason": "no binary"}):
            browsertools._CHROMIUM_PROBE_CACHE.clear()
            r = browsertools.check_chromium()
            browsertools._CHROMIUM_PROBE_CACHE.clear()
        self.assertFalse(r["available"])
        self.assertIn("reason", r)


if __name__ == "__main__":
    unittest.main()
