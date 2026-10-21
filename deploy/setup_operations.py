"""Install root-owned VPS operations tools without changing application finances."""
import argparse
import base64
import configparser
from datetime import datetime, timezone, timedelta
import getpass
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from ops_common import (CONFIG_PATH, STATE_DIR, RELEASE_ROOT, OpsError, atomic_json,
                        ensure_directory, https_url, load_config, locked, secure_read)

INSTALL_DIR = Path("/usr/local/lib/bonuspro-ops")
UNIT_DIR = Path("/etc/systemd/system")
CONFIG_DIR = CONFIG_PATH.parent
PYTHON = "/usr/bin/python3"
SYSTEMCTL = "/usr/bin/systemctl"
GIT = "/usr/bin/git"
OWNER_UID = 0
HELPERS = ("ops_common.py", "backup_vps.py", "verify_backup_vps.py",
           "offsite_backup.py", "monitor_vps.py")
UNITS = ("bonuspro-offsite-backup.service", "bonuspro-offsite-backup.timer",
         "bonuspro-monitor.service", "bonuspro-monitor.timer")
MONITOR_TIMER = "bonuspro-monitor.timer"
BACKUP_TIMER = "bonuspro-offsite-backup.timer"


def execute(args, *, timeout=180):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise OpsError("OPS_COMMAND_FAILED") from None


def checked(args, *, timeout=180):
    result = execute(args, timeout=timeout)
    if result.returncode != 0:
        raise OpsError("OPS_COMMAND_FAILED")
    return result.stdout


def _safe_parents(path):
    for directory in reversed(path.parent.parents):
        info = directory.lstat()
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, OWNER_UID)
                or info.st_mode & 0o022 and not sticky_root):
            raise OpsError("OPS_UNSAFE_DIRECTORY")
    info = path.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, OWNER_UID)
            or info.st_mode & 0o022):
        raise OpsError("OPS_UNSAFE_DIRECTORY")


