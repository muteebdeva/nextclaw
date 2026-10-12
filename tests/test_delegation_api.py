"""Tests for the multi-agent dashboard API: CRUD, launch, monitor, cancel, retry."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from nextclaw.config import load_config
from nextclaw.server import Handler, ServerContext


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _tool_subtask(sid, path, depends_on=None):
    return {
        "id": sid, "name": f"list {sid}", "kind": "tool",
        "tool": "fs.list", "args": {"path": path},
        "goal": "", "depends_on": depends_on or [],
        "acceptance_criteria": [], "max_retries": 1, "timeout_s": 60,
    }


class DelegationApiTest(unittest.TestCase):
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
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"raw": raw}

    def _wait_status(self, mission_id, want, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = self.req(f"/api/delegations/{mission_id}")
            self.assertEqual(status, 200)
            if body["status"] == want:
                return body
            time.sleep(0.5)
        self.fail(f"mission {mission_id} never reached {want}: {body['status']}")

    # -- profiles / settings -------------------------------------------------
    def test_profiles(self):
        status, body = self.req("/api/delegations/profiles")
        self.assertEqual(status, 200)
        names = {p["name"] for p in body["profiles"]}
        self.assertTrue({"researcher", "coder", "tester", "reviewer"} <= names)
        for p in body["profiles"]:
            self.assertIn("allowed_tools", p)
            self.assertIn("description", p)

    def test_settings_toggle(self):
        status, body = self.req("/api/delegations/settings")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        status, body = self.req("/api/delegations/settings", "PUT",
                                {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        status, body = self.req("/api/delegations/settings")
        self.assertFalse(body["enabled"])
        # restore
        self.req("/api/delegations/settings", "PUT", {"enabled": True})

    def test_settings_rejects_non_bool(self):
        status, _ = self.req("/api/delegations/settings", "PUT",
                             {"enabled": "yes"})
        self.assertEqual(status, 400)

    def test_auth_required(self):
        status, _ = self.req("/api/delegations", token=None)
        self.assertEqual(status, 401)
        status, _ = self.req("/api/delegations", method="POST",
                             body={"goal": "x", "subtasks": []}, token=None)
        self.assertEqual(status, 401)

    # -- validation ---------------------------------------------------------
    def test_create_validation(self):
        # empty goal
        status, _ = self.req("/api/delegations", "POST",
                             {"goal": "", "subtasks": [_tool_subtask("a", self.ws)]})
        self.assertEqual(status, 400)
        # no subtasks
        status, _ = self.req("/api/delegations", "POST",
                             {"goal": "g", "subtasks": []})
        self.assertEqual(status, 400)
        # unknown profile
        bad = _tool_subtask("a", self.ws)
        bad["kind"] = "agent"
        bad["goal"] = "do things"
        bad["profile"] = "wizard"
        status, _ = self.req("/api/delegations", "POST",
                             {"goal": "g", "subtasks": [bad]})
        self.assertEqual(status, 400)
        # dependency cycle
        s1 = _tool_subtask("a", self.ws, depends_on=["b"])
        s2 = _tool_subtask("b", self.ws, depends_on=["a"])
        status, body = self.req("/api/delegations", "POST",
                                {"goal": "g", "subtasks": [s1, s2]})
        self.assertEqual(status, 400)
        self.assertIn("cycle", json.dumps(body).lower())
        # unknown dependency
        s1 = _tool_subtask("a", self.ws, depends_on=["nope"])
        status, _ = self.req("/api/delegations", "POST",
                             {"goal": "g", "subtasks": [s1]})
        self.assertEqual(status, 400)
        # bad budget
        status, _ = self.req("/api/delegations", "POST",
                             {"goal": "g", "subtasks": [_tool_subtask("a", self.ws)],
                              "budgets": {"max_workers": 999}})
        self.assertEqual(status, 400)

    def test_create_rejected_when_disabled(self):
        self.req("/api/delegations/settings", "PUT", {"enabled": False})
        try:
            status, _ = self.req("/api/delegations", "POST",
                                 {"goal": "g",
                                  "subtasks": [_tool_subtask("a", self.ws)]})
            self.assertEqual(status, 403)
        finally:
            self.req("/api/delegations/settings", "PUT", {"enabled": True})

    # -- full lifecycle: create -> run -> monitor -> complete ----------------
    def test_full_lifecycle(self):
        subs = [_tool_subtask("a", self.ws),
                _tool_subtask("b", self.ws, depends_on=["a"])]
        status, body = self.req("/api/delegations", "POST",
                                {"goal": "list workspace twice",
                                 "subtasks": subs,
                                 "budgets": {"max_concurrent": 2}})
        self.assertEqual(status, 202, body)
        mid = body["mission_id"]
        self.assertTrue(mid.startswith("d_"))

        detail = self._wait_status(mid, "completed")
        self.assertEqual(detail["goal"], "list workspace twice")
        self.assertEqual(len(detail["workers"]), 2)
        by_id = {w["subtask_id"]: w for w in detail["workers"]}
        # dependency order respected: b ran after a
        self.assertEqual(by_id["a"]["status"], "completed")
        self.assertEqual(by_id["b"]["status"], "completed")
        self.assertEqual(by_id["b"]["depends_on"], ["a"])
        self.assertIn("attempts", by_id["a"])
        self.assertGreater(detail["usage"]["workers_launched"], 0)

        # list view shows the mission with counts
        status, body = self.req("/api/delegations")
        self.assertEqual(status, 200)
        found = [m for m in body["delegations"] if m["mission_id"] == mid]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subtask_counts"]["completed"], 2)

    def test_detail_404(self):
        status, _ = self.req("/api/delegations/d_nope")
        self.assertEqual(status, 404)

    # -- cancel ----------------------------------------------------------------
    def test_cancel_created_mission(self):
        # Create the record directly without launching, then cancel it.
        store = self.ctx.delegations
        mid = "d_canceltest1"
        store.create(mid, "cancel me", [_tool_subtask("a", self.ws)], {})
        status, body = self.req(f"/api/delegations/{mid}/cancel", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(store.get(mid)["status"], "cancelled")
        # second cancel is a conflict
        status, _ = self.req(f"/api/delegations/{mid}/cancel", "POST", {})
        self.assertEqual(status, 409)

    # -- retry -----------------------------------------------------------------
    def test_retry_failed_subtasks(self):
        store = self.ctx.delegations
        mid = "d_retrytest1"
        plan = [_tool_subtask("good", self.ws), _tool_subtask("bad", self.ws)]
        results = {
            "good": {"status": "completed", "summary": "ok", "attempts": 1},
            "bad": {"status": "failed", "summary": "boom",
                    "errors": ["kaboom"], "attempts": 2},
        }
        store.create(mid, "retry me", plan, {})
        store.update(mid, status="failed", results=results,
                     error="1 subtask(s) failed: ['bad']")
        status, body = self.req(f"/api/delegations/{mid}/retry", "POST", {})
        self.assertEqual(status, 202, body)
        new_id = body["mission_id"]
        self.assertEqual(body["retry_of"], mid)
        self.assertEqual(body["subtasks"], ["bad"])
        rec = store.get(new_id)
        self.assertEqual(rec["retry_of"], mid)
        # only the failed subtask is in the retry plan
        retry_plan = json.loads(rec["plan"])
        self.assertEqual([s["id"] for s in retry_plan], ["bad"])
        # the retry run executes the (working) tool subtask and completes
        self._wait_status(new_id, "completed")
        # retry with nothing failed is a conflict
        status, _ = self.req(f"/api/delegations/{new_id}/retry", "POST", {})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
