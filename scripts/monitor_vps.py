"""Root-run, read-only probes with durable, redacted Telegram incident delivery.

--check-only never writes incident state or sends Telegram/heartbeat requests.
The monitor never calls a financial mutation or runs a worker cycle.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ops_common import CONFIG_PATH, STATE_DIR, OpsError, load_config, secure_read, atomic_json, locked, https_url

RELEASE_ROOT = Path("/home/ubuntu/bonuspro-release")
STATE_PATH = STATE_DIR / "monitor-state.json"
FAILURES_TO_OPEN = 3
SUCCESSES_TO_RECOVER = 2
REMINDER_SECONDS = 6 * 3600
DEPLOYMENT_GRACE_SECONDS = 30 * 60
SEVERITIES = {
    "app_container": "critical", "db_container": "critical",
    "pool_container": "critical", "image_mismatch": "critical",
    "public_ready": "critical", "processing_probe": "critical",
    "processing_accounts": "critical", "processing_stale": "critical",
    "processing_provider": "critical", "processing_setup": "warning",
    "processing_attention": "warning", "dead_jobs": "warning",
    "disk_space": "critical", "disk_inodes": "critical",
    "disk_space_warning": "warning", "disk_inodes_warning": "warning",
    "backup_local": "critical", "backup_offsite": "critical",
    "backup_unit": "critical", "backup_timer": "warning",
    "backup_restore": "warning", "backup_recovery_bundle": "warning",
    "backup_not_configured": "warning", "recovery_key_not_exported": "warning",
    "external_monitor_missing": "warning", "external_heartbeat_delivery": "warning",
    "notification_configuration": "critical",
    "deployment_lock_invalid": "critical", "deployment_grace_expired": "critical",
}
ALERT_LABELS = {
    "app_container": "Приложение не отвечает",
    "db_container": "База данных недоступна",
    "pool_container": "Обработчик заказов не отвечает",
    "image_mismatch": "Версии приложения и обработчика не совпадают",
    "public_ready": "Публичный сайт недоступен",
    "processing_probe": "Не удалось проверить обработку заказов",
    "processing_accounts": "Нет ожидаемых активных магазинов",
    "processing_stale": "Обработка заказов задерживается",
    "processing_provider": "Не удалось завершить проверку МойСклад",
    "processing_setup": "Настройка обработки заказов не завершена",
    "processing_attention": "Есть операции для проверки администратором",
    "dead_jobs": "Есть задания с исчерпанными попытками",
    "disk_space": "Критически мало места на диске",
    "disk_inodes": "Критически мало свободных записей файлов",
    "disk_space_warning": "Заканчивается место на диске",
    "disk_inodes_warning": "Заканчиваются свободные записи файлов",
    "backup_local": "Нет свежей локальной резервной копии",
    "backup_offsite": "Нет свежей внешней резервной копии",
    "backup_unit": "Ошибка резервного копирования",
    "backup_timer": "Расписание резервного копирования не работает",
    "backup_restore": "Восстановление резервной копии не подтверждено",
    "backup_recovery_bundle": "Копия настроек и ключей не подтверждена",
    "backup_not_configured": "Внешнее резервное копирование не настроено",
    "recovery_key_not_exported": "Независимая копия ключа не подтверждена",
    "external_monitor_missing": "Внешний контроль сервера не настроен",
    "external_heartbeat_delivery": "Внешний контроль не получил сигнал",
    "notification_configuration": "Канал оповещений настроен неверно",
    "deployment_lock_invalid": "Не удалось проверить режим обновления",
    "deployment_grace_expired": "Обновление длится больше 30 минут",
}
DEPLOYMENT_CODES = frozenset({
    "app_container", "db_container", "pool_container", "image_mismatch",
    "public_ready", "processing_probe", "processing_accounts", "processing_stale",
    "processing_provider", "processing_setup", "processing_attention", "dead_jobs",
})
SETUP_CODES = frozenset({
    "backup_not_configured", "recovery_key_not_exported", "external_monitor_missing",
})
CONTAINERS = {
    "app_container": "bonuspro-release-app-1",
    "db_container": "bonuspro-release-db-1",
    "pool_container": "bonuspro-release-worker-pool-1",
}

# Executed inside the existing app container with its limited runtime role.
# Raw tenant/order/customer data never crosses the Docker command boundary.
PROCESSING_PROBE = """
import json,sys
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
import psycopg
from bonuspro.settings import Settings
from bonuspro.adapters.postgres.ledger import Ledger
from bonuspro.adapters.postgres.readiness import Readiness
ledger=Ledger(Settings.from_env().database_url)
limit=int(sys.argv[1])
with closing(psycopg.connect(ledger.dsn,connect_timeout=3)) as conn:
    privileged=conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    if privileged:raise RuntimeError("limited_role_required")
    accounts=conn.execute("SELECT tenant_id FROM bonuspro.worker_accounts ORDER BY tenant_id LIMIT 10001").fetchall()
