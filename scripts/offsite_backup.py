"""Encrypted offsite checkpoints and downloaded recovery verification.

Only installed root-owned helper code is executed. No remote deletion or live
restore is performed. The separate recovery key must be retained by the owner.
"""
import argparse
import base64
import configparser
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

from ops_common import CONFIG_PATH, STATE_DIR, OpsError, load_config, secure_read, atomic_json, locked

RELEASE_ROOT = Path("/home/ubuntu/bonuspro-release")
BACKUP_DIR = Path("/home/ubuntu/bonuspro-backups/encrypted")
HELPER_DIR = Path("/usr/local/lib/bonuspro-ops")
RCLONE = Path("/usr/bin/rclone")
SCHEMA = "0018"
MAX_CONFIG = 8 * 1024 * 1024
ERROR_CODES = {
    "BACKUP_NOT_CONFIGURED", "BACKUP_CONFIG_INVALID", "RECOVERY_KEY_NOT_EXPORTED",
    "OFFSITE_LOCATION_UNCONFIRMED",
    "BACKUP_FILES_INVALID", "BACKUP_CREATE_FAILED", "BACKUP_ENCRYPT_FAILED",
    "BACKUP_UPLOAD_FAILED", "BACKUP_DOWNLOAD_FAILED", "BACKUP_HASH_MISMATCH",
    "BACKUP_RESTORE_FAILED", "RECOVERY_BUNDLE_INVALID", "BACKUP_MANIFEST_FAILED",
    "BACKUP_RECEIPT_FAILED", "BACKUP_TIMEOUT", "BACKUP_ROOT_REQUIRED",
}

def fail(code):
    raise OpsError(code)

def regular(path, *, private=False, trusted=False, limit=None):
    path = Path(path)
    try:
        for parent in (path, *path.parents):
            if parent.is_symlink():
                fail("BACKUP_FILES_INVALID")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            fail("BACKUP_FILES_INVALID")
        if private and info.st_mode & 0o077:
            fail("BACKUP_FILES_INVALID")
        if trusted and info.st_uid != os.geteuid():
            fail("BACKUP_FILES_INVALID")
        if limit is not None and info.st_size > limit:
            fail("BACKUP_FILES_INVALID")
        return info
    except OpsError:
        raise
    except Exception:
        fail("BACKUP_FILES_INVALID")

def read_file(path, *, private=False, limit=65536):
    regular(path, private=private, limit=limit)
    try:
        with open(path, "rb") as source:
            value = source.read(limit + 1)
        if len(value) > limit:
            fail("BACKUP_FILES_INVALID")
        return value
    except OpsError:
        raise
    except Exception:
        fail("BACKUP_FILES_INVALID")

def write_new(path, value):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        fail("BACKUP_FILES_INVALID")

def file_digest(path):
    regular(path, private=True)
    value = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                value.update(block)
                size += len(block)
    except Exception:
        fail("BACKUP_FILES_INVALID")
    return value.hexdigest(), size

