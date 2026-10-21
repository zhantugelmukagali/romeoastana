"""Private, root-owned configuration and state for the VPS operations tools."""
import base64
from contextlib import contextmanager
import configparser
import fcntl
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

CONFIG_PATH = Path("/etc/bonuspro-ops/config.json")
STATE_DIR = Path("/var/lib/bonuspro-ops")
RELEASE_ROOT = Path("/home/ubuntu/bonuspro-release")
OWNER_UID = 0


class OpsError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", code) else "OPS_FAILED"
        super().__init__(self.code)


def _absolute(path):
    result = Path(path)
    if not result.is_absolute() or ".." in result.parts:
        raise OpsError("OPS_UNSAFE_PATH")
    return result


def _parents(path, *, release_lock=False):
    """Every component is inspected without resolving symlinks."""
    operator_uid = OWNER_UID
    if release_lock:
        try:
            operator_uid = pwd.getpwnam("ubuntu").pw_uid
        except KeyError:
            operator_uid = OWNER_UID
    for directory in (*reversed(path.parent.parents), path.parent):
        allowed = (0, OWNER_UID, operator_uid) if release_lock and directory in (RELEASE_ROOT, RELEASE_ROOT.parent) else (0, OWNER_UID)
        _directory(directory, allowed_uids=allowed)


def _directory(path, *, allowed_uids=None):
    try:
        info = path.lstat()
    except OSError:
        raise OpsError("OPS_DIRECTORY_UNAVAILABLE") from None
    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in ((0, OWNER_UID) if allowed_uids is None else allowed_uids)
            or (info.st_mode & 0o022 and not sticky_root)):
        raise OpsError("OPS_UNSAFE_DIRECTORY")


