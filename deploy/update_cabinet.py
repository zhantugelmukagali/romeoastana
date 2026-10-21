"""Compatible cabinet deployment with isolated, guarded upgrades through compatible schema 0016."""
import argparse,json,os,re,stat,subprocess,sys
from uuid import UUID
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen

class UpdateFailed(RuntimeError):pass

def run(args,*,capture=False,timeout=600):
    result=subprocess.run(args,check=True,timeout=timeout,stdout=subprocess.PIPE if capture else None,text=True)
    return result.stdout.strip() if capture else None

def replacement(text,image):
    if not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}",image):raise ValueError("immutable image required")
    lines=text.splitlines()
    if sum(line.startswith("BONUSPRO_IMAGE=") for line in lines)!=1:raise ValueError("exactly one image setting required")
    return "\n".join("BONUSPRO_IMAGE="+image if line.startswith("BONUSPRO_IMAGE=") else line for line in lines)+"\n"

def atomic_write(path,content,original):
    temporary=path.with_name(path.name+".cabinet-tmp-"+str(os.getpid()))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"w") as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        if (original.st_uid,original.st_gid)!=(os.geteuid(),os.getegid()):os.chown(temporary,original.st_uid,original.st_gid)
        os.chmod(temporary,stat.S_IMODE(original.st_mode)&0o600)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def compose(root,*args):
    return ["docker","compose","--project-directory",str(root),"--env-file",str(root/".env.release"),
            "-p","bonuspro-release","-f",str(root/"compose.release.yaml"),"-f",str(root/"compose.https.yaml"),*args]

def asset_names(source):
    # Cover every cabinet/widget module, including the user-context bridge.
    names=sorted(path.name for path in (source/"web/assets").iterdir()
                 if path.is_file() and path.suffix in (".js",".css"))
    if not names or len(names)>64:raise UpdateFailed("Unexpected runtime asset manifest")
    return names

def verify_public(source):
    base="https://loyalty.addwise.kz"
    with urlopen(base+"/health/ready",timeout=15) as response:
        if response.status!=200 or json.loads(response.read(1024)).get("status")!="ready":raise UpdateFailed("readiness failed")
    for name in asset_names(source):
        with urlopen(base+"/assets/"+name,timeout=15) as response:
            if response.status!=200 or response.read(262145)!=(source/"web/assets"/name).read_bytes():
                raise UpdateFailed("public assets do not match the image source")

def switch(root,source,image,*,verify=verify_public):
    env=root/".env.release"
    if env.is_symlink():raise UpdateFailed("release environment must be a regular file")
    old=env.read_text();original=env.stat();new=replacement(old,image)
    backup=root/(".env.release.before-cabinet-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+str(os.getpid()))
    with backup.open("x") as target:
        os.chmod(backup,0o600);target.write(old);target.flush();os.fsync(target.fileno())
    attempted=False
    try:
        atomic_write(env,new,original)
        run(compose(root,"config","--quiet"))
        attempted=True
        run(compose(root,"up","-d","--no-build","--no-deps","--force-recreate","--wait","--wait-timeout","120","app"))
        verify(source)
    except Exception:
        atomic_write(env,old,original)
        if attempted:
            try:run(compose(root,"up","-d","--no-build","--no-deps","--force-recreate","--wait","--wait-timeout","120","app"))
            except Exception:raise UpdateFailed("UPDATE_FAILED: old configuration restored; application rollback needs inspection") from None
        raise UpdateFailed("UPDATE_FAILED: previous configuration restored") from None

def source_revision(source):
    source=Path(source).resolve()
    git=["git","-c","safe.directory="+str(source),"-C",str(source)]
    if run(git+["status","--porcelain","--untracked-files=all"],capture=True):
        raise UpdateFailed("Source checkout has uncommitted or untracked files")
    sha=run(git+["rev-parse","HEAD"],capture=True)
    if not re.fullmatch("[0-9a-f]{40}",sha):raise UpdateFailed("invalid source revision")
    return sha


DB_CONTAINER="bonuspro-release-db-1"
QUIET_SERVICES=("bonuspro-release-web-1","bonuspro-release-app-1","bonuspro-release-worker-1")

def database_sql(database,statement,*,capture=False):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}",database):
        raise UpdateFailed("Invalid database selection")
    result=subprocess.run(["docker","exec","-i",DB_CONTAINER,"psql","-X","-U","postgres","-d",database,
                           "-v","ON_ERROR_STOP=1","-At"],input=statement,text=True,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=600)
    if result.returncode:
        raise UpdateFailed("Database verification or schema operation failed")
    return result.stdout.strip() if capture else None

