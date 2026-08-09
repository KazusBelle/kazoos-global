import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "kazus_host_monitor", Path(__file__).with_name("monitor.py")
)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


class MemoryClassificationTests(unittest.TestCase):
    def setUp(self):
        self.healthy = {
            "MemTotal": 100_000,
            "MemAvailable": 70_000,
            "SwapTotal": 100_000,
            "SwapFree": 75_500,  # 24.5% occupied, intentionally above old threshold
        }

    def test_stable_swap_occupancy_alone_is_silent_across_runs(self):
        state = {}
        first = monitor._memory_findings(
            state, 0, self.healthy, {"pswpin": 100, "pswpout": 100},
            {"some": 0.0, "full": 0.0},
        )
        second = monitor._memory_findings(
            state, 60, self.healthy, {"pswpin": 100, "pswpout": 100},
            {"some": 0.0, "full": 0.0},
        )
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(state["diagnostics"]["memory"]["swap_used_pct"], 24.5)

    def test_sustained_active_swap_io_with_low_available_ram_warns(self):
        state = {"vmstat": {"pswpin": 100, "pswpout": 100}}
        pressured = dict(self.healthy, MemAvailable=15_000)
        first = monitor._memory_findings(
            state, 0, pressured, {"pswpin": 101, "pswpout": 100},
            {"some": 0.0, "full": 0.0},
        )
        second = monitor._memory_findings(
            state, 181, pressured, {"pswpin": 102, "pswpout": 100},
            {"some": 0.0, "full": 0.0},
        )
        self.assertEqual(first, [])
        self.assertEqual([(x.key, x.level) for x in second], [("memory_pressure", monitor.WARNING)])

    def test_sustained_full_memory_psi_is_critical(self):
        state = {}
        monitor._memory_findings(
            state, 0, self.healthy, {"pswpin": 0, "pswpout": 0},
            {"some": 12.0, "full": 12.0},
        )
        findings = monitor._memory_findings(
            state, 121, self.healthy, {"pswpin": 0, "pswpout": 0},
            {"some": 12.0, "full": 12.0},
        )
        self.assertIn(("memory_pressure", monitor.CRITICAL), [(x.key, x.level) for x in findings])

    def test_low_memavailable_still_alerts_immediately(self):
        low = dict(self.healthy, MemAvailable=6_000)
        findings = monitor._memory_findings(
            {}, 0, low, {"pswpin": 0, "pswpout": 0},
            {"some": 0.0, "full": 0.0},
        )
        self.assertIn(("mem", monitor.CRITICAL), [(x.key, x.level) for x in findings])


class OomTests(unittest.TestCase):
    def test_kernel_oom_evidence_is_critical(self):
        with mock.patch.object(
            monitor, "run", return_value=(0, "oom-kill: constraint=CONSTRAINT_NONE")
        ):
            findings = monitor.check_recent_kernel_oom({}, 1000)
        self.assertEqual([(x.key, x.level) for x in findings], [("host_oom", monitor.CRITICAL)])

    def test_unreadable_kernel_journal_is_silent(self):
        with mock.patch.object(monitor, "run", return_value=(1, "")):
            self.assertEqual(monitor.check_recent_kernel_oom({}, 1000), [])

    def test_container_oomkilled_is_critical(self):
        def fake_run(cmd, timeout=monitor.SUBP_TIMEOUT):
            if cmd[1] == "ps":
                return 0, "container-id"
            if cmd[1] == "inspect":
                return 0, "running|healthy|0|true"
            if cmd[1] == "stats":
                return 0, "0.0%"
            raise AssertionError(cmd)

        with mock.patch.object(monitor, "CONTAINERS", {"worker": "worker"}), \
             mock.patch.object(monitor, "run", side_effect=fake_run):
            findings = monitor.check_docker({}, 0)
        self.assertIn(("oom_worker", monitor.CRITICAL), [(x.key, x.level) for x in findings])


class AlertTransitionTests(unittest.TestCase):
    def finding(self, level=monitor.WARNING, key="memory_pressure"):
        return monitor.Finding(key, level, "test", "", "host", "test")

    def test_entry_persistence_escalation_and_single_recovery(self):
        state = {}
        with mock.patch.object(monitor, "send_telegram", return_value=True) as send:
            monitor.orchestrate_alerts(state, [self.finding()], 0)
            monitor.orchestrate_alerts(state, [self.finding()], 60)
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 61)
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 600)
            monitor.orchestrate_alerts(state, [], 700)
            monitor.orchestrate_alerts(state, [], 999)
            monitor.orchestrate_alerts(state, [], 1000)
            monitor.orchestrate_alerts(state, [], 1400)

        self.assertEqual(send.call_count, 3)
        messages = [call.args[0] for call in send.call_args_list]
        self.assertIn("WARNING", messages[0])
        self.assertIn("CRITICAL", messages[1])
        self.assertIn("RECOVERED", messages[2])

    def test_unrelated_disk_alert_still_sends(self):
        state = {}
        with mock.patch.object(monitor, "send_telegram", return_value=True) as send:
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL, "disk")], 0)
        self.assertEqual(send.call_count, 1)
        self.assertIn("disk", state["active"])

    def test_failed_delivery_retries_but_not_every_cycle(self):
        state = {}
        with mock.patch.object(monitor, "send_telegram", return_value=False) as send:
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 1)
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 60)
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 600)
            monitor.orchestrate_alerts(state, [self.finding(monitor.CRITICAL)], 601)
        self.assertEqual(send.call_count, 2)
        self.assertFalse(state["active"]["memory_pressure"]["notified"])

    def test_failed_recovery_is_retained_for_bounded_retry(self):
        state = {}
        outcomes = [True, False, True]
        with mock.patch.object(monitor, "send_telegram", side_effect=outcomes) as send:
            monitor.orchestrate_alerts(state, [self.finding()], 1)
            monitor.orchestrate_alerts(state, [], 100)
            monitor.orchestrate_alerts(state, [], 400)
            monitor.orchestrate_alerts(state, [], 500)
            monitor.orchestrate_alerts(state, [], 1000)
        self.assertEqual(send.call_count, 3)
        self.assertNotIn("memory_pressure", state["active"])


class AtomicStateTests(unittest.TestCase):
    def test_failed_write_keeps_last_good_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"old": True}, fh)
            with mock.patch.object(monitor, "STATE_FILE", path), \
                 mock.patch.object(monitor.json, "dump", side_effect=OSError("full")):
                monitor.save_state({"new": True})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"old": True})

    def test_successful_write_replaces_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"old": True}, fh)
            with mock.patch.object(monitor, "STATE_FILE", path):
                monitor.save_state({"new": True})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"new": True})


if __name__ == "__main__":
    unittest.main()
