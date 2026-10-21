"""Real Docker/rclone backup recovery on disposable GitHub Actions fixtures only.

The transport is a private LOCAL rclone alias. This smoke does not establish that
any S3 account, production backup, or independent-machine recovery is configured.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tarfile
import time
from uuid import uuid4

PROJECT = "bonuspro-ci-container"
DB_ALIAS = "bonuspro-release-db-1"
IMAGE = "bonuspro:000000000018"

class SmokeFailed(RuntimeError):
    pass

def command(args, *, timeout=180):
    result = subprocess.run(
        [str(value) for value in args], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    if result.returncode:
        raise SmokeFailed("command_failed")
    return result.stdout

def guard(project):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.geteuid() != 0 or project != PROJECT:
        raise SmokeFailed("restricted_to_disposable_ci")
    original = project + "-db-1"
    state = json.loads(command([
        "docker", "inspect", "--format",
        '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
        '"running":{{json .State.Running}}}', original]))
    if state != {"project": PROJECT, "service": "db", "running": True}:
        raise SmokeFailed("unexpected_database")
    existing = command(["docker", "ps", "-a", "--format", "{{.Names}}"]).decode().splitlines()
    if DB_ALIAS in existing:
        raise SmokeFailed("production_name_present")
    if not Path("/usr/bin/rclone").is_file():
        raise SmokeFailed("rclone_missing")
    return original

def snapshot():
    statements = ["SELECT version_num FROM public.alembic_version;"]
    for table in ("ledger_transactions", "ledger_postings", "wallets"):
        statements.append(
            "SELECT '" + table + "|'||count(*)::text||'|'||"
            "md5(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' "
            "ORDER BY to_jsonb(t)::text COLLATE \"C\"),'')) "
            "FROM bonuspro." + table + " t;")
    statements.append(
        "SELECT 'database|'||datname FROM pg_database WHERE NOT datistemplate ORDER BY datname;")
    statements.append(
        "SELECT 'temporary|'||count(*)::text FROM pg_database "
        "WHERE datname LIKE 'bonuspro_restore_check_%';")
    value = command([
        "docker", "exec", DB_ALIAS, "psql", "-XAt", "-U", "postgres", "-d",
        "bonuspro", "-v", "ON_ERROR_STOP=1", "-c", "\n".join(statements)])
    lines = value.decode().splitlines()
    if (not lines or lines[0] != "0018" or not lines[-1:] == ["temporary|0"]
            or not any(line.startswith("ledger_transactions|") and
                       int(line.split("|")[1]) > 0 for line in lines)):
        raise SmokeFailed("seeded_journal_not_ready")
    return value

def private_write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(value)
    path.chmod(mode)
    if path.stat().st_uid != 0 or path.is_symlink():
        raise SmokeFailed("fixture_permissions")

def copy_private(source, target, *, app_secret=False):
    if source.is_symlink() or not source.is_file():
        raise SmokeFailed("fixture_source_invalid")
    private_write(target, source.read_bytes(), 0o400 if app_secret else 0o600)
    if app_secret:
        os.chown(target, 10001, 10001)

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def fixture(source, directory):
    release = directory / "release"
    encrypted = directory / "encrypted"
    state = directory / "state"
    library = directory / "library"
    remote = directory / "local-offsite"
    for path in (release, encrypted, state, library, remote):
        path.mkdir(mode=0o700)
    for name in ("compose.release.yaml", "compose.https.yaml", "deploy/Caddyfile.release"):
        copy_private(source / name, release / name)
    private_write(release / ".env.release", (
        "BONUSPRO_IMAGE=" + IMAGE + "\nPUBLIC_ORIGIN=https://ci.bonuspro.test\n"
        "MOYSKLAD_APP_ID=11111111-1111-4111-8111-111111111111\n"
        "MOYSKLAD_APP_UID=ci.test\n").encode())
    revision = command([
        "git", "-c", "safe.directory=" + str(source), "-C", source, "rev-parse", "HEAD"]
    ).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SmokeFailed("source_revision_invalid")
    private_write(release / ".marketplace-deployment.json", (
        json.dumps({"schema": "0018", "source_revision": revision, "image": IMAGE}) + "\n").encode())
    secrets = source / "deploy/secrets"
    if (base64.b64decode((secrets / "token_encryption_key").read_bytes().strip(), validate=True)
            != b"k" * 32):
        raise SmokeFailed("unexpected_secret_fixture")
    for path in secrets.iterdir():
        copy_private(path, release / "deploy/secrets" / path.name, app_secret=True)
    for name in ("offsite_backup.py", "ops_common.py"):
        copy_private(source / "scripts" / name, library / name)
    for name in ("backup_vps.py", "verify_backup_vps.py"):
        actual = library / ("actual_" + name)
        copy_private(source / "scripts" / name, actual)
        wrapper = (
            "import importlib.util\nfrom pathlib import Path\n"
            "spec=importlib.util.spec_from_file_location('isolated_ci_helper'," + repr(str(actual)) + ")\n"
            "module=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
            "module.ROOT=Path(" + repr(str(release)) + ")\n"
            "module.DEST=Path(" + repr(str(encrypted)) + ")\nmodule.main()\n")
        private_write(library / name, wrapper.encode())
    recovery_key = directory / "recovery_key"
    private_write(recovery_key, base64.b64encode(os.urandom(32)))
    key_info = recovery_key.stat()
    if stat.S_IMODE(key_info.st_mode) != 0o600 or key_info.st_uid != 0:
        raise SmokeFailed("recovery_key_permissions")
    s3_config = directory / "s3.conf"
    private_write(s3_config, (
        "[bonuspro-backup]\ntype = s3\nprovider = Other\n"
        "access_key_id = synthetic-ci-only\nsecret_access_key = synthetic-ci-only\n"
        "endpoint = https://s3-fixture.invalid\n").encode())
    alias_config = directory / "local-alias.conf"
    private_write(alias_config, (
        "[bonuspro-backup]\ntype = alias\nremote = " + str(remote) + "\n").encode())
    config = {
        "release_root": str(release), "recovery_key_exported": True,
        "backup": {
            "enabled": True, "remote": "bonuspro-backup:bucket/isolated-ci",
            "expected_schema": "0018", "rclone_config": str(s3_config),
            "recovery_key_file": str(recovery_key), "offsite_location_confirmed": True,
        },
    }
    return release, encrypted, state, library, remote, alias_config, config

def fresh_cluster_recovery(module, receipt, config, encrypted, directory):
    """Restore downloaded bytes and recovered roles onto a separate empty cluster."""
    run_id = receipt["run_id"]
    recovery = encrypted / (run_id + ".recovery-tar-bpbk.download")
    database = encrypted / (run_id + ".database-pgdump-bpbk.pgdump.bpbk")
    with recovery.open("rb") as source, tempfile.TemporaryFile() as plaintext:
        subprocess.run(module.crypto_command(IMAGE, config["backup"]["recovery_key_file"], "decrypt"),
                       stdin=source, stdout=plaintext, stderr=subprocess.DEVNULL,
                       check=True, timeout=60)
        plaintext.seek(0)
        with tarfile.open(fileobj=plaintext, mode="r:") as archive:
            names = ("admin_database_url", "app_database_url",
                     "lifecycle_database_url", "migration_database_url", "token_encryption_key")
            recovered = {name: archive.extractfile("deploy/secrets/" + name).read().decode().strip()
                         for name in names}
    key = directory / "recovered-application-key"
    private_write(key, recovered.pop("token_encryption_key").encode())
    name = "bonuspro-ci-fresh-" + uuid4().hex[:12]
    created = False
    try:
        command(["docker", "run", "-d", "--name", name, "--network", PROJECT + "_default",
                 "--log-driver", "none", "-e", "POSTGRES_DB=bonuspro",
                 "-e", "POSTGRES_PASSWORD=ci-only-independent", "postgres:17-alpine"])
        created = True
        deadline = time.monotonic() + 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailed("fresh_database_not_ready")
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                                    "-d", "bonuspro"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=min(5, remaining), check=False)
            if ready.returncode == 0:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        payload = {"host": name, "secrets": recovered}
        provision = (
            "import json,os,sys;from psycopg.conninfo import conninfo_to_dict,make_conninfo;"
            "p=json.load(sys.stdin);mapping={'DATABASE_ADMIN_URL':'admin_database_url',"
            "'DATABASE_URL':'app_database_url','LIFECYCLE_DATABASE_URL':'lifecycle_database_url',"
            "'DATABASE_MIGRATION_URL':'migration_database_url'};"
            "\nfor env,key in mapping.items():\n"
            " d=conninfo_to_dict(p['secrets'][key]);d['host']=p['host'];"
            "d['password']='ci-only-independent' if env=='DATABASE_ADMIN_URL' else d['password'];"
            "os.environ[env]=make_conninfo(**d)\n"
            "from deploy.bootstrap import main;main()")
        def app(code):
            subprocess.run(["docker", "run", "--rm", "--network", PROJECT + "_default",
                            "--log-driver", "none", "-i", IMAGE, "python", "-c", code],
                           input=json.dumps(payload).encode(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=60)
        app(provision)
        with database.open("rb") as source, tempfile.TemporaryFile() as plaintext:
            subprocess.run(module.crypto_command(IMAGE, key, "decrypt"), stdin=source,
                           stdout=plaintext, stderr=subprocess.DEVNULL, check=True, timeout=60)
            plaintext.seek(0)  # Authentication must finish before the new cluster sees SQL.
            subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", "postgres",
                            "-d", "bonuspro", "--exit-on-error"], stdin=plaintext,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=60)
        verify = (
            "import json,sys,psycopg;from psycopg.conninfo import conninfo_to_dict,make_conninfo;"
            "from bonuspro.adapters.postgres.ledger import Ledger;p=json.load(sys.stdin);"
            "d=conninfo_to_dict(p['secrets']['app_database_url']);d['host']=p['host'];"
            "dsn=make_conninfo(**d);"
            "assert Ledger(dsn).snapshot('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',"
            "'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')['balance']==1000;"
            "c=psycopg.connect(dsn);"
            "assert c.execute('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()==(False,);"
            "assert c.execute('SELECT version_num FROM public.alembic_version').fetchone()==('0018',);c.close();"
            "d=conninfo_to_dict(p['secrets']['admin_database_url']);d.update(host=p['host'],password='ci-only-independent');"
            "c=psycopg.connect(make_conninfo(**d));"
            "assert c.execute(\"SELECT count(*) FROM (SELECT t.id FROM bonuspro.ledger_transactions t "
            "LEFT JOIN bonuspro.ledger_postings q ON q.tenant_id=t.tenant_id AND q.transaction_id=t.id "
            "GROUP BY t.tenant_id,t.id,t.delta HAVING count(q.account)<>2 OR sum(q.amount)<>0 "
            "OR sum(q.amount) FILTER(WHERE q.account='customer') IS DISTINCT FROM t.delta::numeric) invalid\").fetchone()==(0,);"
            "c.close()")
        app(verify)
    finally:
        if created:
            command(["docker", "rm", "-f", "-v", name])

def exercise(source, project):
    original = guard(project)
    renamed = False
    os.umask(0o077)
    try:
        command(["docker", "tag", "bonuspro:ci", IMAGE])
        command(["docker", "rename", original, DB_ALIAS])
        renamed = True
        before = snapshot()
        with tempfile.TemporaryDirectory(prefix="bonuspro-ci-operations-") as temp:
            directory = Path(temp)
            release, encrypted, state, library, remote, alias_config, config = fixture(
                source, directory)
            # Only root-owned copies of operational code are imported/executed.
            sys.path.insert(0, str(library))
            try:
                common = load_module("ops_common", library / "ops_common.py")
                common.RELEASE_ROOT = release
                common.STATE_DIR = state
                module = load_module("ci_isolated_offsite_backup", library / "offsite_backup.py")
                module.RELEASE_ROOT = release
                module.BACKUP_DIR = encrypted
                module.HELPER_DIR = library
                module.RCLONE = Path("/usr/bin/rclone")
                corrupt = False
                restores = 0
                copies = 0
                corruption_applied = False

                def process(args, **options):
                    nonlocal copies, restores, corruption_applied
                    actual = list(args)
                    transfer = str(actual[0]) == "/usr/bin/rclone"
                    if transfer:
                        copies += 1
                        actual[actual.index("--config") + 1] = str(alias_config)
                    if len(actual) > 2 and str(actual[2]).endswith("/verify_backup_vps.py"):
                        restores += 1
                    result = subprocess.run(actual, **options)
                    if transfer and corrupt and result.returncode == 0:
                        index = actual.index("copyto")
                        origin, target = actual[index + 1:index + 3]
                        if str(origin).startswith("bonuspro-backup:") and str(origin).endswith(
                                "/database.pgdump.bpbk"):
                            with open(target, "r+b") as output:
                                first = output.read(1)
                                if not first:
                                    raise SmokeFailed("empty_download")
                                output.seek(0)
                                output.write(bytes([first[0] ^ 1]))
                                output.flush()
                                os.fsync(output.fileno())
                            corruption_applied = True
                    return result

                receipt = module.run_backup(config, run=process, state_dir=state)
                if (receipt["image"] != IMAGE or receipt["schema"] != "0018"
                        or not all(receipt.get(key) is True for key in (
                            "offsite_verified", "restoration_verified", "recovery_bundle_verified"))
                        or receipt.get("independent_machine_restore_verified") is not False
                        or restores != 1 or copies != 9):
                    raise SmokeFailed("success_receipt_invalid")
                fresh_cluster_recovery(module, receipt, config, encrypted, directory)
                if snapshot() != before:
                    raise SmokeFailed("journal_changed")
                success_path = state / "backup-success.json"
                previous_receipt = success_path.read_bytes()
                if json.loads(previous_receipt) != receipt:
                    raise SmokeFailed("receipt_not_persisted")
                manifests = list(remote.rglob("manifest.json"))
                if len(manifests) != 1 or json.loads(manifests[0].read_bytes()) != receipt:
                    raise SmokeFailed("completion_manifest_invalid")
                corrupt = True
                try:
                    module.run_backup(config, run=process, state_dir=state)
                except common.OpsError as error:
                    if module.error_code(error) != "BACKUP_HASH_MISMATCH":
                        raise SmokeFailed("unexpected_corruption_failure") from None
                else:
                    raise SmokeFailed("corruption_accepted")
                if (not corruption_applied or copies != 14 or restores != 1
                        or success_path.read_bytes() != previous_receipt
                        or list(remote.rglob("manifest.json")) != manifests
                        or snapshot() != before):
                    raise SmokeFailed("corruption_changed_verified_state")
            finally:
                sys.path.remove(str(library))
    finally:
        if renamed:
            command(["docker", "rename", DB_ALIAS, original])
    print("CI_OPERATIONS_SMOKE_OK real_crypto=true real_restore=true fresh_cluster_recovery=true local_transport_fixture=true real_s3_verified=false")

def run_smoke(source, project=PROJECT):
    source = Path(source).resolve()
    if os.environ.get("GITHUB_ACTIONS") != "true" or project != PROJECT:
        raise SmokeFailed("restricted_to_disposable_ci")
    if source != Path(__file__).resolve().parents[1]:
        raise SmokeFailed("unexpected_source")
    if os.geteuid() == 0:
        exercise(source, project)
        return
    result = subprocess.run([
        "sudo", "--preserve-env=GITHUB_ACTIONS", "/usr/bin/python3", "-I",
        str(source / "scripts/ci_operations_smoke.py"), "--project", project,
    ], check=False, timeout=720)
    if result.returncode:
        raise SmokeFailed("isolated_operations_smoke_failed")

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=PROJECT)
    args = parser.parse_args(argv)
    try:
        exercise(Path(__file__).resolve().parents[1], args.project)
        return 0
    except (Exception, KeyboardInterrupt):
        print("CI_OPERATIONS_SMOKE_FAILED")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
