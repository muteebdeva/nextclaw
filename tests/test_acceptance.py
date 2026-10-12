"""Final acceptance test: the complete autonomous workflow on a real project.

1. A concrete software change is requested (fix a bug in a test project).
2. A mission inspects the project, reproduces the failure, patches, retests.
3. A deliberately introduced test failure triggers diagnosis + correction.
4. Acceptance criteria are verified deterministically; results reported.
5. The mission is interrupted (paused) and resumed without duplicating actions.
6. A browser task opens a local test page, interacts, verifies resulting state.
7. An intentionally failed acceptance criterion prevents false success.

Deterministic: scripted mission plans + real tools. No model API calls.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest

from nextclaw import tools
from nextclaw.config import Config, ensure_data_dir
from nextclaw import missions
from nextclaw.missions import MissionStore
from nextclaw import verify as verify_mod

APPROVALS = ("exec.run", "browser.click", "browser.fill")


def make_cfg(**kw):
    ws = tempfile.mkdtemp(prefix="nc-acc-ws-")
    data = tempfile.mkdtemp(prefix="nc-acc-data-")
    cfg = Config(data_dir=data, workspace_root=ws, **kw)
    ensure_data_dir(cfg)
    return cfg


def write_project(ws):
    """A small Python project with a deliberate, recoverable bug."""
    with open(os.path.join(ws, "calc.py"), "w") as f:
        f.write("def add(a, b):\n    return a - b  # BUG: should be a + b\n")
    with open(os.path.join(ws, "test_calc.py"), "w") as f:
        f.write(
            "import unittest\nfrom calc import add\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
            "    def test_add_neg(self):\n"
            "        self.assertEqual(add(-1, 1), 0)\n")


class AcceptanceWorkflowTests(unittest.TestCase):
    def test_full_coding_mission(self):
        cfg = make_cfg()
        write_project(cfg.workspace_root)
        store = MissionStore(cfg.data_dir)

        # 2-4. Bounded plan: explore -> reproduce -> fix -> retest.
        plan = [
            {"name": "explore", "tool": "fs.list",
             "args": {"path": ".", "pattern": "*.py"}},
            {"name": "reproduce", "tool": "exec.run",
             "args": {"command": "python3 -m unittest test_calc",
                      "cwd": ".", "timeout": 60},
             "depends_on": ["explore"]},
            {"name": "fix", "tool": "fs.edit",
             "args": {"path": "calc.py",
                      "old_string": "return a - b  # BUG: should be a + b",
                      "new_string": "return a + b"},
             "depends_on": ["reproduce"]},
            {"name": "retest", "tool": "exec.run",
             "args": {"command": "python3 -m unittest test_calc -v",
                      "cwd": ".", "timeout": 60},
             "depends_on": ["fix"]},
        ]
        criteria = [
            {"criterion": "calc test suite passes", "method": "tests",
             "command": "python3 -m unittest test_calc", "cwd": "."},
            {"criterion": "bug line fixed", "method": "files",
             "files": [{"path": "calc.py", "contains": "return a + b"}]},
        ]
        mid = store.create_mission(
            "Fix the add() bug in calc.py", plan=plan,
            budgets={"max_runtime_s": 300, "max_tool_calls": 20},
            acceptance_criteria=criteria)

        # 5. Interrupt mid-mission: pause after the reproduce step ran.
        # Run once with a pause requested up-front to exercise the path,
        # then resume to completion.
        store.request_pause(mid)
        m1 = missions.run_mission(mid, cfg, approvals=APPROVALS)
        self.assertEqual(m1["state"], "paused")

        m2 = missions.resume_mission(mid, cfg, approvals=APPROVALS,
                                     verifier=verify_mod.verify_criteria)
        self.assertEqual(m2["state"], "completed", m2.get("error"))

        # 6. Verify the actual results: bug fixed, tests green.
        with open(os.path.join(cfg.workspace_root, "calc.py")) as f:
            self.assertIn("return a + b", f.read())
        proc = subprocess.run(
            ["python3", "-m", "unittest", "test_calc"],
            cwd=cfg.workspace_root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # 7. No duplicated actions across pause/resume: each idempotency
        # key completed exactly once.
        with store._connect() as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT idempotency_key FROM mission_actions WHERE mission_id = ?",
                (mid,))]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 4)  # explore, reproduce, fix, retest

        # The reproduce step genuinely failed first (evidence, not assumed).
        steps = {s["name"]: s for s in store.get_steps(mid)}
        self.assertEqual(steps["reproduce"]["status"], "completed")
        repro_out = steps["reproduce"]["result"] or {}
        self.assertNotEqual(repro_out.get("returncode"), 0)
        self.assertEqual(steps["retest"]["result"].get("returncode"), 0)

    def test_failed_criterion_blocks_success(self):
        cfg = make_cfg()
        write_project(cfg.workspace_root)
        store = MissionStore(cfg.data_dir)
        plan = [
            {"name": "noop", "tool": "fs.list", "args": {"path": "."}},
        ]
        # Intentionally impossible criterion: must NOT report success.
        criteria = [
            {"criterion": "project has 100% coverage badge", "method": "files",
             "files": [{"path": "coverage.svg", "contains": "100%"}]},
        ]
        mid = store.create_mission("impossible", plan=plan,
                                   acceptance_criteria=criteria)
        m = missions.run_mission(mid, cfg, approvals=APPROVALS,
                                 verifier=verify_mod.verify_criteria)
        self.assertEqual(m["state"], "failed")
        self.assertIn("acceptance", m["error"].lower())


class BrowserWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from nextclaw import browsing
        try:
            browsing.get_session()
            browsing.close_all()
        except browsing.BrowserError as exc:
            raise unittest.SkipTest(f"no browser available: {exc}")

    def test_browser_interaction_verified(self):
        from nextclaw import browsing
        cfg = make_cfg()
        # Local test page as a data: URL (no network). Note: plain
        # http://localhost/ navigation is blocked by this Chromium build's
        # Local Network Access checks (environment limitation, not an
        # adapter bug); data: URLs exercise the same code path.
        html = (
            "<html><body><h1>Counter</h1>"
            "<button id='inc'>+1</button><span id='count'>0</span>"
            "<script>document.getElementById('inc').onclick = () => {"
            "const s = document.getElementById('count');"
            "s.textContent = parseInt(s.textContent) + 1;};</script>"
            "</body></html>")
        import urllib.parse
        url = "data:text/html," + urllib.parse.quote(html)

        r = tools.execute("browser.open", {"url": url}, cfg)
        self.assertTrue(r.ok, r.error)

        snap = tools.execute("browser.snapshot", {}, cfg)
        self.assertTrue(snap.ok, snap.error)
        self.assertIn("#inc", snap.output["snapshot"])

        r2 = tools.execute("browser.click", {"selector": "#inc"}, cfg,
                           approvals=APPROVALS)
        self.assertTrue(r2.ok, r2.error)

        # Verify the RESULTING STATE, not just that the click "worked".
        sess = browsing.get_session()
        count = sess.evaluate(
            "document.getElementById('count').textContent")
        self.assertEqual(count.strip(), "1")

        shot = tools.execute("browser.screenshot", {"name": "shots/counter.png"},
                             cfg)
        self.assertTrue(shot.ok, shot.error)
        self.assertTrue(os.path.isfile(
            os.path.join(cfg.workspace_root, "shots/counter.png")))
        browsing.close_all()


if __name__ == "__main__":
    unittest.main()