def validate(config):
    backup = config.get("backup", {})
    if not isinstance(backup, dict) or backup.get("enabled") is not True:
        fail("BACKUP_NOT_CONFIGURED")
    if config.get("release_root") != str(RELEASE_ROOT):
        fail("BACKUP_CONFIG_INVALID")
    if backup.get("expected_schema") != SCHEMA:
        fail("BACKUP_CONFIG_INVALID")
    if config.get("recovery_key_exported") is not True:
        fail("RECOVERY_KEY_NOT_EXPORTED")
    if backup.get("offsite_location_confirmed") is not True:
        fail("OFFSITE_LOCATION_UNCONFIRMED")
    remote = backup.get("remote", "")
    if not isinstance(remote, str) or not re.fullmatch(
            r"bonuspro-backup:[A-Za-z0-9][A-Za-z0-9._/-]{0,511}", remote):
        fail("BACKUP_CONFIG_INVALID")
    if any(part in ("", ".", "..") for part in remote.split(":", 1)[1].split("/")):
        fail("BACKUP_CONFIG_INVALID")
    try:
        rclone_config = Path(backup["rclone_config"])
        key_path = Path(backup["recovery_key_file"])
        if not rclone_config.is_absolute() or not key_path.is_absolute():
            fail("BACKUP_CONFIG_INVALID")
        key = base64.b64decode(secure_read(key_path).strip(), validate=True)
        token_key = base64.b64decode(read_file(
            RELEASE_ROOT / "deploy/secrets/token_encryption_key", private=True).strip(), validate=True)
        if len(key) != 32 or len(token_key) != 32 or hmac.compare_digest(key, token_key):
            fail("BACKUP_CONFIG_INVALID")
        parsed = configparser.RawConfigParser()
        parsed.read_string(secure_read(rclone_config).decode("utf-8"))
        section = parsed["bonuspro-backup"]
        if section.get("type", "").strip().lower() != "s3":
            fail("BACKUP_CONFIG_INVALID")
        endpoint = section.get("endpoint", "").strip()
        if endpoint:
            url = urlsplit(endpoint)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.path not in ("", "/")):
                fail("BACKUP_CONFIG_INVALID")
    except OpsError:
        raise
    except Exception:
        fail("BACKUP_CONFIG_INVALID")
    for name in ("backup_vps.py", "verify_backup_vps.py"):
        regular(HELPER_DIR / name, trusted=True, limit=1024 * 1024)
    regular(RCLONE, trusted=True)
    image_lines = [
        line.split("=", 1)[1] for line in read_file(
            RELEASE_ROOT / ".env.release", private=True).decode("utf-8").splitlines()
        if line.startswith("BONUSPRO_IMAGE=")
    ]
    if len(image_lines) != 1 or not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}", image_lines[0]):
        fail("BACKUP_CONFIG_INVALID")
    return backup, image_lines[0]

class Commands:
    def __init__(self, run=subprocess.run, clock=time.monotonic):
        self.run = run
        self.clock = clock
        self.deadline = clock() + 3000

    def call(self, args, code, *, stdin=None, stdout=None, timeout=900):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            fail("BACKUP_TIMEOUT")
        # The root-owned explicit config must not be overridden by inherited
        # rclone/AWS environment variables from an interactive shell.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("RCLONE_", "AWS_"))
                       and key not in ("PYTHONPATH", "PYTHONHOME") }
        try:
            result = self.run(
                [str(arg) for arg in args], stdin=stdin,
                stdout=subprocess.PIPE if stdout is None else stdout,
                stderr=subprocess.DEVNULL, env=environment,
                timeout=min(timeout, remaining), check=False)
            if result.returncode:
                fail(code)
            value = result.stdout if stdout is None else b""
            if value is None:
                value = b""
            if len(value) > 1024 * 1024:
                fail(code)
            return value
        except OpsError:
            raise
        except Exception:
            fail(code)

def crypto_command(image, key, operation):
    return [
        "docker", "run", "--rm", "--user", "0:0", "--network", "none", "--read-only",
        "--log-driver", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--mount", "type=bind,src=" + str(key) + ",dst=/run/backup-key,readonly",
        "-i", image, "python", "-m", "bonuspro.backup_stream",
        operation, "--key-file", "/run/backup-key",
    ]

def recovery_files(image):
    result = {}
    for name in (".env.release", "compose.release.yaml", "compose.https.yaml",
                 "deploy/Caddyfile.release", ".marketplace-deployment.json"):
        result[name] = read_file(
            RELEASE_ROOT / name, private=name in (".env.release", ".marketplace-deployment.json"),
            limit=1024 * 1024)
    secrets = RELEASE_ROOT / "deploy/secrets"
    try:
        if secrets.is_symlink() or not secrets.is_dir() or secrets.stat().st_mode & 0o077:
            fail("BACKUP_FILES_INVALID")
        entries = sorted(secrets.iterdir())
        if not 7 <= len(entries) <= 64:
            fail("BACKUP_FILES_INVALID")
        required = {"db_password", "admin_database_url", "migration_database_url",
                    "app_database_url", "lifecycle_database_url", "vendor_secret",
                    "token_encryption_key"}
        if not required.issubset({entry.name for entry in entries}):
            fail("BACKUP_FILES_INVALID")
        for entry in entries:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", entry.name):
                fail("BACKUP_FILES_INVALID")
            result["deploy/secrets/" + entry.name] = read_file(entry, private=True)
    except OpsError:
        raise
    except Exception:
        fail("BACKUP_FILES_INVALID")
    result["RECOVERY.json"] = (json.dumps({
        "format": "bonuspro-recovery-v1", "image": image, "schema": SCHEMA,
        "contains_application_secrets": True,
        "requires_separately_preserved_recovery_key": True,
        "independent_machine_restore_verified": False,
    }, sort_keys=True) + "\n").encode()
    if sum(map(len, result.values())) > MAX_CONFIG:
        fail("BACKUP_FILES_INVALID")
    return result