if len(accounts)>10000:raise RuntimeError("account_limit")
result=dict(active_accounts=len(accounts),stale_accounts=0,provider_accounts=0,
            setup_accounts=0,attention_accounts=0,dead_jobs=0)
for account in accounts:
    tenant=str(account[0])
    report=Readiness(ledger).report(tenant)
    heartbeat=report["last_processing_at"]
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
    lag=report["scan_lag_seconds"]
    if age is None or not 0<=age<=limit or lag is None or not 0<=lag<=limit:
        result["stale_accounts"]+=1
    if not all(x["complete"] for x in report["checks"] if x["id"]!="processing"):
        result["setup_accounts"]+=1
    if any(report["attention"].values()):result["attention_accounts"]+=1
    with ledger.transaction(tenant) as conn:
        error=conn.execute("SELECT last_error FROM bonuspro.sync_state WHERE tenant_id=%s",(tenant,)).fetchone()
        if error and error["last_error"]:result["provider_accounts"]+=1
        count=conn.execute("SELECT count(*) AS n FROM (SELECT 1 FROM bonuspro.jobs WHERE tenant_id=%s AND state='dead' LIMIT 1001) q",(tenant,)).fetchone()["n"]
        result["dead_jobs"]=min(1001,result["dead_jobs"]+count)
print(json.dumps(result))
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def check(code, ok):
    if code not in SEVERITIES or not (type(ok) is bool or ok is None):
        raise OpsError("monitor_probe_invalid")
    return {"code": code, "ok": ok, "severity": SEVERITIES[code]}


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise OpsError("monitor_configuration_invalid")
    return value


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError()
    return parsed.timestamp()


def command(arguments, timeout=8, max_bytes=65536):
    try:
        completed = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=timeout, check=False)
        if completed.returncode or len(completed.stdout) > max_bytes:
            raise ValueError()
        return completed.stdout
    except Exception:
        raise OpsError("monitor_command_failed") from None


def public_ready(origin, opener=None):
    try:
        parsed = urlsplit(origin)
        if any(character.isspace() for character in origin):
            raise ValueError()
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError()
        request = Request(origin.rstrip("/") + "/health/ready", headers={"Accept": "application/json"})
        with (opener or build_opener(NoRedirect())).open(request, timeout=5) as response:
            body = response.read(4097)
            return response.status == 200 and len(body) <= 4096 and json.loads(body) == {"status": "ready"}
    except Exception:
        return False


def docker_checks(run=command):
    values = []
    images = {}
    template = "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}|{{.Config.Image}}"
    for code, name in CONTAINERS.items():
        try:
            parts = run(["docker", "inspect", "--format", template, name], timeout=5).decode("utf-8").strip().split("|")
            good = len(parts) == 3 and parts[0] == "true" and parts[1] == "healthy"
            if good:
                images[code] = parts[2]
            values.append(check(code, good))
        except Exception:
            values.append(check(code, False))
    values.append(check("image_mismatch", bool(re.fullmatch(r"bonuspro:[0-9a-f]{12,40}",
                                                          images.get("app_container", ""))) and
                        images.get("app_container") == images.get("pool_container")))
    return values