def write_file(path, content, mode=0o600, *, exclusive=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise OpsError("OPS_UNSAFE_PATH")
    _safe_parents(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise OpsError("OPS_UNSAFE_FILE")
        if exclusive:
            raise OpsError("OPS_FILE_EXISTS")
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), mode)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        raise OpsError("OPS_FILE_EXISTS") from None
    except OSError:
        raise OpsError("OPS_INSTALL_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def source_bytes(source, relative):
    source, relative = Path(source), Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise OpsError("OPS_UNSAFE_SOURCE")
    path = source / relative
    for part in relative.parents:
        directory = source / part
        if directory.is_symlink() or not directory.is_dir():
            raise OpsError("OPS_UNSAFE_SOURCE")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as value:
            info = os.fstat(value.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
                raise OpsError("OPS_UNSAFE_SOURCE")
            body = value.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise OpsError("OPS_UNSAFE_SOURCE")
        return body
    except OSError:
        raise OpsError("OPS_SOURCE_UNAVAILABLE") from None


def source_revision(source, expected=None):
    prefix = [GIT, "-c", "safe.directory=" + str(source), "-C", str(source)]
    revision = checked(prefix + ["rev-parse", "HEAD"]).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or expected is not None and revision != expected:
        raise OpsError("OPS_SOURCE_REVISION_MISMATCH")
    if checked(prefix + ["status", "--porcelain", "--untracked-files=no"]).strip():
        raise OpsError("OPS_SOURCE_DIRTY")
    for relative in [*("scripts/" + name for name in HELPERS),
                     *("deploy/" + name for name in UNITS)]:
        if source_bytes(source, relative) != checked(prefix + ["show", "HEAD:" + relative]):
            raise OpsError("OPS_SOURCE_DIRTY")
    return revision


def install_files(source=SOURCE):
    ensure_directory(INSTALL_DIR)
    for name in HELPERS:
        write_file(INSTALL_DIR / name, source_bytes(source, "scripts/" + name), 0o700)
    for name in UNITS:
        write_file(UNIT_DIR / name, source_bytes(source, "deploy/" + name), 0o644)
    checked([SYSTEMCTL, "daemon-reload"])


def ensure_recovery_key(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        try:
            value = base64.b64decode(secure_read(path).strip(), validate=True)
        except (ValueError, UnicodeError):
            raise OpsError("OPS_INVALID_RECOVERY_KEY") from None
        if len(value) != 32:
            raise OpsError("OPS_INVALID_RECOVERY_KEY")
        return
    write_file(path, base64.b64encode(secrets.token_bytes(32)) + b"\n", exclusive=True)


def _yes(prompt, default=False):
    answer = input(prompt + (" [Y/n]: " if default else " [y/N]: ")).strip().lower()
    if not answer:
        return default
    if answer in ("y", "yes", "да", "д"):
        return True
    if answer in ("n", "no", "нет", "н"):
        return False
    raise OpsError("OPS_INVALID_CONFIRMATION")


def _text(prompt, default=""):
    value = input(prompt + ((" [" + default + "]") if default else "") + ": ").strip()
    return value or default


def _secret_file(name, value, old_path=None):
    body = (value.strip() + "\n").encode("utf-8")
    if old_path:
        previous = Path(old_path)
        if previous.parent != CONFIG_DIR:
            raise OpsError("OPS_SECRET_OUTSIDE_CONFIG")
        if secure_read(previous) == body:
            return str(previous)
    target = CONFIG_DIR / name
    if target.exists() or target.is_symlink():
        target = CONFIG_DIR / (name + "-" + secrets.token_hex(8))
    write_file(target, body, exclusive=True)
    return str(target)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def detect_chat(token, since):
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
        raise OpsError("OPS_INVALID_TELEGRAM_TOKEN")
    try:
        with build_opener(NoRedirect()).open(
                Request("https://api.telegram.org/bot" + token + "/getUpdates?limit=100&timeout=0"),
                timeout=15) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError()
        data = json.loads(body)
        if data.get("ok") is not True or not isinstance(data.get("result"), list):
            raise ValueError()
        chats = set()
        for update in data["result"]:
            message = update.get("message", {}) if isinstance(update, dict) else {}
            chat, sender = message.get("chat", {}), message.get("from", {})
            if (message.get("text") == "/start" and type(message.get("date")) is int
                    and since <= message["date"] <= time.time() + 60
                    and chat.get("type") == "private" and type(chat.get("id")) is int
                    and sender.get("id") == chat["id"] and sender.get("is_bot") is False):
                chats.add(str(chat["id"]))
        if len(chats) != 1:
            raise OpsError("OPS_TELEGRAM_CHAT_NOT_UNAMBIGUOUS")
        return chats.pop()
    except OpsError:
        raise
    except Exception:
        raise OpsError("OPS_TELEGRAM_CHAT_LOOKUP_FAILED") from None


def configure(config_path=CONFIG_PATH):
    if not sys.stdin.isatty():
        raise OpsError("OPS_CONFIGURE_REQUIRES_TTY")
    previous = {}
    if config_path.exists() or config_path.is_symlink():
        try:
            previous = json.loads(secure_read(config_path).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise OpsError("OPS_INVALID_CONFIG") from None
        if not isinstance(previous, dict) or previous.get("version") != 1:
            raise OpsError("OPS_INVALID_CONFIG")
    config = dict(previous)
    config.update(version=1, release_root=str(RELEASE_ROOT))
    config.setdefault("monitor", {
        "public_origin": "https://loyalty.addwise.kz", "expected_active_accounts": 1,
        "max_processing_age_seconds": 900, "max_backup_age_hours": 32})
    old_telegram = previous.get("telegram", {})
    old_token_path = old_telegram.get("token_file")
    token = getpass.getpass("Токен Telegram-бота (скрыто; Enter — оставить существующий): ").strip()
    if not token:
        token_path = Path(old_token_path) if old_token_path else CONFIG_DIR / "telegram_token"
        if not token_path.exists():
            print("OPS_SETUP_PENDING: создайте бота через @BotFather и повторите настройку.", flush=True)
            return None
        token = secure_read(token_path).decode("ascii").strip()
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
        raise OpsError("OPS_INVALID_TELEGRAM_TOKEN")
    token_file = _secret_file("telegram_token", token, old_token_path)
    chat = _text("Chat ID; Enter без значения — найти личный чат после /start",
                 old_telegram.get("chat_id", ""))
    if not chat:
        since = int(time.time())
        input("Откройте своего бота, нажмите Start или отправьте /start, затем Enter здесь: ")
        chat = detect_chat(token, since)
        print("Найден личный чат: " + chat, flush=True)
        if not _yes("Использовать этот чат для уведомлений?"):
            raise OpsError("OPS_TELEGRAM_CHAT_NOT_CONFIRMED")
    if not re.fullmatch(r"-?[0-9]{1,20}", chat) or int(chat) == 0:
        raise OpsError("OPS_INVALID_TELEGRAM_CHAT")
    config["telegram"] = {"token_file": token_file, "chat_id": chat}
    old_backup = previous.get("backup", {})
    backup = dict(old_backup)
    enabled = _yes("Уже есть отдельное S3-хранилище вне этого VPS?", old_backup.get("enabled") is True)
    backup["enabled"] = enabled
    if enabled:
        reuse = bool(old_backup.get("enabled")) and _yes("Оставить прежнее подключение хранилища?", True)
        if not reuse:
            endpoint = https_url(_text("HTTPS endpoint S3"))
            parsed = urlsplit(endpoint)
            if parsed.query or parsed.path not in ("", "/") or parsed.hostname == "loyalty.addwise.kz":
                raise OpsError("OPS_INVALID_STORAGE_CONFIG")
            region = _text("Регион S3", "us-east-1")
            bucket = _text("Имя существующего bucket")
            prefix = _text("Папка внутри bucket", "bonuspro")
            remote = "bonuspro-backup:" + bucket + "/" + prefix
            if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,400}", prefix)
                    or any(part in ("", ".", "..") for part in prefix.split("/"))
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", region)):
                raise OpsError("OPS_INVALID_STORAGE_CONFIG")
            access = getpass.getpass("S3 Access Key ID (скрыто): ").strip()
            secret = getpass.getpass("S3 Secret Access Key (скрыто): ").strip()
            if any(not value or len(value) > 1024 or "\n" in value or "\r" in value
                   for value in (access, secret)):
                raise OpsError("OPS_INVALID_STORAGE_CREDENTIALS")
            parser = configparser.ConfigParser(interpolation=None)
            parser["bonuspro-backup"] = {
                "type": "s3", "provider": "Other", "access_key_id": access,
                "secret_access_key": secret, "endpoint": endpoint, "region": region,
                "acl": "private", "no_check_bucket": "true"}
            body = io.StringIO()
            parser.write(body)
            storage_file = _secret_file("rclone.conf", body.getvalue(), old_backup.get("rclone_config"))
            backup.update(remote=remote, rclone_config=storage_file)
        recovery = CONFIG_DIR / "recovery_key"
        ensure_recovery_key(recovery)
        backup.update(expected_schema="0018", recovery_key_file=str(recovery))
        backup["offsite_location_confirmed"] = _yes(
            "Хранилище находится вне основного VPS и не удалится вместе с ним?",
            old_backup.get("offsite_location_confirmed") is True)
        print("Ключ восстановления: " + str(recovery) + ". Сохраните отдельную защищённую копию вне VPS.", flush=True)
        config["recovery_key_exported"] = _yes(
            "Отдельная копия ключа уже сохранена вне VPS?",
            previous.get("recovery_key_exported") is True)
    else:
        config["recovery_key_exported"] = previous.get("recovery_key_exported", False)
    config["backup"] = backup
    heartbeat_path = (previous.get("heartbeat") or {}).get("url_file")
    heartbeat = getpass.getpass(
        "HTTPS URL независимого heartbeat-монитора (скрыто; Enter — оставить или пропустить): ").strip()
    if heartbeat:
        https_url(heartbeat)
        config["heartbeat"] = {"url_file": _secret_file("heartbeat_url", heartbeat, heartbeat_path)}
    elif heartbeat_path:
        config["heartbeat"] = {"url_file": heartbeat_path}
    else:
        config.pop("heartbeat", None)
    candidate = CONFIG_DIR / (".config-check-" + secrets.token_hex(8) + ".json")
    try:
        atomic_json(candidate, config)
        load_config(candidate)
        atomic_json(config_path, config)
    finally:
        candidate.unlink(missing_ok=True)
    return config


def helper(name, config_path, *extra):
    return [PYTHON, "-E", "-s", str(INSTALL_DIR / name), "--config", str(config_path), *extra]


def monitor_result(config_path, *, notify=False):
    arguments = helper("monitor_vps.py", config_path, *(() if notify else ("--check-only",)))
    result = execute(arguments, timeout=240)
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise OpsError("OPS_MONITOR_PROTOCOL") from None
    if (not isinstance(report, dict) or type(report.get("ready")) is not bool
            or type(report.get("technical_ready")) is not bool
            or not isinstance(report.get("checks"), list)
            or result.returncode not in (0, 1)):
        raise OpsError("OPS_MONITOR_PROTOCOL")
    if not notify and (result.returncode == 0) != report["ready"]:
        raise OpsError("OPS_MONITOR_PROTOCOL")
    return report


def verified_receipt(config, since):
    try:
        receipt = json.loads(secure_read(STATE_DIR / "backup-success.json").decode("utf-8"))
        completed = datetime.fromisoformat(receipt["completed_at"])
        if completed.tzinfo is None:
            raise ValueError()
        now = datetime.now(timezone.utc)
        if (completed < since or completed > now + timedelta(minutes=5)
                or not all(receipt.get(name) is True for name in
                           ("offsite_verified", "restoration_verified", "recovery_bundle_verified"))
                or receipt.get("schema") != config["backup"]["expected_schema"]
                or not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}", receipt.get("image", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", ""))
                or type(receipt.get("bytes")) is not int or receipt["bytes"] <= 0):
            raise ValueError()
        return receipt
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise OpsError("OPS_BACKUP_NOT_VERIFIED") from None


