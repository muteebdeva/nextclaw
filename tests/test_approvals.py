"""Tests for the approval gate + audit log (no network, no model)."""
import tempfile
import threading
import time
import unittest

from nextclaw.approvals import ApprovalGate, ApprovalStore, preapproved_approver
from nextclaw.tasks import TaskStore


class ApprovalStoreTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.store = ApprovalStore(self.d)

    def test_request_decide_cycle(self):
        aid = self.store.request("t1", "exec.run", {"command": "x"}, "risky")
        rec = self.store.get(aid)
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["tool"], "exec.run")
        decided = self.store.decide(aid, "approved", decided_by="u1")
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["decided_by"], "u1")
        # double decision is rejected
        self.assertIsNone(self.store.decide(aid, "rejected"))

    def test_bad_decision_rejected(self):
        aid = self.store.request("t1", "exec.run", {}, "")
        with self.assertRaises(ValueError):
            self.store.decide(aid, "maybe")

    def test_pending_for_task(self):
        self.store.request("t1", "exec.run", {}, "")
        self.store.request("t1", "fs.write", {}, "")
        self.assertEqual(len(self.store.pending_for_task("t1")), 2)
        self.assertEqual(len(self.store.pending_for_task("t2")), 0)

    def test_audit_log(self):
        self.store.audit("user", "task.cancel", "t9", {"why": "test"})
        events = self.store.list_audit(limit=5)
        self.assertTrue(any(e["action"] == "task.cancel" and e["actor"] == "user"
                            for e in events))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.tasks = TaskStore(self.d)
        self.store = ApprovalStore(self.d)
        self.gate = ApprovalGate(self.store, self.tasks, timeout_s=10)
        self.tid = self.tasks.create_task("g", "file_report", {})

    def _decide_soon(self, decision, delay=0.5):
        def _w():
            time.sleep(delay)
            recs = self.store.list(status="pending")
            self.store.decide(recs[0]["id"], decision, decided_by="test")
            self.gate.wake(recs[0]["id"])
        threading.Thread(target=_w, daemon=True).start()

    def test_approve(self):
        self._decide_soon("approved")
        self.assertTrue(self.gate(self.tid, "exec.run", {"command": "echo hi"}, "r"))

    def test_reject(self):
        self._decide_soon("rejected")
        self.assertFalse(self.gate(self.tid, "exec.run", {"command": "echo hi"}, "r"))

    def test_timeout_denies(self):
        gate = ApprovalGate(self.store, self.tasks, timeout_s=0.3)
        self.assertFalse(gate(self.tid, "exec.run", {}, "r"))
        recs = self.store.list()
        self.assertEqual(recs[0]["status"], "rejected")

    def test_cancel_wakes_with_denial(self):
        self.tasks.request_cancel(self.tid)
        # gate should notice cancellation within its poll interval
        gate = ApprovalGate(self.store, self.tasks, timeout_s=10)
        self.assertFalse(gate(self.tid, "exec.run", {}, "r"))


class PreapprovedTest(unittest.TestCase):
    def test_preapproved(self):
        ap = preapproved_approver(["exec.run"])
        self.assertTrue(ap("t", "exec.run", {}, ""))
        self.assertFalse(ap("t", "fs.write", {}, ""))
        ap2 = preapproved_approver(None)
        self.assertFalse(ap2("t", "exec.run", {}, ""))


if __name__ == "__main__":
    unittest.main()