def processing_checks(monitor, run=command):
    expected = integer(monitor.get("expected_active_accounts", 1), 0, 10000)
    maximum_age = integer(monitor.get("max_processing_age_seconds", 900), 60, 86400)
    try:
        raw = run(["docker", "exec", "-e",
                   "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000",
                   CONTAINERS["app_container"], "python", "-c", PROCESSING_PROBE, str(maximum_age)],
                  timeout=30)
        result = json.loads(raw)
        fields = ("active_accounts", "stale_accounts", "provider_accounts", "setup_accounts",
                  "attention_accounts", "dead_jobs")
        if set(result) != set(fields):
            raise ValueError()
        for field in fields:
            integer(result[field], 0, 10001)
        if any(result[field] > result["active_accounts"] for field in
               ("stale_accounts", "provider_accounts", "setup_accounts", "attention_accounts")):
            raise ValueError()
        return [
            check("processing_probe", True),
            check("processing_accounts", result["active_accounts"] >= expected),
            check("processing_stale", result["stale_accounts"] == 0),
            check("processing_provider", result["provider_accounts"] == 0),
            check("processing_setup", result["setup_accounts"] == 0),
            check("processing_attention", result["attention_accounts"] == 0),
            check("dead_jobs", result["dead_jobs"] == 0),
        ]
    except Exception:
        # Do not invent recovered account checks when the query itself failed.
        return [check("processing_probe", False)]


def disk_checks(root=RELEASE_ROOT, statvfs=os.statvfs):
    try:
        free_bytes = []
        free_ratios = []
        inode_ratios = []
        for candidate in (root, root.parent / "bonuspro-backups", Path("/var/lib/docker"), STATE_DIR):
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            info = statvfs(candidate)
            if info.f_blocks <= 0 or info.f_files <= 0:
                raise ValueError()
            free_bytes.append(info.f_bavail * info.f_frsize)
            free_ratios.append(info.f_bavail / info.f_blocks)
            inode_ratios.append(info.f_favail / info.f_files)
        return [
            check("disk_space", min(free_bytes) >= 2 * 1024**3 and min(free_ratios) >= 0.10),
            check("disk_inodes", min(inode_ratios) >= 0.10),
            check("disk_space_warning", min(free_ratios) >= 0.20),
            check("disk_inodes_warning", min(inode_ratios) >= 0.20),
        ]
    except Exception:
        return [check("disk_space", False), check("disk_inodes", False)]


def systemd_properties(raw, expected):
    values = {}
    for line in raw.decode("utf-8").splitlines():
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError()
        values[key] = value
    if set(values) != set(expected):
        raise ValueError()
    return values


def backup_unit_checks(config, run=command):
    if config.get("backup", {}).get("enabled", False) is not True:
        return []
    service = {}
    try:
        service = systemd_properties(run([
            "systemctl", "show", "bonuspro-offsite-backup.service",
            "--property=LoadState,ActiveState,Result,ExecMainStatus"], timeout=5),
            ("LoadState", "ActiveState", "Result", "ExecMainStatus"))
        healthy = (service["LoadState"] == "loaded"
                   and service["ActiveState"] in ("active", "activating", "inactive")
                   and service["Result"] == "success" and service["ExecMainStatus"] == "0")
    except Exception:
        healthy = False
    values = [check("backup_unit", healthy)]
    try:
        enabled = run(["systemctl", "is-enabled", "bonuspro-offsite-backup.timer"], timeout=5).strip() == b"enabled"
        timer = systemd_properties(run([
            "systemctl", "show", "bonuspro-offsite-backup.timer",
            "--property=LoadState,ActiveState,SubState"], timeout=5),
            ("LoadState", "ActiveState", "SubState"))
        scheduled = (enabled and timer["LoadState"] == "loaded" and timer["ActiveState"] == "active"
                     and (timer["SubState"] == "waiting" or
                          (timer["SubState"] == "running" and
                           service.get("ActiveState") in ("active", "activating"))))
    except Exception:
        scheduled = False
    return values + [check("backup_timer", scheduled)]