def _enable_monitor():
    checked([SYSTEMCTL, "enable", "--now", MONITOR_TIMER])


def enable_operations(config_path=CONFIG_PATH):
    config = load_config(config_path)
    # An explicit test is authorised by --enable; token and message never appear in argv.
    checked(helper("monitor_vps.py", config_path, "--test-notification"), timeout=45)
    backup_ready, failure, report = False, None, None
    try:
        if config["backup"]["enabled"]:
            if shutil.which("rclone") != "/usr/bin/rclone":
                raise OpsError("OPS_RCLONE_NOT_INSTALLED")
            if config["backup"].get("offsite_location_confirmed") is not True:
                raise OpsError("OPS_OFFSITE_LOCATION_NOT_CONFIRMED")
            # A previous local-only timer must not race the offsite checkpoint.
            active = execute([SYSTEMCTL, "is-active", "--quiet", "bonuspro-backup.service"])
            if active.returncode == 0:
                raise OpsError("OPS_LEGACY_BACKUP_RUNNING")
            legacy = execute([SYSTEMCTL, "list-unit-files", "--no-legend", "bonuspro-backup.timer"])
            if legacy.returncode == 0 and b"bonuspro-backup.timer" in legacy.stdout:
                checked([SYSTEMCTL, "disable", "--now", "bonuspro-backup.timer"])
            since = datetime.now(timezone.utc)
            print("OPS_VERIFYING_OFFSITE_BACKUP: проверяем загрузку, скачивание и восстановление.", flush=True)
            checked(helper("offsite_backup.py", config_path), timeout=7200)
            verified_receipt(config, since)
            checked([SYSTEMCTL, "enable", "--now", BACKUP_TIMER])
            backup_ready = True
        report = monitor_result(config_path)
        if report["ready"]:
            delivered = monitor_result(config_path, notify=True)
            if delivered.get("heartbeat_delivered") is not True or delivered.get("ready") is not True:
                failure = "OPS_HEARTBEAT_NOT_CONFIRMED"
    except OpsError as error:
        failure = error.code
    finally:
        # Observability remains useful while business attention or setup is unresolved.
        _enable_monitor()
    ready = bool(backup_ready and report and report["ready"] and failure is None)
    return {
        "status": "OPS_READY" if ready else "OPS_SETUP_PENDING",
        "monitor_enabled": True, "offsite_backup_enabled": backup_ready,
        "read_only_business_checks": True, "business_acceptance_verified": False,
        "independent_restore_verified": False,
        "reason": failure or (None if ready else "OPS_READINESS_INCOMPLETE")}