def create_recovery(path, files, image, key, commands):
    with tempfile.TemporaryFile() as plaintext:
        with tarfile.open(fileobj=plaintext, mode="w") as archive:
            for name, value in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(value)
                entry.mode = 0o600
                entry.uid = entry.gid = 0
                entry.mtime = 0
                archive.addfile(entry, io.BytesIO(value))
        plaintext.seek(0)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as encrypted:
                commands.call(crypto_command(image, key, "encrypt"), "BACKUP_ENCRYPT_FAILED",
                              stdin=plaintext, stdout=encrypted)
                encrypted.flush()
                os.fsync(encrypted.fileno())
        except OpsError:
            raise
        except Exception:
            fail("BACKUP_ENCRYPT_FAILED")
    digest, size = file_digest(path)
    if size < 68:
        fail("BACKUP_ENCRYPT_FAILED")
    return digest, size

def verify_recovery(path, files, image, key, commands):
    try:
        with tempfile.TemporaryFile() as plaintext, open(path, "rb") as encrypted:
            commands.call(crypto_command(image, key, "decrypt"), "RECOVERY_BUNDLE_INVALID",
                          stdin=encrypted, stdout=plaintext)
            if plaintext.tell() > MAX_CONFIG + 1024 * 1024:
                fail("RECOVERY_BUNDLE_INVALID")
            plaintext.seek(0)
            with tarfile.open(fileobj=plaintext, mode="r:") as archive:
                entries = archive.getmembers()
                if len(entries) != len(files) or {entry.name for entry in entries} != set(files):
                    fail("RECOVERY_BUNDLE_INVALID")
                for entry in entries:
                    expected = files[entry.name]
                    if not entry.isfile() or entry.mode != 0o600 or entry.size != len(expected):
                        fail("RECOVERY_BUNDLE_INVALID")
                    source = archive.extractfile(entry)
                    if source is None or source.read(len(expected) + 1) != expected:
                        fail("RECOVERY_BUNDLE_INVALID")
    except OpsError:
        raise
    except Exception:
        fail("RECOVERY_BUNDLE_INVALID")

def rclone_copy(commands, backup, source, target, *, download=False):
    commands.call([
        RCLONE, "--config", backup["rclone_config"], "--quiet",
        "--retries", "2", "--low-level-retries", "2", "--contimeout", "10s",
        "--timeout", "60s", "--stats", "0", "--s3-no-check-bucket",
        "copyto", source, target, "--immutable",
    ], "BACKUP_DOWNLOAD_FAILED" if download else "BACKUP_UPLOAD_FAILED", timeout=600)

def downloaded(commands, backup, remote, destination, expected_hash, expected_bytes):
    if destination.exists() or destination.is_symlink():
        fail("BACKUP_FILES_INVALID")
    rclone_copy(commands, backup, remote, destination, download=True)
    digest, size = file_digest(destination)
    if digest != expected_hash or size != expected_bytes:
        fail("BACKUP_HASH_MISMATCH")
    return destination