def backup_artifacts(receipt):
    """Check retained ciphertext metadata; the backup job owns hash/restore proof."""
    directory = Path("/home/ubuntu/bonuspro-backups/encrypted")
    descriptor = None
    try:
        names = [receipt["database_file"], receipt["recovery_file"]]
        if (len(set(names)) != 2 or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,180}", name)
                or not name.endswith(".bpbk") for name in names)):
            return False
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if info.st_uid != 0 or info.st_mode & 0o077:
            return False
        for index, name in enumerate(names):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                            dir_fd=descriptor)
            try:
                info = os.fstat(child)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                        or info.st_nlink != 1 or info.st_size < 68
                        or (index == 0 and info.st_size != receipt["bytes"])):
                    return False
            finally:
                os.close(child)
        return True
    except Exception:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def backup_checks(config, now, reader=secure_read, artifacts=backup_artifacts):
    if config.get("backup", {}).get("enabled", False) is not True:
        return [check("backup_not_configured", False)]
    monitor = config["monitor"]
    max_hours = monitor.get("max_backup_age_hours", 32)
    if type(max_hours) not in (int, float) or not 1 <= max_hours <= 168:
        raise OpsError("monitor_configuration_invalid")
    values = [check("backup_not_configured", True),
              check("recovery_key_not_exported", config.get("recovery_key_exported") is True)]
    try:
        receipt = json.loads(reader(STATE_DIR / "backup-success.json"))
        age = now - timestamp(receipt["completed_at"])
        valid = (0 <= age <= max_hours * 3600
                 and re.fullmatch(r"bonuspro:[0-9a-f]{12,40}", receipt["image"]) is not None
                 and receipt["schema"] == "0018"
                 and re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is not None
                 and type(receipt["bytes"]) is int and receipt["bytes"] >= 68
                 and type(receipt["offsite_verified"]) is bool
                 and type(receipt["restoration_verified"]) is bool
                 and type(receipt["recovery_bundle_verified"]) is bool
                 and artifacts(receipt))
        values.extend([
            check("backup_local", valid),
            check("backup_offsite", valid and receipt["offsite_verified"] and
                  config["backup"].get("offsite_location_confirmed") is True),
            check("backup_restore", valid and receipt["restoration_verified"]),
            check("backup_recovery_bundle", valid and receipt["recovery_bundle_verified"]),
        ])
    except Exception:
        values.extend(check(code, False) for code in
                      ("backup_local", "backup_offsite", "backup_restore", "backup_recovery_bundle"))
    return values


def telegram_credentials(config, reader=secure_read):
    try:
        token = reader(Path(config["telegram"]["token_file"]), max_bytes=256).decode("ascii").strip()
        chat_id = str(config["telegram"]["chat_id"])
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,200}", token):
            raise ValueError()
        if not re.fullmatch(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}", chat_id):
            raise ValueError()
        return token, chat_id
    except Exception:
        raise OpsError("notification_configuration_invalid") from None


