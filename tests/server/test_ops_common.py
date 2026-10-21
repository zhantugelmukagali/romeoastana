"""Root-private configuration, atomic state and exclusive locking contracts."""
import base64
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ops_common as ops


class OperationsCommonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.change = patch.object(ops, "OWNER_UID", os.getuid())
        self.change.start()
        self.addCleanup(self.change.stop)
        self.token = self.write("telegram_token", b"123456789:" + b"a" * 35)
        self.config_path = self.root / "config.json"
        self.config = {
            "version": 1,
            "release_root": str(ops.RELEASE_ROOT),
            "backup": {"enabled": False},
            "telegram": {"token_file": str(self.token), "chat_id": "-100123456789"},
            "monitor": {
                "public_origin": "https://loyalty.addwise.kz",
                "expected_active_accounts": 1,
                "max_processing_age_seconds": 900,
                "max_backup_age_hours": 32,
            },
            "recovery_key_exported": False,
        }

    def write(self, name, body):
        path = self.root / name
        path.write_bytes(body)
        path.chmod(0o600)
        return path

    def save(self):
        ops.atomic_json(self.config_path, self.config)

    def enable_backup(self):
        storage = self.write("rclone.conf", (
            "[bonuspro-backup]\ntype=s3\nprovider=Other\n"
            "access_key_id=fixture-access\nsecret_access_key=fixture-secret\n"
            "region=us-east-1\nendpoint=https://storage.example.com\n"
            "acl=private\nno_check_bucket=true\n").encode())
        key = self.write("recovery_key", base64.b64encode(b"r" * 32))
        self.config["backup"] = {
            "enabled": True, "remote": "bonuspro-backup:customer-backups/bonuspro",
            "rclone_config": str(storage), "recovery_key_file": str(key),
            "expected_schema": "0018", "offsite_location_confirmed": True,
        }
        self.config["recovery_key_exported"] = True
        return storage, key

    def test_partial_configuration_never_invents_storage_or_key_export(self):
        self.save()
        value = ops.load_config(self.config_path)
        self.assertFalse(value["backup"]["enabled"])
        self.assertFalse(value["recovery_key_exported"])

    def test_complete_private_s3_and_telegram_configuration_is_read_only(self):
        self.enable_backup()
        self.save()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(ops.load_config(self.config_path), self.config)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_secure_read_rejects_symlinks_hardlinks_and_public_permissions(self):
        linked = self.root / "linked"
        linked.symlink_to(self.token)
        with self.assertRaises(ops.OpsError):
            ops.secure_read(linked)
        linked.unlink()
        os.link(self.token, linked)
        with self.assertRaises(ops.OpsError):
            ops.secure_read(self.token)
        linked.unlink()
        self.token.chmod(0o644)
        with self.assertRaises(ops.OpsError):
            ops.secure_read(self.token)

    def test_read_is_bounded_and_rejects_untrusted_parent(self):
        self.write("large", b"x" * 1025)
        with self.assertRaisesRegex(ops.OpsError, "OPS_FILE_TOO_LARGE"):
            ops.secure_read(self.root / "large", max_bytes=1024)
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        file = unsafe / "secret"
        file.write_bytes(b"not emitted")
        file.chmod(0o600)
        with self.assertRaises(ops.OpsError):
            ops.secure_read(file)

    def test_atomic_json_is_private_and_preserves_previous_state_on_invalid_data(self):
        target = self.root / "state.json"
        ops.atomic_json(target, {"confirmed": True})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(ops.OpsError):
            ops.atomic_json(target, {"number": float("nan")})
        self.assertEqual(json.loads(target.read_text()), {"confirmed": True})
        target.chmod(0o644)
        with self.assertRaises(ops.OpsError):
            ops.atomic_json(target, {"confirmed": False})
        self.assertEqual(json.loads(target.read_text()), {"confirmed": True})

    def test_exclusive_lock_refuses_overlap_and_releases_after_failure(self):
        target = self.root / "operation.lock"
        with self.assertRaisesRegex(ValueError, "fixture"):
            with ops.locked(target):
                with self.assertRaisesRegex(ops.OpsError, "OPS_BUSY"):
                    with ops.locked(target):
                        self.fail("Overlapping operation entered")
                raise ValueError("fixture")
        with ops.locked(target):
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        target.unlink()
        target.symlink_to(self.token)
        with self.assertRaises(ops.OpsError):
            with ops.locked(target):
                self.fail("Symlink lock entered")

    def test_shared_checkpoint_lock_allows_monitor_read_but_blocks_deployment(self):
        target = self.root / "shared.lock"
        with ops.locked(target, shared=True):
            with ops.locked(target, shared=True):
                with self.assertRaisesRegex(ops.OpsError, "OPS_BUSY"):
                    with ops.locked(target):
                        self.fail("Deployment entered during checkpoint")
        with ops.locked(target):
            with self.assertRaisesRegex(ops.OpsError, "OPS_BUSY"):
                with ops.locked(target, shared=True):
                    self.fail("Checkpoint entered during deployment")

    def test_unknown_version_bool_counts_wrong_release_and_invalid_urls_fail_closed(self):
        alternatives = [
            ("version", True), ("version", 2), ("release_root", "/tmp/foreign"),
        ]
        for key, replacement in alternatives:
            with self.subTest(key=key, value=replacement):
                original = self.config[key]
                self.config[key] = replacement
                self.save()
                with self.assertRaises(ops.OpsError):
                    ops.load_config(self.config_path)
                self.config[key] = original
        for field, replacement in (
            ("expected_active_accounts", True), ("expected_active_accounts", -1),
            ("max_processing_age_seconds", 0), ("max_backup_age_hours", 1000),
            ("public_origin", "http://loyalty.addwise.kz"),
            ("public_origin", "https://user:secret@example.com"),
            ("public_origin", "https://127.0.0.1"),
        ):
            with self.subTest(field=field, replacement=replacement):
                original = self.config["monitor"][field]
                self.config["monitor"][field] = replacement
                self.save()
                with self.assertRaises(ops.OpsError):
                    ops.load_config(self.config_path)
                self.config["monitor"][field] = original

    def test_telegram_token_must_be_private_ascii_and_not_another_directory(self):
        for value in (b"invalid", b"\xff", b"123:short"):
            self.token.write_bytes(value)
            self.save()
            with self.assertRaises(ops.OpsError) as caught:
                ops.load_config(self.config_path)
            self.assertNotIn(repr(value), str(caught.exception))
        self.config["telegram"]["token_file"] = "/etc/other-secret"
        self.save()
        with self.assertRaisesRegex(ops.OpsError, "OPS_SECRET_OUTSIDE_CONFIG"):
            ops.load_config(self.config_path)

    def test_storage_cannot_override_authentication_tls_or_embed_another_remote(self):
        storage, _ = self.enable_backup()
        original = storage.read_text()
        for addition in ("\nenv_auth=true\n", "\nno_check_certificate=true\n",
                         "\n[foreign]\ntype=local\n"):
            storage.write_text(original + addition)
            self.save()
            with self.assertRaises(ops.OpsError):
                ops.load_config(self.config_path)
        for endpoint in ("http://storage.example.com", "https://user:pass@storage.example.com",
                         "https://storage.example.com/?token=secret", "https://127.0.0.1"):
            storage.write_text(original.replace("https://storage.example.com", endpoint))
            self.save()
            with self.assertRaises(ops.OpsError):
                ops.load_config(self.config_path)

    def test_remote_traversal_and_invalid_recovery_key_fail_before_use(self):
        _, key = self.enable_backup()
        original = self.config["backup"]["remote"]
        for remote in ("bonuspro-backup:bucket/../escape", "bonuspro-backup:bucket//empty",
                       "other:bucket/private", "bonuspro-backup:/absolute"):
            self.config["backup"]["remote"] = remote
            self.save()
            with self.assertRaises(ops.OpsError):
                ops.load_config(self.config_path)
        self.config["backup"]["remote"] = original
        key.write_bytes(base64.b64encode(b"short"))
        self.save()
        with self.assertRaisesRegex(ops.OpsError, "OPS_INVALID_RECOVERY_KEY"):
            ops.load_config(self.config_path)

    def test_optional_heartbeat_is_private_https_and_never_returned_in_logs(self):
        url = self.write("heartbeat_url", b"https://example.com/ping/private-sentinel\n")
        self.config["heartbeat"] = {"url_file": str(url)}
        self.save()
        self.assertEqual(ops.load_config(self.config_path)["heartbeat"]["url_file"], str(url))
        url.write_bytes(b"https://secret@example.com/ping")
        with self.assertRaises(ops.OpsError) as caught:
            ops.load_config(self.config_path)
        self.assertNotIn("secret@", str(caught.exception))

    def test_safe_error_codes_do_not_accept_arbitrary_exception_text(self):
        error = ops.OpsError("request failed: https://token@example.com")
        self.assertEqual(str(error), "OPS_FAILED")


if __name__ == "__main__":
    unittest.main()
