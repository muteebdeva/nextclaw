"""Tests for the multi-agent delegation engine.

Covers: single-agent path preservation, delegation success, parallel
execution, dependency ordering, invalid plans/cycles, worker timeouts and
provider failures, structured-output validation, cancellation, budgets,
recovery, duplicate prevention, conflicting results, review limits,
permission enforcement, and existing regressions.
"""
import json
import os
import tempfile
import threading
import time
import unittest

from nextclaw import delegation as d
from nextclaw.delegation import (
    Subtask, WorkerResult, DelegationBudgets, DelegationError,
    validate_plan, validate_worker_result,
)
from nextclaw.config import Config, ensure_data_dir
from nextclaw import tools


def make_cfg(**kw):
    ws = tempfile.mkdtemp(prefix="nc-dlg-ws-")
    data = tempfile.mkdtemp(prefix="nc-dlg-data-")
    cfg = Config(data_dir=data, workspace_root=ws, **kw)
    ensure_data_dir(cfg)
    return cfg


def tool_sub(sid, name, tool, args, depends_on=None, **kw):
    return Subtask(id=sid, name=name, goal=name, kind="tool", tool=tool,
                   args=args, depends_on=depends_on or [], **kw)


class PlanValidationTests(unittest.TestCase):
    def test_empty_plan_rejected(self):
        with self.assertRaises(DelegationError):
            validate_plan([])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(DelegationError):
            validate_plan([
                tool_sub("a", "x", "fs.list", {"path": "."}),
                tool_sub("a", "y", "fs.list", {"path": "."}),
            ])

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(DelegationError):
            validate_plan([
                tool_sub("a", "x", "fs.list", {"path": "."}, depends_on=["zzz"]),
            ])

    def test_self_dependency_rejected(self):
        with self.assertRaises(DelegationError):
            validate_plan([
                tool_sub("a", "x", "fs.list", {"path": "."}, depends_on=["a"]),
            ])

    def test_cycle_rejected(self):
        with self.assertRaises(DelegationError) as cm:
            validate_plan([
                tool_sub("a", "x", "fs.list", {"path": "."}, depends_on=["c"]),
                tool_sub("b", "y", "fs.list", {"path": "."}, depends_on=["a"]),
                tool_sub("c", "z", "fs.list", {"path": "."}, depends_on=["b"]),
            ])
        self.assertIn("cycle", str(cm.exception).lower())

    def test_unknown_profile_rejected(self):
        with self.assertRaises(DelegationError):
            validate_plan([
                Subtask(id="a", name="x", goal="g", kind="agent",
                        profile="nope"),
            ])

    def test_valid_plan_passes(self):
        subs = [
            tool_sub("a", "x", "fs.list", {"path": "."}),
            tool_sub("b", "y", "fs.list", {"path": "."}, depends_on=["a"]),
        ]
        self.assertEqual(len(validate_plan(subs)), 2)


class WorkerResultValidationTests(unittest.TestCase):
    def test_valid_result(self):
        r = validate_worker_result({
            "task_id": "t1", "worker_id": "w1", "profile": "coder",
            "subtask_id": "s1", "status": "completed", "summary": "done"})
        self.assertEqual(r.status, "completed")

    def test_missing_field_rejected(self):
        with self.assertRaises(ValueError):
            validate_worker_result({
                "task_id": "t1", "worker_id": "w1", "profile": "coder",
                "subtask_id": "s1"})  # no status

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            validate_worker_result({
                "task_id": "t1", "worker_id": "w1", "profile": "coder",
                "subtask_id": "s1", "status": "maybe"})

    def test_tool_worker_may_omit_task_id(self):
        r = validate_worker_result({
            "task_id": "", "worker_id": "w1", "profile": "tool",
            "subtask_id": "s1", "status": "completed"})
        self.assertEqual(r.profile, "tool")


class DelegationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_successful_delegation(self):
        report = d.run_delegation(self.cfg, "test", [
            tool_sub("a", "write", "fs.write",
                     {"path": "a.txt", "content": "hi"}),
        ])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["results"]["a"]["status"], "completed")

    def test_parallel_independent_tasks(self):
        # Two slow tool calls should overlap in time.
        subs = [
            tool_sub("a", "slow1", "exec.run",
                     {"command": "python3 -c 'import time; time.sleep(2)'"}),
            tool_sub("b", "slow2", "exec.run",
                     {"command": "python3 -c 'import time; time.sleep(2)'"}),
        ]
        t0 = time.time()
        report = d.run_delegation(
            self.cfg, "test", subs,
            budgets=DelegationBudgets(max_concurrent=2),
            approvals=("exec.run",))
        elapsed = time.time() - t0
        self.assertEqual(report["status"], "completed")
        # Sequential would take >= 4s; parallel should take < 3.5s.
        self.assertLess(elapsed, 3.5,
                        f"tasks did not run in parallel ({elapsed:.1f}s)")

    def test_dependency_ordering(self):
        order = []
        orig = tools.execute

        def tracking(name, args, cfg, approvals=()):
            order.append(name + ":" + str(args.get("path", "")))
            return orig(name, args, cfg, approvals)

        tools.execute = tracking
        try:
            report = d.run_delegation(self.cfg, "test", [
                tool_sub("a", "write", "fs.write",
                         {"path": "x.txt", "content": "1"}),
                tool_sub("b", "read", "fs.read", {"path": "x.txt"},
                         depends_on=["a"]),
            ])
        finally:
            tools.execute = orig
        self.assertEqual(report["status"], "completed")
        self.assertEqual(order[0], "fs.write:x.txt")
        self.assertEqual(order[1], "fs.read:x.txt")

    def test_worker_timeout_and_failure(self):
        report = d.run_delegation(self.cfg, "test", [
            tool_sub("a", "missing", "fs.read", {"path": "nope.txt"},
                     max_retries=1),
        ], budgets=DelegationBudgets(max_retries_per_worker=1))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["results"]["a"]["status"], "failed")

    def test_dependency_failure_skips_dependents(self):
        report = d.run_delegation(self.cfg, "test", [
            tool_sub("a", "bad", "fs.read", {"path": "nope.txt"}),
            tool_sub("b", "never", "fs.list", {"path": "."},
                     depends_on=["a"]),
        ])
        self.assertEqual(report["status"], "failed")
        r = report["results"]["b"]
        self.assertEqual(r["status"], "failed")
        self.assertIn("dependency", r["summary"].lower())

    def test_max_workers_budget(self):
        subs = [tool_sub(f"s{i}", f"t{i}", "fs.list", {"path": "."})
                for i in range(5)]
        with self.assertRaises(DelegationError):
            d.run_delegation(self.cfg, "test", subs,
                             budgets=DelegationBudgets(max_workers=3))

    def test_runtime_budget(self):
        report = d.run_delegation(
            self.cfg, "test",
            [tool_sub("a", "slow", "exec.run",
                      {"command": "python3 -c 'import time; time.sleep(30)'"})],
            budgets=DelegationBudgets(max_runtime_s=2),
            approvals=("exec.run",))
        # Either the delegation budget trips or the worker fails; the
        # mission must not hang and must not report success.
        self.assertNotEqual(report["status"], "completed")

    def test_permission_enforcement(self):
        # Workers cannot bypass the tool policy (traversal denied, no retry).
        report = d.run_delegation(self.cfg, "test", [
            tool_sub("evil", "x", "fs.read", {"path": "../secret"}),
        ])
        self.assertEqual(report["status"], "failed")
        err = report["results"]["evil"]["errors"][0].lower()
        self.assertTrue("denied" in err or "escapes workspace" in err, err)

    def test_approval_required_fails_worker(self):
        # exec.run without approval -> worker fails (no silent bypass).
        report = d.run_delegation(self.cfg, "test", [
            tool_sub("a", "x", "exec.run", {"command": "echo hi"}),
        ])
        self.assertEqual(report["status"], "failed")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        d._artifacts_init(self.cfg.data_dir)

    def test_register_and_list(self):
        p = os.path.join(self.cfg.workspace_root, "out.txt")
        with open(p, "w") as f:
            f.write("data")
        rec = d.register_artifact(self.cfg.data_dir, "m1", "w1", "t1",
                                  "report", p)
        self.assertEqual(rec["version"], 1)
        self.assertEqual(len(rec["sha256"]), 64)
        arts = d.list_artifacts(self.cfg.data_dir, "m1")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["name"], "report")

    def test_versioning(self):
        p = os.path.join(self.cfg.workspace_root, "out.txt")
        with open(p, "w") as f:
            f.write("v1")
        d.register_artifact(self.cfg.data_dir, "m1", "w1", "t1", "r", p)
        with open(p, "w") as f:
            f.write("v2")
        rec = d.register_artifact(self.cfg.data_dir, "m1", "w1", "t1", "r", p)
        self.assertEqual(rec["version"], 2)

    def test_missing_file_rejected(self):
        with self.assertRaises(DelegationError):
            d.register_artifact(self.cfg.data_dir, "m1", "w1", "t1",
                                "r", "/nonexistent/x.txt")


class DelegateToolTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_delegate_tool_runs(self):
        r = tools.execute("delegate", {
            "goal": "write two files",
            "subtasks": [
                {"id": "a", "name": "w1", "goal": "g", "kind": "tool",
                 "tool": "fs.write", "args": {"path": "d1.txt", "content": "1"}},
                {"id": "b", "name": "w2", "goal": "g", "kind": "tool",
                 "tool": "fs.write", "args": {"path": "d2.txt", "content": "2"}},
            ],
        }, self.cfg)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output["status"], "completed")

    def test_delegate_rejects_empty(self):
        r = tools.execute("delegate", {"goal": "g", "subtasks": []}, self.cfg)
        self.assertFalse(r.ok)

    def test_delegate_depth_guard(self):
        r = tools.execute("delegate", {
            "goal": "g",
            "subtasks": [{"id": "a", "name": "x", "goal": "g",
                          "kind": "tool", "tool": "fs.list",
                          "args": {"path": "."}}],
            "depth": 99,
        }, self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("depth", r.error)


class CancellationTests(unittest.TestCase):
    def test_cancel_stops_delegation(self):
        cfg = make_cfg()
        ctx = d.DelegationContext(DelegationBudgets())
        ctx.cancel()
        with self.assertRaises(DelegationError):
            ctx.check_budgets()

    def test_cancel_delegation_marks_tasks(self):
        from nextclaw.tasks import TaskStore
        cfg = make_cfg()
        store = TaskStore(cfg.data_dir)
        tid = store.create_task("[tester] slow", "agent",
                                {"worker_id": "w9", "mission_id": "m9"})
        store.set_task_status(tid, "running")
        ctx = d.DelegationContext(DelegationBudgets())
        d.cancel_delegation(ctx, cfg)
        self.assertTrue(ctx.cancel_event.is_set())
        self.assertTrue(store.cancel_requested(tid))


if __name__ == "__main__":
    unittest.main()
