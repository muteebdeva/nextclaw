"""Tests for the dashboard API server (real HTTP, temp data dir)."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from nextclaw.config import load_config
from nextclaw.server import Handler, ServerContext, VERSION


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.ws = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": cls.d, "workspace_root": cls.ws})
        cls.ctx = ServerContext(cfg)
        cls.ctx.token = "test-token"
        Handler.ctx = cls.ctx
        from http.server import ThreadingHTTPServer
        cls.port = _free_port()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.srv.daemon_threads = True
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.ctx.runner.shutdown(wait=False)

    def req(self, path, method="GET", body=None, token="test-token"):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    # -- auth ------------------------------------------------------------
    def test_health_open(self):
        status, body = self.req("/api/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], VERSION)

    def test_auth_required(self):
        status, body = self.req("/api/status", token=None)
        self.assertEqual(status, 401)
        status, _ = self.req("/api/status", token="wrong")
        self.assertEqual(status, 401)

    def test_status(self):
        status, body = self.req("/api/status")
        self.assertEqual(status, 200)
        # New provider-system shape: default routing summary + browser probe.
        self.assertIn("default_id", body["providers"])
        self.assertIn("count", body["providers"])
        self.assertIn("available", body["browser"])
        self.assertIn("offline", body)

    # -- tasks --------------------------------------------------------------
    def test_create_and_run_task(self):
        status, body = self.req("/api/tasks", method="POST", body={
            "goal": "count via api", "mode": "run", "task_type": "file_report",
            "params": {"root": ".", "pattern": "*.md", "output": "reports/api.md"},
        })
        self.assertEqual(status, 201)
        tid = body["task_id"]
        for _ in range(60):
            _, detail = self.req(f"/api/tasks/{tid}")
            if detail["status"] == "completed":
                break
            time.sleep(0.5)
        self.assertEqual(detail["status"], "completed")
        self.assertTrue(detail["result"]["verified"])
        self.assertTrue(len(detail["events"]) > 0)
        self.assertEqual(len(detail["steps"]), 4)

    def test_create_task_validation(self):
        status, body = self.req("/api/tasks", method="POST", body={"goal": ""})
        self.assertEqual(status, 400)
        status, _ = self.req("/api/tasks", method="POST", body={"goal": "x", "mode": "bogus"})
        self.assertEqual(status, 400)

    def test_list_and_filter(self):
        self.req("/api/tasks", method="POST", body={
            "goal": "filter me", "mode": "run", "task_type": "file_report",
            "params": {"root": ".", "pattern": "*.md", "output": "reports/f.md"}})
        status, body = self.req("/api/tasks?q=filter+me")
        self.assertEqual(status, 200)
        self.assertTrue(any("filter me" in t["goal"] for t in body["tasks"]))

    def test_unknown_task_404(self):
        status, _ = self.req("/api/tasks/nope123")
        self.assertEqual(status, 404)

    # -- approvals ------------------------------------------------------------
    def test_approval_flow_via_api(self):
        from nextclaw.approvals import ApprovalStore
        store = ApprovalStore(self.d)
        aid = store.request("t-approval", "exec.run", {"command": "echo hi"}, "test")
        status, body = self.req("/api/approvals?status=pending")
        self.assertTrue(any(a["id"] == aid for a in body["approvals"]))
        status, body = self.req(f"/api/approvals/{aid}", method="POST",
                                body={"decision": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(body["approval"]["status"], "approved")
        # second decision conflicts
        status, _ = self.req(f"/api/approvals/{aid}", method="POST",
                             body={"decision": "rejected"})
        self.assertEqual(status, 409)

    def test_gate_blocks_until_api_decision(self):
        # End-to-end: agent hits approval, UI decides, task completes.
        from nextclaw import agent
        cfg = self.ctx.cfg
        tid = agent.create_task("echo test", "file_write",
                                {"path": "appr/ok.txt", "content": "x"}, cfg)
        import threading as th
        result = {}
        def _run():
            # simulate what the server runner does with an approval-requiring tool
            from nextclaw import tools
            try:
                tools.check_permission("exec.run", {"command": "echo hi"}, cfg, ())
            except tools.ApprovalRequired as exc:
                ok = self.ctx.gate(tid, "exec.run", {"command": "echo hi"}, str(exc))
                result["approved"] = ok
        t = th.Thread(target=_run, daemon=True)
        t.start()
        # wait for the pending approval to appear, then approve via API
        aid = None
        for _ in range(60):
            _, body = self.req("/api/approvals?status=pending")
            pend = [a for a in body["approvals"] if a["task_id"] == tid]
            if pend:
                aid = pend[0]["id"]
                break
            time.sleep(0.5)
        self.assertIsNotNone(aid, "approval request never appeared")
        status, _ = self.req(f"/api/approvals/{aid}", method="POST", body={"decision": "approved"})
        self.assertEqual(status, 200)
        t.join(timeout=15)
        self.assertTrue(result.get("approved"))

    # -- providers / quota / policy ----------------------------------------------
    def test_provider_test_no_key(self):
        status, body = self.req("/api/providers/test", method="POST", body={"provider": "gemini"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])  # no key in test env

    def test_config_update_and_reject(self):
        status, body = self.req("/api/providers/config", method="POST",
                                body={"llm_model": "gemini-3.7-flash"})
        self.assertEqual(status, 200)
        self.assertEqual(body["updated"]["llm_model"], "gemini-3.7-flash")
        status, body = self.req("/api/providers/config", method="POST",
                                body={"NEXTCLAW_GEMINI_API_KEY": "nope"})
        self.assertEqual(status, 400)  # keys can never be set via API

    def test_quota(self):
        status, body = self.req("/api/quota")
        self.assertEqual(status, 200)
        self.assertIn("daily_used", body)

    def test_policy(self):
        status, body = self.req("/api/policy")
        self.assertEqual(status, 200)
        names = [t["name"] for t in body["tools"]]
        self.assertIn("browser.fetch", names)
        self.assertIn("verify.file_write", names)
        self.assertIn("exec.run", names)

    def test_audit(self):
        status, body = self.req("/api/audit?limit=5")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["events"], list)

    # -- scheduler -------------------------------------------------------------------
    def test_scheduler_jobs(self):
        status, body = self.req("/api/scheduler/jobs")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["jobs"], list)

    def test_scheduler_enable_disable(self):
        from nextclaw.scheduler import Scheduler, DEV_CYCLE_JOB
        sch = Scheduler(self.ctx.cfg)
        sch.ensure_defaults()
        status, body = self.req(f"/api/scheduler/jobs/{DEV_CYCLE_JOB}/disable", method="POST")
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        status, body = self.req(f"/api/scheduler/jobs/{DEV_CYCLE_JOB}/enable", method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])

    # -- SSE ----------------------------------------------------------------------------
    def test_sse_streams_events(self):
        status, body = self.req("/api/tasks", method="POST", body={
            "goal": "sse test", "mode": "run", "task_type": "file_report",
            "params": {"root": ".", "pattern": "*.md", "output": "reports/sse.md"}})
        tid = body["task_id"]
        req = urllib.request.Request(
            f"{self.base}/api/tasks/{tid}/events?token=test-token&since=0")
        text = ""
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
            # read line-by-line: the stream stays open until the task ends
            start = time.time()
            while time.time() - start < 25:
                line = resp.readline().decode()
                if not line:
                    break
                text += line
                if line.startswith("event: done"):
                    break
        self.assertIn('"kind": "status"', text)


if __name__ == "__main__":
    unittest.main()
