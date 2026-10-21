"""Incident lifecycle and boundary tests; no Docker, provider, or notification calls."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("bonuspro_monitor_test_module", SCRIPTS / "monitor_vps.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class Response:
    def __init__(self, body=b'{"ok":true}', status=200):
        self.body, self.status = body, status
    def read(self, size):
        return self.body[:size]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "version": 1, "release_root": str(monitor.RELEASE_ROOT),
            "monitor": {"public_origin": "https://loyalty.addwise.kz",
                        "expected_active_accounts": 1, "max_processing_age_seconds": 900,
                        "max_backup_age_hours": 32},
            "backup": {"enabled": True, "offsite_location_confirmed": True},
            "telegram": {"token_file": "/etc/bonuspro-ops/telegram_token", "chat_id": "-123456"},
            "heartbeat": {"url_file": "/etc/bonuspro-ops/heartbeat_url"},
            "recovery_key_exported": True,
        }
        self.state = monitor.empty_state()
        self.writes = []
        self.writer = lambda path, value: self.writes.append(copy.deepcopy(value))
        self.token = b"1234567890:" + b"synthetic_private_token_0123456789"
        self.now = 1760000000

    def sample(self, code="app_container", ok=False):
        return [monitor.check(code, ok)]

    def fail(self, code="app_container", at=None):
        at = self.now if at is None else at
        for offset in range(3):
            monitor.advance(self.state, self.sample(code), at + offset)

    def receipt(self, **changes):
        result = {
            "completed_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            "image": "bonuspro:" + "a" * 40, "schema": "0018", "sha256": "b" * 64,
            "bytes": 1024, "offsite_verified": True, "restoration_verified": True,
            "recovery_bundle_verified": True, "database_file": "20260101T030000Z-1.pgdump.bpbk",
            "recovery_file": "20260101T030000Z-1.recovery.bpbk",
        }
        result.update(changes)
        return json.dumps(result).encode()

    def test_three_failures_open_and_two_successes_recover_after_delivery(self):
        monitor.advance(self.state, self.sample(), self.now)
        monitor.advance(self.state, self.sample(), self.now + 1)
        self.assertEqual(monitor.pending_events(self.state, self.now + 1), [])
        monitor.advance(self.state, self.sample(), self.now + 2)
        sender = mock.Mock(return_value=True)
        self.assertTrue(monitor.deliver(self.state, self.config, self.now + 2, sender, self.writer))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(monitor.pending_events(self.state, self.now + 3), [])
        monitor.advance(self.state, self.sample(ok=True), self.now + 3)
        self.assertEqual(monitor.pending_events(self.state, self.now + 3), [])
        monitor.advance(self.state, self.sample(ok=True), self.now + 4)
        self.assertEqual(monitor.pending_events(self.state, self.now + 4)[0]["kind"], "recovery")
        monitor.deliver(self.state, self.config, self.now + 4, sender, self.writer)
        self.assertFalse(self.state["incidents"]["app_container"]["alerted"])
        self.assertEqual(monitor.pending_events(self.state, self.now + 5), [])

    def test_failed_delivery_is_retried_without_marking_alerted(self):
        self.fail()
        sender = mock.Mock(side_effect=[False, True])
        self.assertFalse(monitor.deliver(self.state, self.config, self.now + 3, sender, self.writer))
        self.assertFalse(self.state["incidents"]["app_container"]["alerted"])
        self.assertIsNone(monitor.deliver(self.state, self.config, self.now + 30, sender, self.writer))
        self.assertTrue(monitor.deliver(self.state, self.config, self.now + 63, sender, self.writer))
        self.assertEqual(sender.call_count, 2)
        self.assertTrue(self.writes[0]["incidents"]["app_container"]["open"])
        self.assertFalse(self.writes[0]["incidents"]["app_container"]["alerted"])
        self.assertEqual(sender.call_args_list[0].args[1], sender.call_args_list[1].args[1])

    def test_failed_recovery_delivery_stays_pending_across_state_round_trip(self):
        self.fail()
        monitor.deliver(self.state, self.config, self.now + 3, lambda *args: True, self.writer)
        for offset in (4, 5):
            monitor.advance(self.state, self.sample(ok=True), self.now + offset)
        monitor.deliver(self.state, self.config, self.now + 5, lambda *args: False, self.writer)
        restored = json.loads(json.dumps(self.state))
        self.assertTrue(restored["incidents"]["app_container"]["alerted"])
        self.assertEqual(monitor.pending_events(restored, self.now + 65)[0]["kind"], "recovery")
        monitor.deliver(restored, self.config, self.now + 65, lambda *args: True, self.writer)
        self.assertEqual(monitor.pending_events(restored, self.now + 66), [])

    def test_pending_recovery_is_cancelled_by_a_new_failed_probe(self):
        self.fail()
        monitor.deliver(self.state, self.config, self.now + 3, lambda *args: True, self.writer)
        for offset in (4, 5):
            monitor.advance(self.state, self.sample(ok=True), self.now + offset)
        monitor.deliver(self.state, self.config, self.now + 5, lambda *args: False, self.writer)
        monitor.advance(self.state, self.sample(ok=False), self.now + 65)
        sender = mock.Mock(return_value=True)
        self.assertIsNone(monitor.deliver(self.state, self.config, self.now + 65, sender, self.writer))
        sender.assert_not_called()
        for offset in (66, 67):
            monitor.advance(self.state, self.sample(ok=True), self.now + offset)
        self.assertEqual(monitor.pending_events(self.state, self.now + 67)[0]["kind"], "recovery")

    def test_unnotified_outage_does_not_send_recovery(self):
        self.fail()
        monitor.deliver(self.state, self.config, self.now + 3, lambda *args: False, self.writer)
        for offset in (4, 5):
            monitor.advance(self.state, self.sample(ok=True), self.now + offset)
        self.assertEqual(monitor.pending_events(self.state, self.now + 100), [])

    def test_operational_reminder_after_six_hours_but_setup_does_not_repeat(self):
        self.fail()
        self.fail("external_monitor_missing")
        monitor.deliver(self.state, self.config, self.now + 3, lambda *args: True, self.writer)
        self.assertEqual(monitor.pending_events(self.state, self.now + 4), [])
        events = monitor.pending_events(self.state, self.now + 3 + monitor.REMINDER_SECONDS)
        self.assertEqual([(x["code"], x["kind"]) for x in events], [("app_container", "reminder")])

    def test_deployment_grace_uses_persistent_first_observation_and_expires(self):
        self.assertEqual(monitor.deployment_status(self.state, True, self.now), (True, False))
        restored = json.loads(json.dumps(self.state))
        self.assertEqual(monitor.deployment_status(restored, True, self.now + 1799), (True, False))
        self.assertEqual(monitor.deployment_status(restored, True, self.now + 1800), (False, True))
        monitor.deployment_status(restored, False, self.now + 1900)
        self.assertIsNone(restored["deployment_since"])

    def test_deployment_freezes_incidents_and_suppresses_pending_delivery(self):
        self.fail()
        before = copy.deepcopy(self.state["incidents"]["app_container"])
        monitor.advance(self.state, self.sample(ok=None), self.now + 4)
        self.assertEqual(before, self.state["incidents"]["app_container"])
        sender = mock.Mock(return_value=True)
        self.assertIsNone(monitor.deliver(self.state, self.config, self.now + 4, sender, self.writer,
                                         {"app_container"}))
        sender.assert_not_called()

    def test_check_only_has_no_state_write_or_posts_even_for_failed_probes(self):
        collector = lambda *args: (self.sample(), False)
        sender, heartbeat, writer = mock.Mock(), mock.Mock(), mock.Mock()
        original = copy.deepcopy(self.state)
        result = monitor.run_monitor(self.config, check_only=True, state=self.state, now=self.now,
                                     collector=collector, sender=sender, heartbeat_sender=heartbeat,
                                     writer=writer)
        self.assertFalse(result["ready"])
        self.assertFalse(result["technical_ready"])
        self.assertEqual(self.state, original)
        sender.assert_not_called()
        heartbeat.assert_not_called()
        writer.assert_not_called()

    def test_heartbeat_attests_completed_monitor_cycle_including_an_app_outage(self):
        checks = self.sample() + [monitor.check("external_monitor_missing", True)]
        heartbeat = mock.Mock(return_value=True)
        result = monitor.run_monitor(self.config, state=self.state, now=self.now,
                                     collector=lambda *args: (checks, False),
                                     sender=mock.Mock(), heartbeat_sender=heartbeat, writer=self.writer)
        heartbeat.assert_called_once()
        self.assertTrue(result["heartbeat_delivered"])
        self.assertFalse(result["technical_ready"])

    def test_heartbeat_failure_becomes_a_redacted_condition(self):
        checks = [monitor.check("external_monitor_missing", True)]
        result = monitor.run_monitor(self.config, state=self.state, now=self.now,
                                     collector=lambda *args: (checks, False),
                                     heartbeat_sender=lambda config: False, writer=self.writer)
        self.assertIn(monitor.check("external_heartbeat_delivery", False), result["checks"])
        self.assertFalse(result["ready"])

    def test_missing_heartbeat_key_or_explicit_null_does_not_crash_delivery(self):
        self.config["heartbeat"] = None
        result = monitor.run_monitor(self.config, state=self.state, now=self.now,
                                     collector=lambda *args: ([monitor.check("external_monitor_missing", False)], False),
                                     sender=mock.Mock(), writer=self.writer)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["heartbeat_delivered"])

    def test_failed_state_persistence_does_not_refresh_external_heartbeat(self):
        heartbeat = mock.Mock(return_value=True)
        checks = [monitor.check("external_monitor_missing", True)]
        with self.assertRaises(monitor.OpsError):
            monitor.run_monitor(self.config, state=self.state, now=self.now,
                                collector=lambda *args: (checks, False), heartbeat_sender=heartbeat,
                                writer=mock.Mock(side_effect=monitor.OpsError("OPS_STATE_WRITE_FAILED")))
        heartbeat.assert_not_called()

    def test_missing_retained_ciphertext_does_not_count_as_fresh_local_backup(self):
        values = monitor.backup_checks(self.config, self.now, lambda *args: self.receipt(),
                                       lambda receipt: False)
        self.assertFalse(next(x["ok"] for x in values if x["code"] == "backup_local"))
        self.assertFalse(monitor.backup_artifacts(json.loads(self.receipt(database_file="../escape.bpbk"))))

    def test_backup_requires_fresh_complete_offsite_restore_and_recovery_proof(self):
        good = monitor.backup_checks(self.config, self.now, lambda *args: self.receipt(), lambda receipt: True)
        self.assertTrue(all(item["ok"] for item in good))
        for changes, code in (({"offsite_verified": False}, "backup_offsite"),
                              ({"restoration_verified": False}, "backup_restore"),
                              ({"recovery_bundle_verified": False}, "backup_recovery_bundle"),
                              ({"bytes": 1}, "backup_local"),
                              ({"schema": "0015"}, "backup_local"),
                              ({"completed_at": "2020-01-01T00:00:00+00:00"}, "backup_local")):
            with self.subTest(changes=changes):
                values = monitor.backup_checks(self.config, self.now,
                                               lambda *args, changes=changes: self.receipt(**changes),
                                               lambda receipt: True)
                self.assertFalse(next(item["ok"] for item in values if item["code"] == code))

    def test_disabled_backup_and_missing_external_monitor_are_setup_pending(self):
        self.config["backup"]["enabled"] = False
        values = monitor.backup_checks(self.config, self.now, mock.Mock())
        self.assertEqual(values, [monitor.check("backup_not_configured", False)])
        result = monitor.summary(values + [monitor.check("external_monitor_missing", False)], False)
        self.assertFalse(result["ready"])
        self.assertTrue(result["technical_ready"])

    def systemd_runner(self, service=None, timer=None, enabled=b"enabled\n"):
        service = service or {"LoadState": "loaded", "ActiveState": "inactive",
                              "Result": "success", "ExecMainStatus": "0"}
        timer = timer or {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting"}
        def run(arguments, **kwargs):
            self.assertIn(arguments[1], ("show", "is-enabled"))
            if arguments[1] == "is-enabled":
                return enabled
            value = service if arguments[2].endswith(".service") else timer
            return "".join(key + "=" + item + "\n" for key, item in value.items()).encode()
        return run

    def test_backup_unit_failure_is_not_masked_by_a_fresh_verified_receipt(self):
        receipt = monitor.backup_checks(self.config, self.now, lambda *args: self.receipt(),
                                        lambda receipt: True)
        service = {"LoadState": "loaded", "ActiveState": "failed",
                   "Result": "timeout", "ExecMainStatus": "1"}
        probes = monitor.backup_unit_checks(self.config, self.systemd_runner(service=service))
        self.assertTrue(all(item["ok"] for item in receipt))
        self.assertEqual(probes, [monitor.check("backup_unit", False), monitor.check("backup_timer", True)])
        combined = receipt + probes
        for offset in range(3):
            monitor.advance(self.state, combined, self.now + offset)
        self.assertEqual([item["code"] for item in monitor.pending_events(self.state, self.now + 3)],
                         ["backup_unit"])

    def test_backup_service_accepts_successful_loaded_idle_or_running_states_only(self):
        for active in ("inactive", "active", "activating"):
            with self.subTest(active=active):
                service = {"LoadState": "loaded", "ActiveState": active,
                           "Result": "success", "ExecMainStatus": "0"}
                probes = monitor.backup_unit_checks(self.config, self.systemd_runner(service=service))
                self.assertTrue(probes[0]["ok"])
        baseline = {"LoadState": "loaded", "ActiveState": "inactive",
                    "Result": "success", "ExecMainStatus": "0"}
        for change in ({"LoadState": "not-found"}, {"ActiveState": "failed"},
                       {"ActiveState": "deactivating"}, {"Result": "timeout"}, {"ExecMainStatus": "9"}):
            with self.subTest(change=change):
                service = {**baseline, **change}
                probes = monitor.backup_unit_checks(self.config, self.systemd_runner(service=service))
                self.assertFalse(probes[0]["ok"])

    def test_backup_timer_must_be_enabled_and_waiting_or_currently_running(self):
        probes = monitor.backup_unit_checks(self.config, self.systemd_runner(enabled=b"disabled\n"))
        self.assertFalse(probes[1]["ok"])
        timer = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"}
        probes = monitor.backup_unit_checks(self.config, self.systemd_runner(timer=timer))
        self.assertFalse(probes[1]["ok"])
        timer = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running"}
        service = {"LoadState": "loaded", "ActiveState": "activating",
                   "Result": "success", "ExecMainStatus": "0"}
        probes = monitor.backup_unit_checks(self.config, self.systemd_runner(service=service, timer=timer))
        self.assertTrue(all(item["ok"] for item in probes))

    def test_disabled_backup_does_not_probe_systemd(self):
        self.config["backup"]["enabled"] = False
        runner = mock.Mock()
        self.assertEqual(monitor.backup_unit_checks(self.config, runner), [])
        runner.assert_not_called()

    def test_backup_systemd_errors_are_redacted_and_properties_are_strict(self):
        output = io.StringIO()
        runner = mock.Mock(side_effect=RuntimeError("private provider URL and token"))
        with redirect_stdout(output):
            probes = monitor.backup_unit_checks(self.config, runner)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(probes, [monitor.check("backup_unit", False), monitor.check("backup_timer", False)])
        with self.assertRaises(ValueError):
            monitor.systemd_properties(b"Result=success\nResult=private\n", ("Result",))
        with self.assertRaises(ValueError):
            monitor.systemd_properties(b"Result=success\nDescription=private\n", ("Result",))

    def test_notification_labels_are_closed_russian_descriptions_with_safe_codes(self):
        self.assertEqual(set(monitor.ALERT_LABELS), set(monitor.SEVERITIES))
        text = monitor.notification_text([{"code": "backup_unit", "kind": "alert", "event": "a" * 12},
                                          {"code": "processing_stale", "kind": "recovery", "event": "b" * 12}])
        self.assertIn("Ошибка резервного копирования (backup_unit)", text)
        self.assertIn("Обработка заказов задерживается (processing_stale)", text)
        self.assertIn("Проблема устранена", text)
        with self.assertRaises(monitor.OpsError):
            monitor.notification_text([{"code": "private token", "kind": "alert", "event": "x"}])

    def test_retained_ciphertext_with_public_permissions_is_rejected(self):
        directory = mock.Mock(st_uid=0, st_mode=0o40700)
        public_file = mock.Mock(st_uid=0, st_mode=0o100644, st_nlink=1, st_size=1024)
        with mock.patch.object(monitor.os, "open", side_effect=[10, 11]), \
             mock.patch.object(monitor.os, "fstat", side_effect=[directory, public_file]), \
             mock.patch.object(monitor.os, "close") as close:
            self.assertFalse(monitor.backup_artifacts(json.loads(self.receipt())))
        self.assertEqual(close.call_args_list, [mock.call(11), mock.call(10)])

    def test_unsafe_receipt_permissions_and_unsafe_token_never_reach_sender(self):
        def unsafe(*args, **kwargs):
            raise monitor.OpsError("OPS_UNSAFE_FILE")
        values = monitor.backup_checks(self.config, self.now, unsafe)
        self.assertFalse(next(item["ok"] for item in values if item["code"] == "backup_local"))
        opener = mock.Mock()
        self.assertFalse(monitor.telegram_send(self.config, "fixed", opener=opener, reader=unsafe))
        opener.open.assert_not_called()

    def test_telegram_requires_http_success_and_telegram_ok_without_logging_body(self):
        for response in (Response(b'{"ok":false,"description":"private provider data"}'),
                         Response(b'{"ok":true}', 500),
                         Response(b"private invalid response"),
                         Response(b"x" * 65537)):
            with self.subTest(body=response.body[:20], status=response.status):
                opener = mock.Mock()
                opener.open.return_value = response
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertFalse(monitor.telegram_send(self.config, "fixed", opener,
                                                           lambda *args, **kwargs: self.token))
                self.assertEqual(output.getvalue(), "")
        opener = mock.Mock()
        opener.open.return_value = Response()
        self.assertTrue(monitor.telegram_send(self.config, "fixed", opener,
                                              lambda *args, **kwargs: self.token))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data)["chat_id"], "-123456")

    def test_redirects_and_non_https_are_rejected(self):
        self.assertIsNone(monitor.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))
        opener = mock.Mock()
        self.assertFalse(monitor.public_ready("http://private.test", opener))
        opener.open.assert_not_called()
        opener.open.return_value = Response(b'{"status":"ready"}', 302)
        self.assertFalse(monitor.public_ready("https://loyalty.addwise.kz", opener))
        opener.open.return_value = Response(b'{"status":"ready"}')
        self.assertTrue(monitor.public_ready("https://loyalty.addwise.kz", opener))
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://loyalty.addwise.kz/health/ready")

    def test_public_and_notification_errors_do_not_reflect_exception_or_secrets(self):
        opener = mock.Mock()
        opener.open.side_effect = RuntimeError("token, https://secret.example/path, customer-private")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(monitor.public_ready("https://loyalty.addwise.kz", opener))
            self.assertFalse(monitor.telegram_send(self.config, "fixed", opener,
                                                  lambda *args, **kwargs: self.token))
        self.assertEqual(output.getvalue(), "")

    def test_docker_pool_health_is_not_mistaken_for_account_readiness(self):
        health = monitor.docker_checks(lambda *args, **kwargs: b"true|healthy|bonuspro:aaaaaaaaaaaa")
        self.assertTrue(all(item["ok"] for item in health))
        aggregate = {"active_accounts": 1, "stale_accounts": 1, "provider_accounts": 0,
                     "setup_accounts": 0, "attention_accounts": 0, "dead_jobs": 2}
        captured = []
        def runner(args, **kwargs):
            captured.append(args)
            return json.dumps(aggregate).encode()
        values = monitor.processing_checks(self.config["monitor"], runner)
        self.assertFalse(next(item["ok"] for item in values if item["code"] == "processing_stale"))
        self.assertFalse(next(item["ok"] for item in values if item["code"] == "dead_jobs"))
        self.assertIn("default_transaction_read_only=on", captured[0][3])
        self.assertNotIn("active_accounts", json.dumps(values))
        self.assertNotIn("tenant_id", json.dumps(values))

    def test_failed_processing_probe_does_not_invent_account_recovery(self):
        values = monitor.processing_checks(self.config["monitor"], lambda *args, **kwargs: b"private failure")
        self.assertEqual(values, [monitor.check("processing_probe", False)])

    def test_disk_checks_cover_bytes_and_inodes(self):
        info = mock.Mock(f_blocks=100, f_bavail=50, f_frsize=1024**3,
                         f_files=100, f_favail=5)
        values = monitor.disk_checks(statvfs=lambda path: info)
        self.assertTrue(next(x["ok"] for x in values if x["code"] == "disk_space"))
        self.assertFalse(next(x["ok"] for x in values if x["code"] == "disk_inodes"))

    def test_main_redacts_configuration_failures(self):
        output = io.StringIO()
        with mock.patch.object(monitor.os, "geteuid", return_value=0), \
             mock.patch.object(monitor, "load_config", side_effect=RuntimeError("private token URL")), \
             redirect_stdout(output):
            self.assertEqual(monitor.main(["--check-only"]), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "monitor_failed", "code": "monitor_failed"})


if __name__ == "__main__":
    unittest.main()