def run_backup(config, *, run=subprocess.run, state_dir=None, now=None):
    backup, image = validate(config)
    state_dir = STATE_DIR if state_dir is None else Path(state_dir)
    commands = Commands(run=run)
    completed_clock = now or (lambda: datetime.now(timezone.utc))
    with locked(state_dir / "backup.lock"), locked(RELEASE_ROOT / ".marketplace-deployment.lock", shared=True):
        try:
            # Re-read the image only after serializing against release changes.
            backup, image = validate(config)
            for parent in (BACKUP_DIR, *BACKUP_DIR.parents):
                if parent.is_symlink():
                    fail("BACKUP_FILES_INVALID")
            BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = BACKUP_DIR.stat()
            if BACKUP_DIR.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                fail("BACKUP_FILES_INVALID")
            payload = commands.call(
                [sys.executable, "-I", HELPER_DIR / "backup_vps.py", "--image", image],
                "BACKUP_CREATE_FAILED", timeout=1800)
            try:
                created = json.loads(payload)
                database = Path(created["file"])
                if (created.get("status") != "encrypted_backup_created"
                        or database.parent != BACKUP_DIR
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.pgdump\.bpbk", database.name)):
                    fail("BACKUP_CREATE_FAILED")
                db_hash, db_bytes = file_digest(database)
                if created.get("sha256") != db_hash or db_bytes < 68:
                    fail("BACKUP_CREATE_FAILED")
            except OpsError:
                raise
            except Exception:
                fail("BACKUP_CREATE_FAILED")
            run_id = completed_clock().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
            remote = backup["remote"] + "/" + run_id
            files = recovery_files(image)
            recovery = BACKUP_DIR / (run_id + ".recovery.tar.bpbk")
            recovery_hash, recovery_bytes = create_recovery(
                recovery, files, image, backup["recovery_key_file"], commands)
            db_checksum = BACKUP_DIR / (run_id + ".database.sha256")
            recovery_checksum = BACKUP_DIR / (run_id + ".recovery.sha256")
            db_sum = (db_hash + "  database.pgdump.bpbk\n").encode()
            recovery_sum = (recovery_hash + "  recovery.tar.bpbk\n").encode()
            write_new(db_checksum, db_sum)
            write_new(recovery_checksum, recovery_sum)
            objects = [
                (database, "database.pgdump.bpbk", db_hash, db_bytes),
                (db_checksum, "database.sha256", hashlib.sha256(db_sum).hexdigest(), len(db_sum)),
                (recovery, "recovery.tar.bpbk", recovery_hash, recovery_bytes),
                (recovery_checksum, "recovery.sha256",
                 hashlib.sha256(recovery_sum).hexdigest(), len(recovery_sum)),
            ]
            for local, name, _, _ in objects:
                rclone_copy(commands, backup, local, remote + "/" + name)
            staging = {}
            for _, name, digest, size in objects:
                suffix = ".pgdump.bpbk" if name == "database.pgdump.bpbk" else ".download"
                path = BACKUP_DIR / (run_id + "." + name.replace(".", "-") + suffix)
                staging[name] = downloaded(
                    commands, backup, remote + "/" + name, path, digest, size)
            # The existing verifier expects its checksum next to the ciphertext.
            write_new(staging["database.pgdump.bpbk"].with_suffix(".bpbk.sha256"), db_sum)
            commands.call([
                sys.executable, "-I", HELPER_DIR / "verify_backup_vps.py",
                staging["database.pgdump.bpbk"], "--image", image,
                "--expected-schema", SCHEMA,
            ], "BACKUP_RESTORE_FAILED", timeout=1800)
            verify_recovery(staging["recovery.tar.bpbk"], files, image,
                            backup["recovery_key_file"], commands)
            receipt = {
                "completed_at": completed_clock().isoformat(), "image": image,
                "schema": SCHEMA, "sha256": db_hash, "bytes": db_bytes,
                "offsite_verified": True, "restoration_verified": True,
                "recovery_bundle_verified": True,
                "recovery_sha256": recovery_hash, "recovery_bytes": recovery_bytes,
                "independent_machine_restore_verified": False, "run_id": run_id,
                "database_file": database.name, "recovery_file": recovery.name,
            }
            manifest = BACKUP_DIR / (run_id + ".manifest.json")
            write_new(manifest, (json.dumps(receipt, sort_keys=True) + "\n").encode())
            rclone_copy(commands, backup, manifest, remote + "/manifest.json")
            try:
                atomic_json(state_dir / "backup-success.json", receipt)
            except Exception:
                fail("BACKUP_RECEIPT_FAILED")
            return receipt
        except OpsError:
            raise
        except Exception:
            fail("BACKUP_FILES_INVALID")

def error_code(error):
    candidate = getattr(error, "code", None) or str(error)
    return candidate if candidate in ERROR_CODES else "BACKUP_CONFIG_INVALID"

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("OFFSITE_BACKUP_FAILED BACKUP_ROOT_REQUIRED")
        return 2
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.check_config:
            validate(config)
            print("OFFSITE_BACKUP_CONFIG_OK")
            return 0
    except Exception as error:
        print("OFFSITE_BACKUP_FAILED " + error_code(error))
        return 2
    try:
        run_backup(config)
        print("OFFSITE_BACKUP_READY")
        return 0
    except Exception as error:
        print("OFFSITE_BACKUP_FAILED " + error_code(error))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