def _file_info(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise OpsError("OPS_UNSAFE_FILE")


def secure_read(path, max_bytes=65536):
    path = _absolute(path)
    if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
        raise OpsError("OPS_INVALID_READ_LIMIT")
    _parents(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise OpsError("OPS_FILE_UNAVAILABLE") from None
    try:
        _file_info(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise OpsError("OPS_FILE_TOO_LARGE")
        return content
    finally:
        os.close(descriptor)


def ensure_directory(path):
    path = _absolute(path)
    # Ancestors must already exist and must not permit a substituted path.
    _parents(path)
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise OpsError("OPS_DIRECTORY_UNAVAILABLE") from None
    _directory(path)
    info = path.lstat()
    if info.st_uid != OWNER_UID or info.st_mode & 0o077:
        raise OpsError("OPS_UNSAFE_DIRECTORY")
    return path


def atomic_json(path, value):
    path = _absolute(path)
    _parents(path)
    if path.exists() or path.is_symlink():
        _file_info(path.lstat())
    try:
        body = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise OpsError("OPS_INVALID_STATE") from None
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise OpsError("OPS_STATE_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked(path, *, shared=False):
    path = _absolute(path)
    _parents(path, release_lock=path == RELEASE_ROOT / ".marketplace-deployment.lock")
    try:
        if shared:
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        else:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise OpsError("OPS_LOCK_UNAVAILABLE") from None
    acquired = False
    try:
        _file_info(os.fstat(descriptor))
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            raise OpsError("OPS_BUSY") from None
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def https_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() for c in value):
        raise OpsError("OPS_INVALID_HTTPS_URL")
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        raise OpsError("OPS_INVALID_HTTPS_URL") from None
    if (parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in (None, 443)
            or hostname in ("localhost", "localhost.localdomain")
            or hostname.endswith((".localhost", ".local", ".internal"))):
        raise OpsError("OPS_INVALID_HTTPS_URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise OpsError("OPS_INVALID_HTTPS_URL")
    return value


def _private_child(value, directory):
    path = _absolute(value)
    if path.parent != directory:
        raise OpsError("OPS_SECRET_OUTSIDE_CONFIG")
    return path


def _integer(value, low, high, code):
    if type(value) is not int or not low <= value <= high:
        raise OpsError(code)
    return value


def _rclone(path):
    raw = secure_read(path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw.decode("utf-8"))
    except (UnicodeError, configparser.Error):
        raise OpsError("OPS_INVALID_STORAGE_CONFIG") from None
    allowed = {"type", "provider", "access_key_id", "secret_access_key", "region",
               "endpoint", "acl", "no_check_bucket"}
    if parser.sections() != ["bonuspro-backup"] or parser.defaults():
        raise OpsError("OPS_INVALID_STORAGE_CONFIG")
    values = dict(parser["bonuspro-backup"])
    if (set(values) - allowed or values.get("type") != "s3"
            or values.get("provider") != "Other" or values.get("acl", "private") != "private"
            or values.get("no_check_bucket") != "true"
            or any(not values.get(key) for key in ("access_key_id", "secret_access_key", "region"))):
        raise OpsError("OPS_INVALID_STORAGE_CONFIG")
    endpoint = https_url(values.get("endpoint"))
    parsed = urlsplit(endpoint)
    if parsed.query or parsed.path not in ("", "/") or parsed.hostname == "loyalty.addwise.kz":
        raise OpsError("OPS_INVALID_STORAGE_CONFIG")


def load_config(path=CONFIG_PATH):
    path = _absolute(path)
    try:
        value = json.loads(secure_read(path).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise OpsError("OPS_INVALID_CONFIG") from None
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise OpsError("OPS_INVALID_CONFIG")
    if value.get("release_root") != str(RELEASE_ROOT):
        raise OpsError("OPS_INVALID_RELEASE_ROOT")
    directory = path.parent
    monitor, telegram, backup = (value.get(name) for name in ("monitor", "telegram", "backup"))
    if not all(isinstance(section, dict) for section in (monitor, telegram, backup)):
        raise OpsError("OPS_INVALID_CONFIG")
    origin = https_url(monitor.get("public_origin"))
    parsed = urlsplit(origin)
    if parsed.query or parsed.path not in ("", "/"):
        raise OpsError("OPS_INVALID_PUBLIC_ORIGIN")
    _integer(monitor.get("expected_active_accounts"), 0, 10000, "OPS_INVALID_ACCOUNT_COUNT")
    _integer(monitor.get("max_processing_age_seconds"), 60, 86400, "OPS_INVALID_PROCESSING_LIMIT")
    _integer(monitor.get("max_backup_age_hours"), 1, 168, "OPS_INVALID_BACKUP_LIMIT")
    if not isinstance(telegram.get("chat_id"), str) or not re.fullmatch(r"-?[0-9]{1,20}", telegram["chat_id"]):
        raise OpsError("OPS_INVALID_TELEGRAM_CHAT")
    token_path = _private_child(telegram.get("token_file", ""), directory)
    try:
        token = secure_read(token_path).decode("ascii").strip()
    except UnicodeError:
        raise OpsError("OPS_INVALID_TELEGRAM_TOKEN") from None
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
        raise OpsError("OPS_INVALID_TELEGRAM_TOKEN")
    if type(value.get("recovery_key_exported", False)) is not bool:
        raise OpsError("OPS_INVALID_RECOVERY_CONFIRMATION")
    if type(backup.get("enabled")) is not bool:
        raise OpsError("OPS_INVALID_BACKUP_CONFIG")
    if backup["enabled"]:
        remote = backup.get("remote")
        if (not isinstance(remote, str) or not re.fullmatch(r"bonuspro-backup:[a-z0-9][a-z0-9./_-]{2,500}", remote)
                or any(part in ("", ".", "..") for part in remote.split(":", 1)[1].split("/"))
                or backup.get("expected_schema") != "0018"
                or type(backup.get("offsite_location_confirmed")) is not bool):
            raise OpsError("OPS_INVALID_BACKUP_CONFIG")
        storage_path = _private_child(backup.get("rclone_config", ""), directory)
        _rclone(storage_path)
        recovery_path = _private_child(backup.get("recovery_key_file", ""), directory)
        try:
            key = base64.b64decode(secure_read(recovery_path).strip(), validate=True)
        except (ValueError, UnicodeError):
            raise OpsError("OPS_INVALID_RECOVERY_KEY") from None
        if len(key) != 32:
            raise OpsError("OPS_INVALID_RECOVERY_KEY")
    heartbeat = value.get("heartbeat")
    if heartbeat is not None:
        if not isinstance(heartbeat, dict):
            raise OpsError("OPS_INVALID_HEARTBEAT")
        heartbeat_path = _private_child(heartbeat.get("url_file", ""), directory)
        try:
            https_url(secure_read(heartbeat_path).decode("utf-8").strip())
        except UnicodeError:
            raise OpsError("OPS_INVALID_HEARTBEAT") from None
    return value
