import tempfile
import time
import unittest

from nextclaw.config import Config, ensure_data_dir
from nextclaw.scheduler import Scheduler, DEV_CYCLE_JOB


def _cfg():
    data = tempfile.mkdtemp(prefix="nc-sch-data-")
    ws = tempfile.mkdtemp(prefix="nc-sch-ws-")
    cfg = Config(data_dir=data, workspace_root=ws)
    ensure_data_dir(cfg)
    return cfg


def _ok_handler(cfg, timeout_s):
    return {"timed_out": False, "returncode": 0, "tests_passed": True}


def _fail_handler(cfg, timeout_s):
    return {"timed_out": False, "returncode": 1, "tests_passed": False}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.sch = Scheduler(self.cfg, handlers={"cycle": _ok_handler})
        self.sch.ensure_defaults()

    def test_job_persisted_across_instances(self):
        sch2 = Scheduler(self.cfg, handlers={"cycle": _ok_handler})
        job = sch2.store.get_job(DEV_CYCLE_JOB)
        self.assertIsNotNone(job)
        self.assertEqual(job["interval_s"], 40 * 60)

    def test_tick_runs_due_job_once(self):
        # force due now
        with self.sch.store._connect() as conn:
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 1, DEV_CYCLE_JOB))
        results = self.sch.tick()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")
        runs = self.sch.store.list_runs(DEV_CYCLE_JOB)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["trigger"], "schedule")
        # next run rescheduled ~40min out, not immediately due
        job = self.sch.store.get_job(DEV_CYCLE_JOB)
        self.assertGreater(job["next_run_at"], time.time() + 30 * 60)

    def test_overlap_claim_prevents_duplicate(self):
        sch2 = Scheduler(self.cfg, handlers={"cycle": _ok_handler})
        # first scheduler claims directly (simulating a concurrent daemon)
        self.assertTrue(self.sch.store.claim(DEV_CYCLE_JOB))
        result = sch2.run_job(DEV_CYCLE_JOB, "schedule")
        self.assertEqual(result["status"], "already-running")
        self.assertEqual(len(sch2.store.list_runs(DEV_CYCLE_JOB)), 0)

    def test_failed_run_recorded_and_rescheduled(self):
        sch = Scheduler(self.cfg, handlers={"cycle": _fail_handler})
        sch.ensure_defaults()
        with sch.store._connect() as conn:
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 1, DEV_CYCLE_JOB))
        results = sch.tick()
        self.assertEqual(results[0]["status"], "failed")
        job = sch.store.get_job(DEV_CYCLE_JOB)
        self.assertEqual(job["consecutive_failures"], 1)
        self.assertEqual(job["status"], "idle")  # released for next attempt

    def test_stale_run_recovery(self):
        store = self.sch.store
        run_id = store.start_run(DEV_CYCLE_JOB, "schedule")
        # fake an old heartbeat (stale) and a stuck job
        with store._connect() as conn:
            conn.execute("UPDATE runs SET heartbeat_at=? WHERE id=?",
                         (time.time() - 9999, run_id))
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (DEV_CYCLE_JOB,))
        actions = self.sch.recover()
        self.assertTrue(any("interrupted" in a for a in actions))
        runs = store.list_runs(DEV_CYCLE_JOB)
        self.assertEqual(runs[0]["status"], "interrupted")
        job = store.get_job(DEV_CYCLE_JOB)
        self.assertEqual(job["status"], "idle")

    def test_missed_schedule_catchup_once(self):
        with self.sch.store._connect() as conn:
            # overdue by 50 min: more than one interval (40m), less than 1h
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 50 * 60, DEV_CYCLE_JOB))
        results = self.sch.tick()
        self.assertEqual(results[0]["trigger"], "catchup")
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(len(self.sch.store.list_runs(DEV_CYCLE_JOB)), 1)

    def test_long_outage_reschedules_without_stampede(self):
        with self.sch.store._connect() as conn:
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 5 * 3600, DEV_CYCLE_JOB))
        results = self.sch.tick()
        self.assertEqual(results[0]["trigger"], "skipped")
        self.assertEqual(len(self.sch.store.list_runs(DEV_CYCLE_JOB)), 0)
        job = self.sch.store.get_job(DEV_CYCLE_JOB)
        self.assertGreater(job["next_run_at"], time.time())

    def test_timeout_marks_timed_out(self):
        def _slow(cfg, timeout_s):
            time.sleep(timeout_s + 2)
            return {"timed_out": False}
        # exercise the timeout path at the store level via a direct slow handler
        # wrapped the way run_job expects: use real subprocess path instead
        import subprocess as sp

        def _timeout_handler(cfg, timeout_s):
            try:
                sp.run([sys.executable, "-c", "import time; time.sleep(30)"],
                       timeout=1, capture_output=True)
                return {"timed_out": False, "returncode": 0}
            except sp.TimeoutExpired:
                return {"timed_out": True, "returncode": None}

        import sys
        sch = Scheduler(self.cfg, handlers={"cycle": _timeout_handler})
        sch.ensure_defaults()
        with sch.store._connect() as conn:
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 1, DEV_CYCLE_JOB))
        results = sch.tick()
        self.assertEqual(results[0]["status"], "timed_out")
        runs = sch.store.list_runs(DEV_CYCLE_JOB)
        self.assertEqual(runs[0]["status"], "timed_out")

    def test_manual_run_now(self):
        result = self.sch.run_job(DEV_CYCLE_JOB, "manual")
        self.assertEqual(result["status"], "completed")
        runs = self.sch.store.list_runs(DEV_CYCLE_JOB)
        self.assertEqual(runs[0]["trigger"], "manual")

    def test_daemon_start_stop(self):
        cfg = _cfg()
        cfg.scheduler_tick_s = 1
        sch = Scheduler(cfg, handlers={"cycle": _ok_handler})
        pid = sch.start_daemon()
        self.assertTrue(pid > 0)
        self.assertEqual(sch.daemon_alive(), pid)
        # second start must refuse (duplicate prevention)
        with self.assertRaises(RuntimeError):
            sch.start_daemon()
        self.assertTrue(sch.stop_daemon())
        self.assertIsNone(sch.daemon_alive())

    def test_enable_disable(self):
        self.sch.store.set_enabled(DEV_CYCLE_JOB, False)
        with self.sch.store._connect() as conn:
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                         (time.time() - 1, DEV_CYCLE_JOB))
        self.assertEqual(self.sch.tick(), [])
        self.sch.store.set_enabled(DEV_CYCLE_JOB, True)
        results = self.sch.tick()
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
