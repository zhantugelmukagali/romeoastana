import base64
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ops_common
SPEC = importlib.util.spec_from_file_location("setup_operations", ROOT / "deploy/setup_operations.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
TOKEN = "123456789:" + "t" * 35


class SetupOperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config"
        self.config.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.installed = self.root / "installed"
        self.units = self.root / "units"
        self.units.mkdir(mode=0o700)
        self.patches = [
            patch.object(setup, "OWNER_UID", os.getuid()),
            patch.object(ops_common, "OWNER_UID", os.getuid()),
            patch.object(setup, "CONFIG_DIR", self.config),
            patch.object(setup, "STATE_DIR", self.state),
            patch.object(setup, "INSTALL_DIR", self.installed),
            patch.object(setup, "UNIT_DIR", self.units),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def private(self, path, value):
        path.write_bytes(value)
        path.chmod(0o600)
        return path

    def candidate(self, enabled=False):
        return {
            "version": 1, "release_root": str(setup.RELEASE_ROOT),
            "backup": {"enabled": enabled, "offsite_location_confirmed": enabled,
                       "expected_schema": "0018"},
            "telegram": {"chat_id": "123", "token_file": str(self.config / "telegram_token")},
            "monitor": {"public_origin": "https://loyalty.addwise.kz",
                        "expected_active_accounts": 1, "max_processing_age_seconds": 900,
                        "max_backup_age_hours": 32},
            "recovery_key_exported": False,
        }

    def probe(self, ready=False, **extra):
        return {"status": "checked", "ready": ready, "technical_ready": True,
                "checks": [{"code": "backup_not_configured", "ok": ready, "severity": "warning"}],
                **extra}

    def execute_idle(self, arguments, **kwargs):
        if "is-active" in arguments:
            return subprocess.CompletedProcess(arguments, 3, b"")
        return subprocess.CompletedProcess(arguments, 0, b"")

    def test_atomic_install_keeps_helpers_outside_checkout_and_does_not_touch_app_secrets(self):
        source = self.root / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "deploy").mkdir()
        for name in setup.HELPERS:
            (source / "scripts" / name).write_bytes(("fixture " + name).encode())
        for name in setup.UNITS:
            (source / "deploy" / name).write_bytes(("unit " + name).encode())
        app_secret = self.private(source / "deploy/app-secret", b"unchanged-app-secret")
        calls = []
        with patch.object(setup, "checked", side_effect=lambda args, **kwargs: calls.append(args) or b""):
            setup.install_files(source)
        for name in setup.HELPERS:
            self.assertEqual((self.installed / name).read_bytes(), ("fixture " + name).encode())
            self.assertEqual((self.installed / name).stat().st_mode & 0o777, 0o700)
        for name in setup.UNITS:
            self.assertEqual((self.units / name).stat().st_mode & 0o777, 0o644)
        self.assertEqual(app_secret.read_bytes(), b"unchanged-app-secret")
        self.assertEqual(calls, [[setup.SYSTEMCTL, "daemon-reload"]])

    def test_install_rejects_symlink_source_and_keeps_destination_untouched(self):
        source = self.root / "source"
        (source / "scripts").mkdir(parents=True)
        victim = self.private(self.root / "victim", b"never-copy")
        (source / "scripts" / setup.HELPERS[0]).symlink_to(victim)
        with self.assertRaises(setup.OpsError):
            setup.install_files(source)
        self.assertEqual(victim.read_bytes(), b"never-copy")
        self.assertFalse((self.installed / setup.HELPERS[0]).exists())

    def test_atomic_writer_rejects_symlink_and_existing_hardlink(self):
        victim = self.private(self.config / "victim", b"protected")
        symlink = self.config / "symlink"
        symlink.symlink_to(victim)
        with self.assertRaises(setup.OpsError):
            setup.write_file(symlink, b"bad")
        linked = self.config / "hardlink"
        os.link(victim, linked)
        with self.assertRaises(setup.OpsError):
            setup.write_file(linked, b"bad")
        self.assertEqual(victim.read_bytes(), b"protected")

    def test_exclusive_secret_write_never_replaces_an_existing_key(self):
        key = self.private(self.config / "key", b"old")
        with self.assertRaises(setup.OpsError):
            setup.write_file(key, b"new", exclusive=True)
        self.assertEqual(key.read_bytes(), b"old")

    def test_recovery_key_is_generated_once_and_unsafe_key_is_not_replaced(self):
        path = self.config / "recovery_key"
        setup.ensure_recovery_key(path)
        original = path.read_bytes()
        self.assertEqual(len(base64.b64decode(original, validate=False)), 32)
        setup.ensure_recovery_key(path)
        self.assertEqual(path.read_bytes(), original)
        path.chmod(0o644)
        with self.assertRaises(setup.OpsError):
            setup.ensure_recovery_key(path)
        self.assertEqual(path.read_bytes(), original)

    def test_new_telegram_credentials_use_new_file_and_preserve_running_config_secret(self):
        old = self.private(self.config / "telegram_token", (TOKEN + "\n").encode())
        same = setup._secret_file("telegram_token", TOKEN, str(old))
        self.assertEqual(same, str(old))
        replacement = setup._secret_file("telegram_token", TOKEN + "x", str(old))
        self.assertNotEqual(replacement, str(old))
        self.assertEqual(old.read_text(), TOKEN + "\n")
        self.assertEqual(Path(replacement).read_text(), TOKEN + "x\n")
        self.assertEqual(Path(replacement).stat().st_mode & 0o777, 0o600)

    def test_source_revision_refuses_dirty_checkout_and_wrong_expected_commit(self):
        source = self.root / "source"
        with patch.object(setup, "checked", side_effect=[b"a" * 40, b" M scripts/ops_common.py\n"]):
            with self.assertRaises(setup.OpsError) as error:
                setup.source_revision(source)
        self.assertEqual(error.exception.code, "OPS_SOURCE_DIRTY")
        with patch.object(setup, "checked", return_value=b"a" * 40):
            with self.assertRaises(setup.OpsError) as error:
                setup.source_revision(source, "b" * 40)
        self.assertEqual(error.exception.code, "OPS_SOURCE_REVISION_MISMATCH")

    def test_configure_requires_interactive_terminal_before_reading_secrets(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), \
                patch.object(setup.getpass, "getpass") as hidden:
            with self.assertRaises(setup.OpsError):
                setup.configure(self.config / "config.json")
        hidden.assert_not_called()

    def test_partial_configuration_enables_no_storage_and_exposes_no_token(self):
        output = io.StringIO()
        target = self.config / "config.json"
        with patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.getpass, "getpass", side_effect=[TOKEN, ""]), \
                patch("builtins.input", side_effect=["123", "n"]), \
                patch("sys.stdout", output):
            value = setup.configure(target)
        self.assertFalse(value["backup"]["enabled"])
        self.assertFalse(value["recovery_key_exported"])
        self.assertNotIn("heartbeat", value)
        self.assertEqual(ops_common.load_config(target)["telegram"]["chat_id"], "123")
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn("secret_access_key", output.getvalue())

    def test_invalid_new_configuration_keeps_previous_published_configuration(self):
        old_token = self.private(self.config / "telegram_token", (TOKEN + "\n").encode())
        target = self.config / "config.json"
        old = self.candidate(False)
        ops_common.atomic_json(target, old)
        before = target.read_bytes()
        with patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.getpass, "getpass", return_value=TOKEN + "x"), \
                patch("builtins.input", side_effect=["123", "y", "http://storage.example.test"]):
            with self.assertRaises(setup.OpsError):
                setup.configure(target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(old_token.read_text(), TOKEN + "\n")

    def test_no_token_configuration_stays_pending_without_publishing_invalid_json(self):
        target = self.config / "config.json"
        with patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.getpass, "getpass", return_value=""), \
                patch("sys.stdout", io.StringIO()):
            self.assertIsNone(setup.configure(target))
        self.assertFalse(target.exists())

    def test_chat_detection_requires_one_recent_private_start_from_that_user(self):
        class Response:
            status = 200
            def __init__(self, updates): self.updates = updates
            def read(self, size): return json.dumps({"ok": True, "result": self.updates}).encode()
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def message(identifier, date=1000):
            return {"message": {"text": "/start", "date": date,
                    "chat": {"id": identifier, "type": "private"},
                    "from": {"id": identifier, "is_bot": False}}}
        class Opener:
            def __init__(self, updates): self.updates = updates
            def open(self, *args, **kwargs): return Response(self.updates)
        with patch.object(setup, "build_opener", return_value=Opener([message(123)])), \
                patch.object(setup.time, "time", return_value=1100):
            self.assertEqual(setup.detect_chat(TOKEN, 900), "123")
        with patch.object(setup, "build_opener", return_value=Opener([message(123), message(456)])), \
                patch.object(setup.time, "time", return_value=1100):
            with self.assertRaises(setup.OpsError) as error:
                setup.detect_chat(TOKEN, 900)
        self.assertEqual(error.exception.code, "OPS_TELEGRAM_CHAT_NOT_UNAMBIGUOUS")

    def test_disabled_storage_keeps_observability_but_never_claims_full_readiness(self):
        calls = []
        with patch.object(setup, "load_config", return_value=self.candidate()), \
                patch.object(setup, "checked", side_effect=lambda args, **kw: calls.append(args) or b""), \
                patch.object(setup, "monitor_result", return_value=self.probe(False)):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["status"], "OPS_SETUP_PENDING")
        self.assertTrue(result["monitor_enabled"])
        self.assertFalse(result["offsite_backup_enabled"])
        self.assertFalse(result["business_acceptance_verified"])
        self.assertTrue(any(args[-1] == setup.MONITOR_TIMER for args in calls))
        self.assertFalse(any("offsite_backup.py" in " ".join(args) for args in calls))
        self.assertFalse(any(setup.BACKUP_TIMER in args for args in calls))

    def test_backup_must_finish_and_have_receipt_before_backup_timer_and_full_readiness(self):
        calls = []
        config = self.candidate(True)
        reports = [self.probe(True), self.probe(True, heartbeat_delivered=True)]
        with patch.object(setup, "load_config", return_value=config), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/rclone"), \
                patch.object(setup, "execute", side_effect=self.execute_idle), \
                patch.object(setup, "checked", side_effect=lambda args, **kw: calls.append(args) or b""), \
                patch.object(setup, "verified_receipt", side_effect=lambda *a: calls.append(["RECEIPT"]) or {}), \
                patch.object(setup, "monitor_result", side_effect=reports):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["status"], "OPS_READY")
        backup_index = next(i for i, args in enumerate(calls) if "offsite_backup.py" in " ".join(args))
        receipt_index = calls.index(["RECEIPT"])
        timer_index = next(i for i, args in enumerate(calls) if setup.BACKUP_TIMER in args)
        self.assertLess(backup_index, receipt_index)
        self.assertLess(receipt_index, timer_index)
        self.assertFalse(result["independent_restore_verified"])
        self.assertFalse(result["business_acceptance_verified"])

    def test_failed_offsite_backup_never_enables_backup_timer_and_keeps_monitoring(self):
        calls = []
        def invoke(args, **kwargs):
            calls.append(args)
            if "offsite_backup.py" in " ".join(args):
                raise setup.OpsError("SYNTHETIC_BACKUP_FAILURE")
            return b""
        with patch.object(setup, "load_config", return_value=self.candidate(True)), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/rclone"), \
                patch.object(setup, "execute", side_effect=self.execute_idle), \
                patch.object(setup, "checked", side_effect=invoke):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["reason"], "SYNTHETIC_BACKUP_FAILURE")
        self.assertEqual(result["status"], "OPS_SETUP_PENDING")
        self.assertFalse(any(setup.BACKUP_TIMER in args for args in calls))
        self.assertTrue(any(setup.MONITOR_TIMER in args for args in calls))

    def test_missing_rclone_does_not_attempt_a_backup_but_monitor_is_enabled(self):
        calls = []
        with patch.object(setup, "load_config", return_value=self.candidate(True)), \
                patch.object(setup.shutil, "which", return_value=None), \
                patch.object(setup, "checked", side_effect=lambda args, **kw: calls.append(args) or b""):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["reason"], "OPS_RCLONE_NOT_INSTALLED")
        self.assertFalse(any("offsite_backup.py" in " ".join(args) for args in calls))
        self.assertTrue(any(setup.MONITOR_TIMER in args for args in calls))

    def test_active_legacy_backup_is_not_killed_or_raced(self):
        calls = []
        with patch.object(setup, "load_config", return_value=self.candidate(True)), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/rclone"), \
                patch.object(setup, "execute", return_value=subprocess.CompletedProcess([], 0, b"")), \
                patch.object(setup, "checked", side_effect=lambda args, **kw: calls.append(args) or b""):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["reason"], "OPS_LEGACY_BACKUP_RUNNING")
        self.assertFalse(any("offsite_backup.py" in " ".join(args) for args in calls))
        self.assertFalse(any("stop" in args and "bonuspro-backup.service" in args for args in calls))

    def test_success_without_confirmed_heartbeat_is_not_ops_ready(self):
        calls = []
        with patch.object(setup, "load_config", return_value=self.candidate(True)), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/rclone"), \
                patch.object(setup, "execute", side_effect=self.execute_idle), \
                patch.object(setup, "checked", side_effect=lambda args, **kw: calls.append(args) or b""), \
                patch.object(setup, "verified_receipt", return_value={}), \
                patch.object(setup, "monitor_result", side_effect=[
                    self.probe(True), self.probe(True, heartbeat_delivered=False)]):
            result = setup.enable_operations(self.config / "config.json")
        self.assertEqual(result["status"], "OPS_SETUP_PENDING")
        self.assertEqual(result["reason"], "OPS_HEARTBEAT_NOT_CONFIRMED")
        self.assertTrue(result["offsite_backup_enabled"])

    def test_receipt_requires_fresh_download_restore_and_recovery_bundle_proofs(self):
        now = datetime.now(timezone.utc)
        good = {"completed_at": now.isoformat(), "schema": "0018",
                "image": "bonuspro:" + "a" * 12, "sha256": "b" * 64, "bytes": 1024,
                "offsite_verified": True, "restoration_verified": True,
                "recovery_bundle_verified": True}
        path = self.state / "backup-success.json"
        config = self.candidate(True)
        ops_common.atomic_json(path, good)
        self.assertEqual(setup.verified_receipt(config, now - timedelta(seconds=5))["bytes"], 1024)
        invalid = [
            {"offsite_verified": False}, {"restoration_verified": False},
            {"recovery_bundle_verified": False}, {"image": "bonuspro:latest"},
            {"completed_at": (now - timedelta(days=1)).isoformat()},
            {"completed_at": (now + timedelta(hours=1)).isoformat()},
            {"completed_at": now.replace(tzinfo=None).isoformat()},
        ]
        for change in invalid:
            with self.subTest(change=change):
                ops_common.atomic_json(path, {**good, **change})
                with self.assertRaises(setup.OpsError):
                    setup.verified_receipt(config, now - timedelta(seconds=5))

    def test_readonly_monitor_protocol_cannot_report_success_with_failed_checks(self):
        inconsistent = self.probe(False)
        with patch.object(setup, "execute", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(inconsistent).encode())):
            with self.assertRaises(setup.OpsError):
                setup.monitor_result(self.config / "config.json")

    def test_combined_setup_restores_previous_config_after_credential_verification_failure(self):
        old_token = self.private(self.config / "telegram_token", (TOKEN + "\n").encode())
        old = self.candidate(False)
        target = self.config / "config.json"
        ops_common.atomic_json(target, old)
        orphan = self.private(self.config / "telegram_token-new", (TOKEN + "x\n").encode())
        new = {**old, "telegram": {"chat_id": "456", "token_file": str(orphan)}}
        def configure(path):
            ops_common.atomic_json(path, new)
            return new
        with patch.object(setup, "configure", side_effect=configure), \
                patch.object(setup, "enable_operations", side_effect=setup.OpsError("OPS_COMMAND_FAILED")):
            with self.assertRaises(setup.OpsError):
                setup.configure_and_enable(target)
        self.assertEqual(json.loads(target.read_text()), old)
        self.assertEqual(old_token.read_text(), TOKEN + "\n")
        self.assertEqual(orphan.read_text(), TOKEN + "x\n")

    def test_combined_setup_keeps_intentional_partial_change_and_working_monitoring(self):
        self.private(self.config / "telegram_token", (TOKEN + "\n").encode())
        storage = self.private(self.config / "rclone.conf", (
            "[bonuspro-backup]\ntype = s3\nprovider = Other\n"
            "access_key_id = example\nsecret_access_key = private-example\n"
            "region = us-east-1\nendpoint = https://storage.example.test\n"
            "acl = private\nno_check_bucket = true\n").encode())
        recovery = self.private(self.config / "recovery_key", base64.b64encode(b"k" * 32))
        old = self.candidate(True)
        old["backup"].update(remote="bonuspro-backup:example/bonuspro",
                             rclone_config=str(storage), recovery_key_file=str(recovery))
        target = self.config / "config.json"
        ops_common.atomic_json(target, old)
        new = {**old, "backup": {**old["backup"], "enabled": False}}
        def configure(path):
            ops_common.atomic_json(path, new)
            return new
        outcome = {"status": "OPS_SETUP_PENDING", "reason": "OPS_READINESS_INCOMPLETE",
                   "monitor_enabled": True}
        with patch.object(setup, "configure", side_effect=configure), \
                patch.object(setup, "enable_operations", return_value=outcome):
            result = setup.configure_and_enable(target)
        self.assertEqual(json.loads(target.read_text()), new)
        self.assertTrue(result["monitor_enabled"])
        self.assertNotIn("configuration_restored", result)
        self.assertTrue(storage.exists())
        self.assertTrue(recovery.exists())


    def test_units_run_only_installed_code_with_fixed_environment_and_do_not_hide_docker_outage(self):
        monitor = (ROOT / "deploy/bonuspro-monitor.service").read_text()
        backup = (ROOT / "deploy/bonuspro-offsite-backup.service").read_text()
        for service in (monitor, backup):
            self.assertIn("User=root", service)
            self.assertIn("UMask=0077", service)
            self.assertIn("/usr/bin/python3 -E -s /usr/local/lib/bonuspro-ops/", service)
            self.assertIn("Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin", service)
            self.assertNotIn("WorkingDirectory=/home/ubuntu", service)
        self.assertNotIn("Requires=docker.service", monitor)
        self.assertIn("Requires=docker.service", backup)
        self.assertIn("OnFailure=bonuspro-monitor.service", backup)
        timer = (ROOT / "deploy/bonuspro-offsite-backup.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 03:00:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnUnitInactiveSec=60s", (ROOT / "deploy/bonuspro-monitor.timer").read_text())


if __name__ == "__main__":
    unittest.main()
