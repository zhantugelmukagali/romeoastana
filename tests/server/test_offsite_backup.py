"""Offsite completion requires verified downloads and successful temporary restore."""
import base64
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import offsite_backup as backup
import ops_common
from bonuspro.backup_stream import encrypt, decrypt

def load_verifier():
    spec = importlib.util.spec_from_file_location(
        "offsite_test_verifier", ROOT / "scripts/verify_backup_vps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Transfer:
    def __init__(self, test):
        self.test = test
        self.objects = {}
        self.events = []
        self.fail_upload = None
        self.corrupt_download = None
        self.fail_restore = False
        self.raise_secret = False

    def __call__(self, args, **options):
        self.events.append(tuple(args))
        self.test.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.test.assertNotIn("RCLONE_SECRET_SENTINEL", options["env"])
        if self.raise_secret:
            raise RuntimeError("SECRET_SENTINEL https://secret.invalid/token")
        if args[0] == str(backup.RCLONE):
            index = args.index("copyto")
            source, target = args[index + 1:index + 3]
            self.test.assertIn("--s3-no-check-bucket", args)
            self.test.assertIn("--immutable", args)
            self.test.assertFalse(any(item in args for item in ("delete", "purge", "sync", "move")))
            if source.startswith("bonuspro-backup:"):
                value = self.objects[source]
                if self.corrupt_download and source.endswith(self.corrupt_download):
                    value = bytes([value[0] ^ 1]) + value[1:]
                Path(target).write_bytes(value)
                Path(target).chmod(0o600)
            else:
                if self.fail_upload and target.endswith(self.fail_upload):
                    return subprocess.CompletedProcess(args, 1, b"")
                self.objects[target] = Path(source).read_bytes()
            return subprocess.CompletedProcess(args, 0, b"")
        if args[0] == sys.executable:
            self.test.assertEqual(args[1], "-I")
            self.test.assertNotIn("PYTHONPATH", options["env"])
            self.test.assertNotIn("PYTHONHOME", options["env"])
            if args[2].endswith("/backup_vps.py"):
                path = backup.BACKUP_DIR / "20261021T000000Z-1.pgdump.bpbk"
                value = b"BPBK0001" + b"database-ciphertext" * 12
                path.write_bytes(value)
                path.chmod(0o600)
                payload = {"status": "encrypted_backup_created", "file": str(path),
                           "sha256": hashlib.sha256(value).hexdigest()}
                return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode())
            if args[2].endswith("/verify_backup_vps.py"):
                path = Path(args[3])
                self.test.assertTrue(path.is_file())
                self.test.assertTrue(path.with_suffix(".bpbk.sha256").is_file())
                self.test.assertTrue(any(key.endswith("/database.pgdump.bpbk")
                                         for key in self.objects))
                return subprocess.CompletedProcess(
                    args, 1 if self.fail_restore else 0, b"ENCRYPTED_BACKUP_RESTORE_OK\n")
        if args[0] == "docker":
            self.test.assertEqual(args[args.index("--user") + 1], "0:0")
            self.test.assertEqual(args[args.index("--network") + 1], "none")
            mount = args[args.index("--mount") + 1]
            key_file = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            key = base64.b64decode(Path(key_file).read_bytes())
            function = encrypt if "encrypt" in args else decrypt
            function(options["stdin"], options["stdout"], key)
            return subprocess.CompletedProcess(args, 0, b"")
        raise AssertionError("Unexpected command")

class OffsiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = self.root / "release"
        self.destination = self.root / "encrypted"
        self.state = self.root / "state"
        self.library = self.root / "library"
        self.executable = self.root / "rclone"
        for path in (self.release, self.destination, self.state, self.library):
            path.mkdir(mode=0o700)
        self.executable.write_text("fixture")
        self.executable.chmod(0o700)
        for name in ("backup_vps.py", "verify_backup_vps.py"):
            (self.library / name).write_text("trusted fixture")
            (self.library / name).chmod(0o600)
        self.make(".env.release", b"BONUSPRO_IMAGE=bonuspro:fa139a7b6635\n")
        self.make("compose.release.yaml", b"runtime compose")
        self.make("compose.https.yaml", b"HTTPS compose")
        self.make("deploy/Caddyfile.release", b"loyalty.example.test")
        self.make(".marketplace-deployment.json", b'{"schema":"0018"}')
        for name in ("db_password", "admin_database_url", "migration_database_url",
                     "app_database_url", "lifecycle_database_url", "vendor_secret"):
            self.make("deploy/secrets/" + name, ("SECRET_" + name).encode())
        self.make("deploy/secrets/token_encryption_key", base64.b64encode(b"a" * 32))
        self.recovery_key = self.root / "recovery_key"
        self.recovery_key.write_bytes(base64.b64encode(b"r" * 32))
        self.recovery_key.chmod(0o600)
        self.rclone_config = self.root / "rclone.conf"
        self.rclone_config.write_text(
            "[bonuspro-backup]\ntype = s3\nendpoint = https://storage.example.test\n")
        self.rclone_config.chmod(0o600)
        self.config = {
            "release_root": str(self.release), "recovery_key_exported": True,
            "backup": {"enabled": True, "remote": "bonuspro-backup:bucket/private",
                       "rclone_config": str(self.rclone_config), "expected_schema": "0018",
                       "recovery_key_file": str(self.recovery_key),
                       "offsite_location_confirmed": True}}
        self.patches = [
            patch.object(ops_common, "OWNER_UID", os.getuid()),
            patch.object(backup, "RELEASE_ROOT", self.release),
            patch.object(backup, "BACKUP_DIR", self.destination),
            patch.object(backup, "HELPER_DIR", self.library),
            patch.object(backup, "RCLONE", self.executable),
            patch.object(backup, "secure_read", side_effect=lambda path: Path(path).read_bytes()),
            patch.dict(os.environ, {"RCLONE_SECRET_SENTINEL": "must not reach subprocess",
                                    "PYTHONPATH": "/writable/checkout", "PYTHONHOME": "/writable/home"}),
        ]
        for change in self.patches:
            change.start()
            self.addCleanup(change.stop)
        self.lock_events = []

        @contextmanager
        def lock(path, *, shared=False):
            self.lock_events.append(("enter", path, shared))
            try:
                yield
            finally:
                self.lock_events.append(("exit", path, shared))
        change = patch.object(backup, "locked", side_effect=lock)
        change.start()
        self.addCleanup(change.stop)
        self.transfer = Transfer(self)

    def make(self, name, value):
        path = self.release / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o600)
        return path

    def perform(self):
        return backup.run_backup(
            self.config, run=self.transfer, state_dir=self.state,
            now=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc))

    def assert_no_success(self):
        self.assertFalse((self.state / "backup-success.json").exists())
        self.assertFalse(any(key.endswith("/manifest.json") for key in self.transfer.objects))

    def test_success_requires_download_restore_and_independent_key_bundle(self):
        receipt = self.perform()
        self.assertTrue(receipt["offsite_verified"])
        self.assertTrue(receipt["restoration_verified"])
        self.assertTrue(receipt["recovery_bundle_verified"])
        self.assertFalse(receipt["independent_machine_restore_verified"])
        self.assertEqual(receipt["schema"], "0018")
        self.assertEqual(receipt["image"], "bonuspro:fa139a7b6635")
        self.assertEqual(json.loads((self.state / "backup-success.json").read_text()), receipt)
        self.assertTrue((self.destination / receipt["database_file"]).is_file())
        self.assertTrue((self.destination / receipt["recovery_file"]).is_file())
        self.assertEqual(len(self.transfer.objects), 5)
        last = self.transfer.events[-1]
        self.assertTrue(last[last.index("copyto") + 2].endswith("/manifest.json"))
        self.assertEqual([event[0] for event in self.lock_events],
                         ["enter", "enter", "exit", "exit"])
        self.assertEqual(self.lock_events[0][2], False)
        self.assertEqual(self.lock_events[1][2], True)
        encrypted = next(value for key, value in self.transfer.objects.items()
                         if key.endswith("/recovery.tar.bpbk"))
        self.assertNotIn(b"SECRET_vendor_secret", encrypted)
        plaintext = io.BytesIO()
        decrypt(io.BytesIO(encrypted), plaintext, b"r" * 32)
        plaintext.seek(0)
        with tarfile.open(fileobj=plaintext) as archive:
            names = archive.getnames()
            self.assertIn("deploy/secrets/token_encryption_key", names)
            self.assertIn(".env.release", names)
            self.assertNotIn("recovery_key", names)
            self.assertEqual(archive.extractfile("deploy/secrets/token_encryption_key").read(),
                             base64.b64encode(b"a" * 32))
        with self.assertRaises(Exception):
            decrypt(io.BytesIO(encrypted), io.BytesIO(), b"a" * 32)

    def test_disabled_unexported_and_unconfirmed_location_fail_before_writes(self):
        for owner, field in ((self.config["backup"], "enabled"),
                             (self.config, "recovery_key_exported"),
                             (self.config["backup"], "offsite_location_confirmed")):
            with self.subTest(field=field):
                previous = owner[field]
                owner[field] = False
                with self.assertRaises(backup.OpsError):
                    self.perform()
                self.assertEqual(self.transfer.events, [])
                self.assertEqual(list(self.destination.iterdir()), [])
                self.assertEqual(self.lock_events, [])
                owner[field] = previous

    def test_recovery_key_must_differ_from_application_token_key(self):
        self.recovery_key.write_bytes(base64.b64encode(b"a" * 32))
        with self.assertRaises(backup.OpsError):
            backup.validate(self.config)
        self.assertEqual(self.transfer.events, [])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_remote_traversal_wrong_schema_and_http_endpoint(self):
        for field, value in (("remote", "bonuspro-backup:bucket/../escape"),
                             ("remote", "https://credential@example.test"),
                             ("expected_schema", "0015")):
            with self.subTest(field=field):
                previous = self.config["backup"][field]
                self.config["backup"][field] = value
                with self.assertRaises(backup.OpsError):
                    backup.validate(self.config)
                self.config["backup"][field] = previous
        self.rclone_config.write_text("[bonuspro-backup]\ntype=s3\nendpoint=http://unsafe.test\n")
        with self.assertRaises(backup.OpsError):
            backup.validate(self.config)
        self.assertEqual(self.transfer.events, [])

    def test_corrupted_database_or_recovery_download_never_commits_receipt(self):
        for name in ("database.pgdump.bpbk", "recovery.tar.bpbk"):
            with self.subTest(name=name):
                self.transfer.corrupt_download = name
                with self.assertRaises(backup.OpsError):
                    self.perform()
                self.assert_no_success()
                self.assertFalse(any(command[2].endswith("/verify_backup_vps.py")
                                     for command in self.transfer.events
                                     if command[0] == sys.executable))
                # The helper's deterministic fixture name is safe to recreate
                # after discarding this unsuccessful isolated attempt.
                self.transfer.events.clear()

    def test_failed_restore_or_upload_leaves_no_completion_manifest(self):
        self.transfer.fail_restore = True
        with self.assertRaises(backup.OpsError):
            self.perform()
        self.assert_no_success()

    def test_failed_manifest_upload_does_not_publish_local_success(self):
        self.transfer.fail_upload = "/manifest.json"
        with self.assertRaises(backup.OpsError):
            self.perform()
        self.assert_no_success()

    def test_secret_symlink_is_rejected_before_archiving(self):
        path = self.release / "deploy/secrets/vendor_secret"
        path.unlink()
        path.symlink_to(self.recovery_key)
        with self.assertRaises(backup.OpsError):
            self.perform()
        self.assert_no_success()

    def test_recovery_archive_rejects_links_and_unexpected_members_without_extracting(self):
        expected = {"safe": b"value"}
        for member_name, member_type in (("../escape", tarfile.REGTYPE),
                                         ("safe", tarfile.SYMTYPE)):
            with self.subTest(member_name=member_name):
                plaintext = io.BytesIO()
                with tarfile.open(fileobj=plaintext, mode="w") as archive:
                    member = tarfile.TarInfo(member_name)
                    member.mode = 0o600
                    member.type = member_type
                    member.linkname = "/outside"
                    member.size = 0 if member_type == tarfile.SYMTYPE else 5
                    archive.addfile(member, None if member_type == tarfile.SYMTYPE
                                    else io.BytesIO(b"value"))
                encrypted = self.destination / "unsafe.bundle"
                output = io.BytesIO()
                encrypt(io.BytesIO(plaintext.getvalue()), output, b"r" * 32)
                encrypted.write_bytes(output.getvalue())
                encrypted.chmod(0o600)
                with self.assertRaises(backup.OpsError):
                    backup.verify_recovery(
                        encrypted, expected, "bonuspro:fa139a7b6635",
                        self.recovery_key, backup.Commands(run=self.transfer))
                self.assertFalse((self.root / "escape").exists())

    def test_subprocess_exceptions_are_redacted_and_failed_run_preserves_previous_success(self):
        previous = {"completed_at": "2026-10-20T00:00:00+00:00"}
        backup.atomic_json(self.state / "backup-success.json", previous)
        self.transfer.raise_secret = True
        output = io.StringIO()
        with patch.object(backup.os, "geteuid", return_value=0), \
             patch.object(backup, "load_config", return_value=self.config), \
             patch.object(backup, "run_backup", side_effect=backup.OpsError("BACKUP_UPLOAD_FAILED")), \
             redirect_stdout(output):
            result = backup.main([])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "OFFSITE_BACKUP_FAILED BACKUP_UPLOAD_FAILED\n")
        with self.assertRaises(backup.OpsError) as caught:
            self.perform()
        self.assertNotIn("SECRET_SENTINEL", str(caught.exception))
        self.assertEqual(json.loads((self.state / "backup-success.json").read_text()), previous)

    def test_check_config_has_no_commands_or_receipts(self):
        with patch.object(backup.os, "geteuid", return_value=0), \
             patch.object(backup, "load_config", return_value=self.config), \
             patch.object(backup, "validate") as validate, \
             patch.object(backup, "run_backup") as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(backup.main(["--check-config"]), 0)
        validate.assert_called_once_with(self.config)
        run.assert_not_called()
        self.assertFalse((self.state / "backup-success.json").exists())

class RestoreCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "test.pgdump.bpbk"
        value = b"encrypted fixture"
        self.source.write_bytes(value)
        self.source.with_suffix(".bpbk.sha256").write_text(
            hashlib.sha256(value).hexdigest() + "  " + self.source.name + "\n")
        self.verifier = load_verifier()
        self.dropped = False
        self.fail_drop = False
        self.fail_restore = False

    def command(self, args, **options):
        if "dropdb" in args:
            self.dropped = True
            if self.fail_drop:
                raise subprocess.CalledProcessError(1, args)
        if "pg_restore" in args and self.fail_restore:
            raise subprocess.CalledProcessError(1, args)
        value = b"0018\n0\n" if "psql" in args else b""
        return subprocess.CompletedProcess(args, 0, value)

    def verify(self):
        output = io.StringIO()
        with patch.object(self.verifier, "DEST", self.directory), \
             patch.object(self.verifier.os, "geteuid", return_value=0), \
             patch.object(self.verifier, "command", side_effect=self.command), \
             patch.object(sys, "argv", [
                 "verify_backup_vps.py", str(self.source), "--image",
                 "bonuspro:fa139a7b6635", "--expected-schema", "0018"]), \
             redirect_stdout(output):
            self.verifier.main()
        return output.getvalue()

    def test_success_marker_is_written_only_after_temporary_database_cleanup(self):
        self.assertEqual(self.verify(), "ENCRYPTED_BACKUP_RESTORE_OK\n")
        self.assertTrue(self.dropped)
        self.assertTrue(self.source.with_suffix(".bpbk.verified.json").exists())

    def test_cleanup_failure_is_nonzero_without_success_marker(self):
        self.fail_drop = True
        with self.assertRaisesRegex(SystemExit, "RESTORE_CLEANUP_FAILED"):
            self.verify()
        self.assertTrue(self.dropped)
        self.assertFalse(self.source.with_suffix(".bpbk.verified.json").exists())

    def test_restore_failure_still_drops_temporary_database_and_never_marks_success(self):
        self.fail_restore = True
        with self.assertRaisesRegex(SystemExit, "RESTORE_CHECK_FAILED"):
            self.verify()
        self.assertTrue(self.dropped)
        self.assertFalse(self.source.with_suffix(".bpbk.verified.json").exists())

if __name__ == "__main__":
    unittest.main()