def telegram_send(config, text, opener=None, reader=secure_read):
    try:
        token, chat_id = telegram_credentials(config, reader)
        body = json.dumps({"chat_id": chat_id, "text": text,
                           "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
        request = Request("https://api.telegram.org/bot" + token + "/sendMessage", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
            response_body = response.read(65537)
            if not 200 <= response.status < 300 or len(response_body) > 65536:
                raise ValueError()
            if json.loads(response_body).get("ok") is not True:
                raise ValueError()
        return True
    except Exception:
        # In particular, never print urllib errors: their URL contains the bot token.
        return False


def heartbeat_url(config, reader=secure_read):
    try:
        return https_url(reader(Path(config["heartbeat"]["url_file"]), max_bytes=4096).decode("ascii").strip())
    except Exception:
        raise OpsError("heartbeat_configuration_invalid") from None


def heartbeat_send(config, opener=None, reader=secure_read):
    try:
        request = Request(heartbeat_url(config, reader), data=b"", method="POST")
        with (opener or build_opener(NoRedirect())).open(request, timeout=5) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def deployment_lock_held(root=RELEASE_ROOT):
    path = root / ".marketplace-deployment.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except Exception:
        raise OpsError("deployment_lock_invalid") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise OpsError("deployment_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def empty_state():
    return {"version": 1, "incidents": {}, "deployment_since": None,
            "delivery_failures": 0, "delivery_retry_at": 0}


def read_state(reader=secure_read):
    try:
        STATE_PATH.lstat()
    except FileNotFoundError:
        return empty_state()
    try:
        state = json.loads(reader(STATE_PATH))
        if state["version"] != 1 or not isinstance(state["incidents"], dict):
            raise ValueError()
        if set(state["incidents"]) - set(SEVERITIES):
            raise ValueError()
        integer(state["delivery_failures"], 0, 100000)
        integer(state["delivery_retry_at"], 0, 2**53)
        if state["deployment_since"] is not None:
            integer(state["deployment_since"], 0, 2**53)
        for item in state["incidents"].values():
            for field in ("failures", "successes"):
                integer(item[field], 0, 100000)
            if type(item["open"]) is not bool or type(item["alerted"]) is not bool:
                raise ValueError()
            for field in ("opened_at", "last_notified_at"):
                if item[field] is not None:
                    integer(item[field], 0, 2**53)
        return state
    except Exception:
        raise OpsError("monitor_state_invalid") from None


def deployment_status(state, held, now):
    if not held:
        state["deployment_since"] = None
        return False, False
    if state["deployment_since"] is None:
        state["deployment_since"] = now
    elapsed = now - state["deployment_since"]
    return 0 <= elapsed < DEPLOYMENT_GRACE_SECONDS, not 0 <= elapsed < DEPLOYMENT_GRACE_SECONDS


def collect_checks(config, state, now, run=command, ready_probe=public_ready,
                   disk_probe=disk_checks, reader=secure_read, lock_probe=deployment_lock_held):
    if Path(config.get("release_root", str(RELEASE_ROOT))) != RELEASE_ROOT:
        raise OpsError("monitor_configuration_invalid")
    monitor = config["monitor"]
    grace = False
    values = []
    try:
        grace, expired = deployment_status(state, lock_probe(), now)
        values.extend([check("deployment_lock_invalid", True), check("deployment_grace_expired", not expired)])
    except Exception:
        values.append(check("deployment_lock_invalid", False))
    if grace:
        values.extend(check(code, None) for code in sorted(DEPLOYMENT_CODES))
    else:
        values.extend(docker_checks(run))
        values.append(check("public_ready", ready_probe(monitor["public_origin"])))
        values.extend(processing_checks(monitor, run))
    values.extend(disk_probe())
    values.extend(backup_checks(config, now, reader))
    values.extend(backup_unit_checks(config, run))
    try:
        heartbeat_url(config, reader)
        values.append(check("external_monitor_missing", True))
    except Exception:
        values.append(check("external_monitor_missing", False))
    try:
        telegram_credentials(config, reader)
        values.append(check("notification_configuration", True))
    except Exception:
        values.append(check("notification_configuration", False))
    return values, grace


def advance(state, checks, now):
    for result in checks:
        if result["ok"] is None:
            continue
        code = result["code"]
        item = state["incidents"].setdefault(code, {
            "failures": 0, "successes": 0, "open": False, "alerted": False,
            "opened_at": None, "last_notified_at": None,
        })
        if result["ok"]:
            item["failures"] = 0
            item["successes"] = min(100000, item["successes"] + 1)
            if item["successes"] >= SUCCESSES_TO_RECOVER:
                item["open"] = False
        else:
            item["successes"] = 0
            item["failures"] = min(100000, item["failures"] + 1)
            if item["failures"] >= FAILURES_TO_OPEN and not item["open"]:
                item["open"] = True
                if not item["alerted"]:
                    item["opened_at"] = now


def pending_events(state, now, suppressed=frozenset()):
    events = []
    for code, item in sorted(state["incidents"].items()):
        if code in suppressed:
            continue
        kind = None
        if item["open"] and not item["alerted"]:
            kind = "alert"
        elif item["open"] and item["alerted"] and code not in SETUP_CODES:
            if now - (item["last_notified_at"] or 0) >= REMINDER_SECONDS:
                kind = "reminder"
        elif (not item["open"] and item["alerted"]
              and item["successes"] >= SUCCESSES_TO_RECOVER):
            kind = "recovery"
        if kind:
            identity = hashlib.sha256((code + ":" + str(item["opened_at"]) + ":" + kind).encode()).hexdigest()[:12]
            events.append({"code": code, "kind": kind, "event": identity})
    return events


def notification_text(events):
    # Codes are a closed vocabulary; no arbitrary provider/configuration strings.
    lines = ["БонусПро: эксплуатация"]
    names = {"alert": "Внимание", "reminder": "Проблема сохраняется", "recovery": "Проблема устранена"}
    for event in events:
        if event["code"] not in SEVERITIES or event["kind"] not in names:
            raise OpsError("monitor_event_invalid")
        lines.append(names[event["kind"]] + ": " + ALERT_LABELS[event["code"]] +
                     " (" + event["code"] + ") [" + event["event"] + "]")
    return "\n".join(lines)


def deliver(state, config, now, sender=telegram_send, writer=atomic_json, suppressed=frozenset()):
    events = pending_events(state, now, suppressed)
    if not events or now < state["delivery_retry_at"]:
        return None
    # Persist the open incident before attempting delivery. A crash after Telegram
    # accepts but before the acknowledgement can cause an at-least-once duplicate.
    writer(STATE_PATH, state)
    try:
        sent = sender(config, notification_text(events)) is True
    except Exception:
        sent = False
    if sent:
        for event in events:
            item = state["incidents"][event["code"]]
            if event["kind"] == "recovery":
                item["alerted"] = False
                item["opened_at"] = None
                item["last_notified_at"] = None
            else:
                item["alerted"] = True
                item["last_notified_at"] = now
        state["delivery_failures"] = 0
        state["delivery_retry_at"] = 0
    else:
        state["delivery_failures"] = min(100000, state["delivery_failures"] + 1)
        state["delivery_retry_at"] = now + min(3600, 60 * 2**min(6, state["delivery_failures"] - 1))
    writer(STATE_PATH, state)
    return sent


def summary(checks, grace, delivered=None, heartbeat=None):
    # This is technical evidence only, never financial/business acceptance.
    return {
        "status": "checked", "read_only_probes": True,
        "ready": all(item["ok"] is True for item in checks),
        "technical_ready": all(item["ok"] is True for item in checks if item["severity"] == "critical"),
        "planned_deployment": grace,
        "checks": checks, "notification_delivered": delivered,
        "heartbeat_delivered": heartbeat,
    }


def run_monitor(config, *, check_only=False, now=None, state=None, collector=collect_checks,
                sender=telegram_send, heartbeat_sender=heartbeat_send, writer=atomic_json):
    now = int(time.time() if now is None else now)
    state = read_state() if state is None else state
    checks, grace = collector(config, state, now)
    if check_only:
        return summary(checks, grace)
    # A failed state store must leave the external deadman stale.
    writer(STATE_PATH, state)
    heartbeat = None
    # The heartbeat attests that this monitor cycle completed, including during
    # an outage or planned deployment. Independent HTTPS monitoring covers uptime.
    if (config.get("heartbeat") or {}).get("url_file") and any(
            item["code"] == "external_monitor_missing" and item["ok"] is True for item in checks):
        try:
            heartbeat = heartbeat_sender(config) is True
        except Exception:
            heartbeat = False
        checks.append(check("external_heartbeat_delivery", heartbeat))
    advance(state, checks, now)
    writer(STATE_PATH, state)
    active_codes = {item["code"] for item in checks if item["ok"] is not None}
    delivered = deliver(state, config, now, sender, writer, set(SEVERITIES) - active_codes)
    return summary(checks, grace, delivered, heartbeat)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Redacted VPS monitoring and Telegram incidents.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--test-notification", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise OpsError("root_required")
        config = load_config(args.config)
        if args.test_notification:
            sent = telegram_send(config, "БонусПро: проверка оповещений. Канал подключён.")
            print(json.dumps({"status": "notification_test", "delivered": sent}))
            return 0 if sent else 1
        if args.check_only:
            report = run_monitor(config, check_only=True)
        else:
            with locked(STATE_DIR / "monitor.lock"):
                report = run_monitor(config)
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if report["ready"] else 1
    except Exception:
        # Never log exceptions, commands, URLs, credentials or customer data.
        print(json.dumps({"status": "monitor_failed", "code": "monitor_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