def configure_and_enable(config_path=CONFIG_PATH):
    previous = None
    if config_path.exists() or config_path.is_symlink():
        try:
            previous = load_config(config_path)
        except OpsError:
            # An invalid old configuration is never promoted as a recovery target.
            previous = None
    try:
        if configure(config_path) is None:
            return None
        result = enable_operations(config_path)
    except (Exception, KeyboardInterrupt):
        if previous is not None:
            atomic_json(config_path, previous)
        raise
    # Business attention or intentional partial setup keeps the owner's valid change.
    # Delivery/transfer/protocol failures restore the previous usable credential references.
    if (previous is not None and result["status"] != "OPS_READY"
            and result.get("reason") not in (None, "OPS_READINESS_INCOMPLETE")):
        atomic_json(config_path, previous)
        result["configuration_restored"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install code and units (also the default action)")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--source-revision")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run setup_operations.py with sudo on the VPS")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    try:
        ensure_directory(CONFIG_DIR)
        ensure_directory(STATE_DIR)
        with locked(STATE_DIR / "setup.lock"):
            revision = source_revision(SOURCE, args.source_revision)
            install_files(SOURCE)
            if args.configure and args.enable:
                result = configure_and_enable()
                if result is None:
                    return 2
            else:
                if args.configure and configure() is None:
                    return 2
                if args.enable:
                    result = enable_operations()
                else:
                    result = {"status": "OPS_INSTALLED_SETUP_PENDING", "monitor_enabled": False}
            result["source_revision"] = revision
            print(json.dumps(result, sort_keys=True), flush=True)
            return 0 if result["status"] == "OPS_READY" else 2
    except OpsError as error:
        print(json.dumps({"status": "OPS_SETUP_PENDING", "code": error.code}), flush=True)
        return 2
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "OPS_SETUP_PENDING", "code": "OPS_SETUP_FAILED"}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
