from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import pi_memory as qc


class ScopeMeasurementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc = Path(temp.name) / "proc"
        self.unit = qc.new_scope()
        self.mount = Path(temp.name) / "cgroup root"
        self.directory = self.mount / "app.slice" / self.unit
        self.directory.mkdir(parents=True)
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "42").mkdir()
        self.membership = self.proc / "42/cgroup"
        self.membership.write_text("0::/user.slice/app.slice/" + self.unit + "\n")
        escaped_mount = str(self.mount).replace(" ", r"\040")
        (self.proc / "self/mountinfo").write_text(
            f"1 2 0:3 /user.slice {escaped_mount} rw - cgroup2 cgroup rw\n")
        (self.directory / "memory.max").write_text(str(qc.LIMIT))
        (self.directory / "memory.peak").write_text(str(qc.PEAK_BUDGET))
        (self.directory / "memory.events").write_text("oom 0\noom_kill 0\n")

    def measure(self):
        return qc.measure_scope(42, self.unit, self.proc)

    def test_exact_boundary_and_subtree_mount(self):
        result = self.measure()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["peak_bytes"], qc.PEAK_BUDGET)

    def test_missing_scope_controller_does_not_borrow_ancestor_limit(self):
        for field in self.directory.glob("memory.*"):
            field.unlink()
        (self.directory.parent / "memory.max").write_text(str(qc.LIMIT))
        (self.mount / "cgroup.controllers").write_text("cpu memory io\n")
        (self.directory.parent / "cgroup.subtree_control").write_text("cpu\n")
        result = self.measure()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(set(result["unavailable"]), {"memory.max", "memory.events", "memory.peak"})
        self.assertIn("memory", result["mount_controllers"])
        self.assertEqual(result["parent_subtree_control"], ["cpu"])

    def test_missing_peak_retains_other_readings(self):
        (self.directory / "memory.peak").unlink()
        result = self.measure()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["limit"], str(qc.LIMIT))
        self.assertEqual(result["events"]["oom_kill"], "0")

    def test_missing_peak_does_not_hide_oom(self):
        (self.directory / "memory.peak").unlink()
        (self.directory / "memory.events").write_text("oom 1\noom_kill 0\n")
        self.assertEqual(self.measure()["status"], "failed")

    def test_wrong_limits(self):
        for limit in ("max", str(qc.LIMIT + 1), str(qc.LIMIT - 1)):
            with self.subTest(limit=limit):
                (self.directory / "memory.max").write_text(limit)
                self.assertEqual(self.measure()["status"], "failed")

    def test_excess_peak_and_oom_kill(self):
        (self.directory / "memory.peak").write_text(str(qc.PEAK_BUDGET + 1))
        (self.directory / "memory.events").write_text("oom 0\noom_kill 1\n")
        result = self.measure()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["failures"]), 2)

    def test_missing_event_counter(self):
        (self.directory / "memory.events").write_text("oom 0\n")
        self.assertEqual(self.measure()["status"], "unverified")

    def test_other_scope_or_no_unified_hierarchy(self):
        for membership in ("0::/inkypi-qc-other.scope", "2:memory:/user.slice"):
            with self.subTest(membership=membership):
                self.membership.write_text(membership)
                self.assertEqual(self.measure()["status"], "unverified")

    def test_user_manager_unavailable(self):
        no_bus = subprocess.CompletedProcess([], 1, "", "Cannot connect to user bus")
        with patch.object(qc.subprocess, "run", return_value=no_bus):
            self.assertEqual(qc.probe(), {"status": "unverified", "measurement_error": no_bus.stderr})


if __name__ == "__main__":
    unittest.main()