def database_schema(database="bonuspro"):
    return database_sql(database,"SELECT version_num FROM public.alembic_version",capture=True)

def compatible_database_lineage(schema):
    expected={"0013":(False,False,False),"0014":(True,False,False),
              "0015":(True,True,False),"0016":(True,True,True)}
    if schema not in expected:return False
    actual=json.loads(database_sql("bonuspro","""
        SELECT json_build_array(
          to_regclass('bonuspro.order_customer_rebindings') IS NOT NULL,
          to_regclass('bonuspro.personal_discount_applications') IS NOT NULL,
          to_regclass('bonuspro.presale_undo_requests') IS NOT NULL)
    """,capture=True))
    return isinstance(actual,list) and all(type(value) is bool for value in actual) and tuple(actual)==expected[schema]

def save_command(args,path):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb") as target:
            result=subprocess.run(args,check=True,timeout=600,stdout=target,stderr=subprocess.PIPE)
            target.flush();os.fsync(target.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if not path.stat().st_size:
        raise UpdateFailed("Protected backup is empty")

def dump_database(path):
    save_command(["docker","exec",DB_CONTAINER,"pg_dump","-U","postgres","-d","bonuspro","-Fc"],path)

def protected_backup(root):
    base=root.parent/"bonuspro-backups"
    if base.is_symlink():raise UpdateFailed("Backup directory must not be a symlink")
    base.mkdir(mode=0o700,exist_ok=True)
    backup=base/("cabinet-upgrade-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid()))
    backup.mkdir(mode=0o700)
    os.chmod(backup,0o700)
    save_command(["tar","--dereference","-czf","-","-C",str(root),
                  ".env.release","deploy/secrets"],backup/"config-and-keys.tar.gz")
    return backup

def migration_owner(database):
    owner=json.loads(database_sql(database,"""
        SELECT json_build_object('role',quote_ident(r.rolname),
            'safe',NOT r.rolsuper AND NOT r.rolbypassrls)
        FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
        WHERE c.oid='bonuspro.order_contexts'::regclass
    """,capture=True))
    if not owner.get("safe") or not isinstance(owner.get("role"),str):
        raise UpdateFailed("Migration owner must not bypass row security")
    return owner["role"]

def guarded_schema_sql(body,*,expected,result,owner,pre_owner=""):
    if (expected,result) not in (("0013","0014"),("0014","0013"),("0014","0015"),("0015","0014"),("0015","0016"),("0016","0015")):
        raise UpdateFailed("Unsupported schema transition")
    return ("BEGIN;\nSET LOCAL lock_timeout='5s';\nSET LOCAL statement_timeout='60s';\n"
            "LOCK TABLE public.alembic_version IN EXCLUSIVE MODE;\n"
            "DO $$ BEGIN IF (SELECT count(*) FROM public.alembic_version)<>1 OR "
            "(SELECT version_num FROM public.alembic_version) IS DISTINCT FROM '"+expected+"' "
            "THEN RAISE EXCEPTION 'Unexpected schema'; END IF; END $$;\n"
            +pre_owner+"\nSET LOCAL ROLE "+owner+";\n"+body+"\n"
            "UPDATE public.alembic_version SET version_num='"+result+"' WHERE version_num='"+expected+"';\nCOMMIT;\n")

def apply_rebinding_migration(database,source):
    sql=(source/"migrations/versions/0014_order_rebinding.sql").read_text()
    if not sql.strip():raise UpdateFailed("Exact compatible migration is required")
    database_sql(database,guarded_schema_sql(sql,expected="0013",result="0014",owner=migration_owner(database)))

def rollback_rebinding_migration(database="bonuspro"):
    # This is an unpublished pre-traffic DDL rollback, never an application
    # downgrade or a financial database restore. A single audit row forbids it.
    pre_owner="""
        LOCK TABLE bonuspro.order_customer_rebindings IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM bonuspro.order_customer_rebindings) THEN
            RAISE EXCEPTION 'Rebinding history prevents schema rollback';
          END IF;
        END $$;
    """
    body="""
        DROP TRIGGER reservation_order_binding ON bonuspro.reservations;
        DROP FUNCTION bonuspro.guard_reservation_order_binding();
        DROP FUNCTION bonuspro.rebind_order_customer(uuid,uuid,text,text,uuid);
        DROP TRIGGER immutable_record ON bonuspro.order_contexts;
        CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.order_contexts
          FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
        DROP FUNCTION bonuspro.guard_order_context_binding();
        DROP FUNCTION bonuspro.order_has_financial_history(uuid,uuid);
        DROP TABLE bonuspro.order_customer_rebindings;
        ALTER TABLE bonuspro.order_contexts DROP COLUMN binding_revision;
    """
    database_sql(database,guarded_schema_sql(body,expected="0014",result="0013",owner=migration_owner(database),pre_owner=pre_owner))


PERSONAL_DISCOUNT_TABLES=("personal_discount_policy_versions","customer_personal_discount_versions",
                          "personal_discount_applications","personal_discount_voids")

def apply_personal_discount_migration(database,source):
    body=(source/"migrations/versions/0015_personal_discounts.sql").read_text()
    if not body.strip():raise UpdateFailed("Exact compatible personal-discount migration is required")
    database_sql(database,guarded_schema_sql(body,expected="0014",result="0015",owner=migration_owner(database)))

def rollback_personal_discount_migration(source,database="bonuspro"):
    body=(source/"migrations/versions/0015_personal_discounts.down.sql").read_text()
    if not body.strip():raise UpdateFailed("Exact compatible personal-discount rollback is required")
    locks="".join("LOCK TABLE bonuspro."+name+" IN ACCESS EXCLUSIVE MODE;\n" for name in PERSONAL_DISCOUNT_TABLES)
    occupied=" OR ".join("EXISTS(SELECT 1 FROM bonuspro."+name+")" for name in PERSONAL_DISCOUNT_TABLES)
    # FORCE RLS must not hide another tenant's policy, assignment, application or
    # void. Lock/check globally as postgres before selecting the migration owner.
    pre_owner=locks+"DO $$ BEGIN IF "+occupied+" THEN RAISE EXCEPTION 'Personal discount history prevents schema rollback'; END IF; END $$;\n"
    database_sql(database,guarded_schema_sql(body,expected="0015",result="0014",
                 owner=migration_owner(database),pre_owner=pre_owner))

PRESALE_UNDO_TABLES=("presale_undo_requests","presale_undo_completions")

def apply_presale_undo_migration(database,source):
    body=(source/"migrations/versions/0016_presale_undo.sql").read_text()
    if not body.strip():raise UpdateFailed("Exact compatible presale-undo migration is required")
    database_sql(database,guarded_schema_sql(body,expected="0015",result="0016",owner=migration_owner(database)))

def rollback_presale_undo_migration(source,database="bonuspro"):
    body=(source/"migrations/versions/0016_presale_undo.down.sql").read_text()
    if not body.strip():raise UpdateFailed("Exact compatible presale-undo rollback is required")
    locks="".join("LOCK TABLE bonuspro."+name+" IN ACCESS EXCLUSIVE MODE;\n" for name in PRESALE_UNDO_TABLES)
    occupied=" OR ".join("EXISTS(SELECT 1 FROM bonuspro."+name+")" for name in PRESALE_UNDO_TABLES)
    # Never let FORCE RLS hide a different tenant's immutable undo request/receipt.
    pre_owner=locks+"DO $$ BEGIN IF "+occupied+" THEN RAISE EXCEPTION 'Presale undo history prevents schema rollback'; END IF; END $$;\n"
    database_sql(database,guarded_schema_sql(body,expected="0016",result="0015",
                 owner=migration_owner(database),pre_owner=pre_owner))

def apply_compatible_migrations(database,source):
    observed=database_schema(database)
    if observed=="0013":
        apply_rebinding_migration(database,source)
        observed=database_schema(database)
    if observed=="0014":
        apply_personal_discount_migration(database,source)
        observed=database_schema(database)
    if observed=="0015":
        apply_presale_undo_migration(database,source)
        observed=database_schema(database)
    if observed!="0016":raise UpdateFailed("Unexpected compatible upgrade schema")

def rollback_compatible_migrations(source,original_schema,database="bonuspro"):
    if original_schema not in ("0013","0014","0015"):
        raise UpdateFailed("Unsupported original compatible schema")
    observed=database_schema(database)
    if observed=="0016":
        rollback_presale_undo_migration(source,database)
        observed=database_schema(database)
    if original_schema in ("0013","0014") and observed=="0015":
        rollback_personal_discount_migration(source,database)
        observed=database_schema(database)
    if original_schema=="0013" and observed=="0014":
        rollback_rebinding_migration(database)
        observed=database_schema(database)
    if observed!=original_schema:raise UpdateFailed("Original schema could not be restored")

def financial_counts(database):
    return json.loads(database_sql(database,"""
        SELECT json_build_object(
          'transactions',(SELECT count(*) FROM bonuspro.ledger_transactions),
          'postings',(SELECT count(*) FROM bonuspro.ledger_postings),
          'reservations',(SELECT count(*) FROM bonuspro.reservations),
          'accruals',(SELECT count(*) FROM bonuspro.order_accruals))
    """,capture=True))


def personal_discount_counts(database):
    return json.loads(database_sql(database,"SELECT json_build_object("+",".join(
        "'"+name+"',(SELECT count(*) FROM bonuspro."+name+")" for name in PERSONAL_DISCOUNT_TABLES)+")",capture=True))

def presale_undo_counts(database):
    return json.loads(database_sql(database,"SELECT json_build_object("+",".join(
        "'"+name+"',(SELECT count(*) FROM bonuspro."+name+")" for name in PRESALE_UNDO_TABLES)+")",capture=True))

def validate_restored_journal(database):
    database_sql(database,"""
        DO $$ BEGIN
          IF EXISTS(
            SELECT 1 FROM bonuspro.ledger_transactions t
            LEFT JOIN bonuspro.ledger_postings p
              ON p.tenant_id=t.tenant_id AND p.transaction_id=t.id
            GROUP BY t.tenant_id,t.id,t.delta
            HAVING count(p.account)<>2
              OR sum(p.amount) FILTER(WHERE p.account='customer') IS DISTINCT FROM t.delta::numeric
              OR sum(p.amount) FILTER(WHERE p.account='system') IS DISTINCT FROM (-t.delta)::numeric
          ) THEN RAISE EXCEPTION 'Unbalanced restored journal'; END IF;
        END $$;
    """)

def verify_restore(backup,source):
    import secrets
    name="bonuspro_upgrade_check_"+str(os.getpid())+"_"+secrets.token_hex(4)
    created=False
    try:
        database_sql("postgres","CREATE DATABASE "+name)
        created=True
        with (backup/"database.dump").open("rb") as dump:
            subprocess.run(["docker","exec","-i",DB_CONTAINER,"pg_restore","-U","postgres","-d",name,
                            "--exit-on-error"],stdin=dump,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           check=True,timeout=600)
        restored_schema=database_schema(name)
        if restored_schema not in ("0013","0014","0015"):raise UpdateFailed("Restored backup has an unexpected schema")
        validate_restored_journal(name)
        before=financial_counts(name)
        personal_before=personal_discount_counts(name) if restored_schema=="0015" else dict.fromkeys(PERSONAL_DISCOUNT_TABLES,0)
        apply_compatible_migrations(name,source)
        if database_schema(name)!="0016" or before!=financial_counts(name):
            raise UpdateFailed("Isolated migration changed financial record counts")
        if personal_before!=personal_discount_counts(name):
            raise UpdateFailed("Isolated migration changed personal-discount history")
        if any(presale_undo_counts(name).values()):
            raise UpdateFailed("Isolated migration unexpectedly created presale-undo history")
        validate_restored_journal(name)
    finally:
        if created:
            database_sql("postgres","DROP DATABASE "+name)

def service_states():
    present=run(["docker","ps","-a","--filter","label=com.docker.compose.project=bonuspro-release",
                 "--format","{{.Names}}"],capture=True).splitlines()
    states={}
    for name in present:
        if name=="bonuspro-release-db-1":continue
        info=json.loads(run(["docker","inspect",name,"--format","{{json .State}}"],capture=True))
        if name not in QUIET_SERVICES:
            if info.get("Running"):raise UpdateFailed("Another release operation is running")
            continue
        states[name]=bool(info.get("Running"))
    if "bonuspro-release-app-1" not in states or "bonuspro-release-web-1" not in states:
        raise UpdateFailed("Existing release application and HTTPS gateway are required")
    return states

def stop_for_upgrade(states):
    for name in QUIET_SERVICES:
        if states.get(name):
            run(["docker","stop","--timeout","120",name])
    observed=service_states()
    if any(observed.values()):
        raise UpdateFailed("Application, gateway and worker must stop before migration")

def verify_local(source):
    from urllib.request import Request
    base="http://127.0.0.1:18080"
    def request(path):
        return urlopen(Request(base+path,headers={"Host":"loyalty.addwise.kz"}),timeout=15)
    with request("/health/ready") as response:
        if response.status!=200 or json.loads(response.read(1024)).get("status")!="ready":
            raise UpdateFailed("Local schema readiness failed")
    for name in asset_names(source):
        with request("/assets/"+name) as response:
            if response.status!=200 or response.read(262145)!=(source/"web/assets"/name).read_bytes():
                raise UpdateFailed("Local assets do not match the new image")

def restore_previous_services(root,states):
    if states.get("bonuspro-release-app-1"):
        run(compose(root,"up","-d","--no-build","--no-deps","--force-recreate",
                    "--wait","--wait-timeout","120","app"))
    if states.get("bonuspro-release-worker-1"):
        run(["docker","start","bonuspro-release-worker-1"])
    if states.get("bonuspro-release-web-1"):
        run(["docker","start","bonuspro-release-web-1"])

def upgrade_schema_and_switch(root,source,image,*,verify=verify_public):
    env=root/".env.release"
    if env.is_symlink():raise UpdateFailed("Release environment must be a regular file")
    old=env.read_text();original=env.stat();new=replacement(old,image)
    original_schema=database_schema()
    if original_schema not in ("0013","0014","0015"):raise UpdateFailed("Compatible upgrade must start from schema 0013, 0014 or 0015")
    states=service_states()
    backup=protected_backup(root)
    try:
        dump_database(backup/"database.dump")
        verify_restore(backup,source)
    except Exception:
        raise UpdateFailed("UPDATE_PRECHECK_FAILED: live services were not changed. Backup: "+str(backup)) from None
    stopped=False;exposed=False
    try:
        stopped=True
        stop_for_upgrade(states)
        if database_schema()!=original_schema:raise UpdateFailed("Schema changed before the maintenance window")
        dump_database(backup/"database.pre-upgrade.dump")
        apply_compatible_migrations("bonuspro",source)
        atomic_write(env,new,original)
        run(compose(root,"config","--quiet"))
        run(compose(root,"up","-d","--no-build","--no-deps","--force-recreate",
                    "--wait","--wait-timeout","120","app"))
        verify_local(source)
        # Treat even an unsuccessful start as possible exposure. Financial or
        # rebinding activity after this point forbids automatic schema downgrade.
        exposed=True
        if states.get("bonuspro-release-web-1"):
            run(["docker","start","bonuspro-release-web-1"])
        verify(source)
    except Exception:
        if exposed:
            raise UpdateFailed("UPDATE_NOT_CONFIRMED: new schema/image retained after HTTPS exposure; inspect services. Backup: "+str(backup)) from None
        try:
            for name in QUIET_SERVICES:
                if name=="bonuspro-release-app-1":
                    current=service_states()
                    if current.get(name):run(["docker","stop","--timeout","120",name])
            observed=database_schema()
            if observed!=original_schema:
                if any(service_states().values()):raise UpdateFailed("Release traffic prevents schema rollback")
                rollback_compatible_migrations(source,original_schema)
            if database_schema()!=original_schema:raise UpdateFailed("Unexpected schema during rollback")
            atomic_write(env,old,original)
            if stopped:restore_previous_services(root,states)
        except Exception:
            # If the schema could not be proven old, never restart an old image.
            try:
                if database_schema()!=original_schema:atomic_write(env,new,original)
            except Exception:pass
            raise UpdateFailed("UPDATE_ROLLBACK_BLOCKED: services require inspection; no financial history was restored or erased. Backup: "+str(backup)) from None
        raise UpdateFailed("UPDATE_FAILED: schema "+original_schema+" and previous services restored. Backup: "+str(backup)) from None
    return backup

def tenant_selector(value):
    try:return str(UUID(value))
    except (TypeError,ValueError,AttributeError):
        raise argparse.ArgumentTypeError("Tenant must be a valid UUID") from None

def start_updated_processing(root,source,image,tenant=None):
    """Restore the current-image worker only after the app update has committed."""
    command=[sys.executable,str(source/"deploy/start_processing.py"),"--root",str(root)]
    if tenant is not None:command.extend(["--tenant",tenant])
    try:
        run(command,timeout=900)
        state=run(["docker","inspect","bonuspro-release-worker-1","--format",
                   "{{.Config.Image}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"],capture=True)
        if state!=image+"|true|healthy":
            raise UpdateFailed("Current-image worker is not healthy")
    except Exception:
        # Public traffic may already have used this schema/image. A processing
        # failure must never re-enter an application or database rollback.
        raise UpdateFailed("UPDATE_INCOMPLETE: application and schema retained; the current-image worker was not confirmed. Run start_processing.py with the selected tenant.") from None

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"))
    parser.add_argument("--tenant",type=tenant_selector,help="Selected MoySklad account UUID; otherwise use the single enabled installation")
    parser.add_argument("--defer-processing",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run update_cabinet.py with sudo")
    os.umask(0o077);root=args.root.resolve();source=Path(__file__).resolve().parents[1]
    try:
        schema=database_schema()
        if schema not in ("0013","0014","0015","0016") or 'revision != [("0016",)]' not in (source/"backend/bonuspro/api/app.py").read_text():
            raise UpdateFailed("Compatible schema-0016 source and an existing schema 0013/0014/0015/0016 release are required; no migration was attempted")
        if not (source/"migrations/versions/0014_order_rebinding.sql").is_file() or not (source/"migrations/versions/0016_presale_undo.sql").is_file():
            raise UpdateFailed("Compatible migration lineage is required; a main-branch image cannot use this upgrade helper")
        if not compatible_database_lineage(schema):
            raise UpdateFailed("Database schema lineage does not match the compatible source; no migration or image switch was attempted")
        sha=source_revision(source)
        image="bonuspro:"+sha[:12]
        run(["docker","build","--pull","-t",image,str(source)])
        run(["docker","run","--rm","--network","none","--read-only","--cap-drop","ALL",
             "--security-opt","no-new-privileges:true",image,"python","-c",
             "from bonuspro.adapters.postgres.portal import Portal; from pathlib import Path; assert Path('/app/web/assets/portal.js').read_bytes()"])
        if schema in ("0013","0014","0015"):
            backup=upgrade_schema_and_switch(root,source,image)
            print("Protected backup and isolated migration verified: "+str(backup))
        else:
            switch(root,source,image)
        print("Schema 0016 is confirmed. Credentials and financial journal history were preserved.")
        if args.defer_processing:
            print("CABINET_UPDATED_PROCESSING_PENDING: "+image+"; finish_release.py must confirm the worker before delivery.")
            return
        start_updated_processing(root,source,image,args.tenant)
        print("CABINET_READY: "+image+" · https://loyalty.addwise.kz/admin")
    except UpdateFailed as error:raise SystemExit(str(error))
    except Exception:raise SystemExit("UPDATE_FAILED: no successful cabinet update was confirmed") from None

if __name__=="__main__":main()
