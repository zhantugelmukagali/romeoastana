"""Verified compatible-0015 to main deployment. Never restore over a live journal."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1, sha256
import fcntl, json, os, re, stat, subprocess, sys, time
from pathlib import Path
from uuid import uuid4
from update_cabinet import atomic_write, replacement, source_revision, verify_local, verify_public

class MarketplaceFailed(RuntimeError): pass

SOURCE_BLOBS = {
    "0014_event_sync.sql": "bb8f64bee6bb720d536df0bdadbbbd6e90fec814",
    "0015_worker_directory.sql": "e0de543b6b630e74552910600e38df170ddb51c2",
    "0016_order_rebinding.sql": "ed4c8aa163b737ffdaae54c8e9512caad449f2df",
    "0017_personal_discounts.sql": "6d59e3a4669c921e0abf5d98dda76f235d3a0e37",
}
SERVICES = ("web", "app", "worker", "worker-pool")
FIXED_SETTINGS = "SET TimeZone='UTC'; SET DateStyle='ISO,YMD'; SET bytea_output='hex'; SET extra_float_digits=3;"

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}",value):
        raise MarketplaceFailed("Unsupported database identifier")
    return '"'+value+'"'

def checked(args, *, capture=False, timeout=900, **options):
    try:
        options.setdefault("stdout",subprocess.PIPE if capture else None)
        options.setdefault("stderr",subprocess.PIPE)
        result=subprocess.run(args,check=False,timeout=timeout,**options)
    except (OSError,subprocess.SubprocessError):
        raise MarketplaceFailed("Deployment command did not complete") from None
    if result.returncode:
        raise MarketplaceFailed("Deployment command failed; no readiness was claimed")
    return result.stdout if capture else None

def canonical_schema(data):
    # Keep comments, ACL, ownership and stored function bodies exactly.
    lines=data.splitlines(keepends=True)
    first=next((i for i,line in enumerate(lines) if line.strip() and not line.startswith(b"--")),None)
    last=next((i for i in range(len(lines)-1,-1,-1) if lines[i].strip()),None)
    if first is not None and lines[first].startswith(b"\\restrict "):
        token=lines[first].strip().split(b" ",1)[1]
        if not re.fullmatch(rb"[A-Za-z0-9]+",token) or lines[last].strip()!=b"\\unrestrict "+token:
            raise MarketplaceFailed("Unexpected schema dump framing")
        lines[first]=b"";lines[last]=b""
    return b"".join(lines)

def verify_source(source):
    for name,expected in SOURCE_BLOBS.items():
        data=(source/"migrations/versions"/name).read_bytes()
        actual=sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()
        if actual!=expected:
            raise MarketplaceFailed("Bridge migration fingerprint differs; review required")
    app=(source/"backend/bonuspro/api/app.py").read_text()
    found=re.findall(r'revision\s*!=\s*\[\(\s*["\x27](\d{4})["\x27]\s*,\s*\)\]',app)
    if len(found)!=1 or found[0] not in ("0017","0018"):
        raise MarketplaceFailed("Verified main schema-0017/0018 source is required")
    head=found[0]
    if not any(re.search(r'revision\s*=\s*["\x27]'+head+r'["\x27]',p.read_text())
               for p in (source/"migrations/versions").glob("*.py")):
        raise MarketplaceFailed("Application schema and migrations differ")
    return head

class Database:
    def __init__(self,container="bonuspro-release-db-1"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+",container):raise MarketplaceFailed("Invalid database container")
        self.container=container
    def sql(self,database,body):
        identifier(database)
        data=checked(["docker","exec","-i",self.container,"psql","-XqAt","-U","postgres",
            "-d",database,"-v","ON_ERROR_STOP=1"],capture=True,input=("SET lock_timeout='5s'; SET statement_timeout='120s';\n"+body).encode(),timeout=180)
        return data.decode().strip()
    def schema(self,database):
        return self.sql(database,"SELECT version_num FROM public.alembic_version")
    def owner(self,database):
        data=json.loads(self.sql(database,"SELECT json_build_object('name',r.rolname,'safe',NOT r.rolsuper AND NOT r.rolbypassrls)"
            " FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE c.oid='bonuspro.order_contexts'::regclass"))
        if data.get("safe") is not True:raise MarketplaceFailed("Migration owner must not bypass row security")
        identifier(data["name"]);return data["name"]
    def schema_hash(self,database):
        identifier(database)
        raw=checked(["docker","exec",self.container,"pg_dump","-U","postgres","-d",database,
            "--schema-only","--schema=bonuspro","--schema=public"],capture=True,timeout=120)
        return sha256(canonical_schema(raw)).hexdigest()
    @contextmanager
    def temporary(self,owner):
        name="bonuspro_marketplace_check_"+str(os.getpid())+"_"+uuid4().hex[:8]
        self.sql("postgres","CREATE DATABASE "+identifier(name)+" OWNER "+identifier(owner))
        try:
            # Match the existing bootstrap's explicit migration grant.
            self.sql(name,"GRANT USAGE,CREATE ON SCHEMA public TO "+identifier(owner))
            yield name
        finally:
            self.sql("postgres","DROP DATABASE "+identifier(name)+" WITH (FORCE)")
    def dump(self,database,path):
        identifier(database)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"wb") as target:
                checked(["docker","exec",self.container,"pg_dump","-U","postgres","-d",database,"-Fc"],
                        stdout=target,timeout=900)
                target.flush();os.fsync(target.fileno())
            if not path.stat().st_size:raise MarketplaceFailed("Protected database dump is empty")
        except Exception:
            path.unlink(missing_ok=True);raise
    def restore(self,name,path):
        identifier(name)
        with path.open("rb") as source:
            checked(["docker","exec","-i",self.container,"pg_restore","-U","postgres","-d",name,"--exit-on-error"],
                    stdin=source,timeout=900)
    def manifest(self,database):
        rows=json.loads(self.sql(database,"SELECT COALESCE(json_agg(json_build_object('table',c.relname,'column',a.attname)"
            " ORDER BY c.relname,a.attnum),'[]'::json) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='bonuspro' AND c.relkind='r'"
            " AND a.attnum>0 AND NOT a.attisdropped"))
        result={}
        for row in rows:
            identifier(row["table"]);identifier(row["column"])
            result.setdefault(row["table"],[]).append(row["column"])
        if not result:raise MarketplaceFailed("Financial column manifest is empty")
        return result
    def data_hashes(self,database,manifest):
        result={}
        for table,columns in sorted(manifest.items()):
            selected=",".join(identifier(column) for column in columns)
            body=FIXED_SETTINGS+" SET statement_timeout='120s'; COPY (SELECT to_jsonb(r)::text FROM (SELECT "+selected+" FROM bonuspro."+identifier(table)+") r"
            body+=" ORDER BY to_jsonb(r)::text COLLATE \"C\") TO STDOUT;"
            # Original columns only; new nullable undo columns are not old data.
            digest=sha256();count=0
            process=subprocess.Popen(["docker","exec","-i",self.container,"psql","-XqAt","-U","postgres",
                "-d",database,"-v","ON_ERROR_STOP=1"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                process.stdin.write(body.encode());process.stdin.close()
                for line in process.stdout:
                    digest.update(line);count+=1
                error=process.stderr.read(4096)
                if process.wait(timeout=900) or error:
                    raise MarketplaceFailed("Protected history fingerprint could not be verified")
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=10)
            result[table]={"rows":count,"sha256":digest.hexdigest()}
        return result
    def balanced(self,database):
        self.sql(database,"DO $$ BEGIN IF EXISTS(SELECT 1 FROM bonuspro.ledger_transactions t"
            " LEFT JOIN bonuspro.ledger_postings p ON p.tenant_id=t.tenant_id AND p.transaction_id=t.id"
            " GROUP BY t.tenant_id,t.id,t.delta HAVING count(p.account)<>2"
            " OR sum(p.amount) FILTER(WHERE p.account='customer') IS DISTINCT FROM t.delta::numeric"
            " OR sum(p.amount) FILTER(WHERE p.account='system') IS DISTINCT FROM (-t.delta)::numeric)"
            " THEN RAISE EXCEPTION 'Unbalanced journal'; END IF; END $$;")

def owner_sql(owner,body):
    return "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s'; SET LOCAL ROLE "+identifier(owner)+";\n"+body+"\nCOMMIT;"

def legacy_baseline(db,database,source,owner,migrate):
    migrate(database,"0013")
    body="\n".join((source/"migrations/versions"/name).read_text() for name in
                  ("0016_order_rebinding.sql","0017_personal_discounts.sql"))
    body+="\nUPDATE public.alembic_version SET version_num='0015' WHERE version_num='0013';"
    db.sql(database,owner_sql(owner,body))
    if db.schema(database)!="0015":raise MarketplaceFailed("Legacy baseline was not constructed")

def bridge_compatible(db,database,source,owner,legacy_hash):
    if db.schema(database)!="0015" or db.schema_hash(database)!=legacy_hash:
        raise MarketplaceFailed("Database is not the proven compatible schema 0015")
    sql="BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s';\n"
    sql+="LOCK TABLE public.alembic_version IN EXCLUSIVE MODE;\n"
    sql+="DO $$ BEGIN IF (SELECT count(*) FROM public.alembic_version)<>1 OR (SELECT version_num FROM public.alembic_version)<>'0015'"
    sql+=" THEN RAISE EXCEPTION 'Schema changed'; END IF; END $$;\nSET LOCAL ROLE "+identifier(owner)+";\n"
    sql+="\n".join((source/"migrations/versions"/name).read_text() for name in
                  ("0014_event_sync.sql","0015_worker_directory.sql"))
    sql+="\nRESET ROLE;\nDO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM information_schema.columns"
    sql+=" WHERE table_schema='bonuspro' AND table_name='jobs' AND column_name='hint_version')"
    sql+=" OR (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
    sql+=" WHERE n.nspname='bonuspro' AND c.relname IN ('sync_streams','related_documents','webhook_setup','webhook_inbox')"
    sql+=" AND c.relrowsecurity AND c.relforcerowsecurity)<>4"
    sql+=" OR to_regclass('bonuspro.worker_accounts') IS NULL"
    sql+=" OR to_regprocedure('bonuspro.route_worker_account()') IS NULL"
    sql+=" OR EXISTS((SELECT tenant_id FROM bonuspro.worker_accounts EXCEPT SELECT tenant_id FROM bonuspro.installations WHERE state='Activated')"
    sql+=" UNION ALL (SELECT tenant_id FROM bonuspro.installations WHERE state='Activated' EXCEPT SELECT tenant_id FROM bonuspro.worker_accounts))"
    sql+=" THEN RAISE EXCEPTION 'Main bridge shape differs'; END IF; END $$;\n"
    sql+="UPDATE public.alembic_version SET version_num='0017' WHERE version_num='0015'; COMMIT;"
    db.sql(database,sql)
    if db.schema(database)!="0017":raise MarketplaceFailed("Bridge revision was not confirmed")

@contextmanager
def expected_schemas(db,source,owner,migrate,head):
    with db.temporary(owner) as legacy,db.temporary(owner) as main:
        legacy_baseline(db,legacy,source,owner,migrate)
        result={"0015":db.schema_hash(legacy)}
        migrate(main,"0017");result["0017"]=db.schema_hash(main)
        if head!="0017":migrate(main,"head")
        result[head]=db.schema_hash(main)
        yield result

def advance(db,database,source,owner,migrate,schemas,head):
    original=db.schema(database)
    if original not in schemas or db.schema_hash(database)!=schemas[original]:
        raise MarketplaceFailed("Unrecognized database shape; no migration was attempted")
    manifest=db.manifest(database);before=db.data_hashes(database,manifest);db.balanced(database)
    if original=="0015":bridge_compatible(db,database,source,owner,schemas["0015"])
    migrate(database,"head")
    if db.schema(database)!=head or db.schema_hash(database)!=schemas[head]:
        raise MarketplaceFailed("Main schema was not confirmed; forward recovery is required")
    if db.data_hashes(database,manifest)!=before:
        raise MarketplaceFailed("Protected history changed; forward recovery is required")
    db.balanced(database)
    if head=="0018":
        count=db.sql(database,"SELECT (SELECT count(*) FROM bonuspro.presale_undo_requests)"
            "+(SELECT count(*) FROM bonuspro.presale_undo_completions)")
        if count!="0" and original!="0018":
            raise MarketplaceFailed("Upgrade unexpectedly created undo history")
    return {"original_schema":original,"schema":head,"protected_tables":len(manifest),
            "protected_rows":sum(row["rows"] for row in before.values())}

def verify_restore(db,dump,source,owner,migrate,schemas,head):
    with db.temporary(owner) as restored:
        db.restore(restored,dump)
        return advance(db,restored,source,owner,migrate,schemas,head)

def compose(root,source,env,*args):
    return ["docker","compose","--project-directory",str(root),"--env-file",str(env),"-p","bonuspro-release",
            "-f",str(source/"compose.release.yaml"),"-f",str(root/"compose.https.yaml"),*args]

def check_pool_config(config,image):
    services=config.get("services",{})
    for name in ("app","worker-pool"):
        if services.get(name,{}).get("image")!=image:raise MarketplaceFailed("Application and pool images must match")
    pool=services["worker-pool"]
    if pool.get("command")!=["python","-m","bonuspro.pool"]:
        raise MarketplaceFailed("Managed pool command differs")
    health=pool.get("healthcheck",{})
    if health.get("test")!=["CMD","python","-m","bonuspro.pool","--health"] or health.get("start_period") not in ("600s","10m","10m0s"):
        raise MarketplaceFailed("Managed pool startup health allowance is required")

def managed_services():
    names=checked(["docker","ps","-a","--filter","label=com.docker.compose.project=bonuspro-release",
                   "--format","{{.Names}}"],capture=True).decode().splitlines()
    result={}
    for name in names:
        data=json.loads(checked(["docker","inspect",name],capture=True))[0]
        labels=data.get("Config",{}).get("Labels",{})
        service=labels.get("com.docker.compose.service")
        if service=="db":continue
        if service not in SERVICES or name!="bonuspro-release-"+service+"-1":
            if data.get("State",{}).get("Running"):raise MarketplaceFailed("Another release operation is running")
            continue
        result[service]={"name":name,"running":bool(data["State"]["Running"])}
    if not all(name in result for name in ("app","web")):
        raise MarketplaceFailed("Existing managed application and HTTPS gateway are required")
    return result

def stop_services(states):
    for service in SERVICES:
        if states.get(service,{}).get("running"):
            checked(["docker","stop","--timeout","120",states[service]["name"]],timeout=180)
    if any(item["running"] for item in managed_services().values()):
        raise MarketplaceFailed("Managed services did not stop")

def backup_config(root):
    base=root.parent/"bonuspro-backups"
    if base.is_symlink():raise MarketplaceFailed("Backup directory must be regular")
    base.mkdir(mode=0o700,exist_ok=True)
    target=base/("marketplace-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid()))
    target.mkdir(mode=0o700)
    fd=os.open(target/"config-and-keys.tar.gz",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as output:
        checked(["tar","--dereference","-czf","-","-C",str(root),".env.release","compose.release.yaml",
                 "compose.https.yaml","deploy/secrets"],stdout=output)
        output.flush();os.fsync(output.fileno())
    return target

def restore_old_services(states):
    # Allowed only before any main schema change and before new traffic.
    for service in ("app","worker","worker-pool","web"):
        if states.get(service,{}).get("running"):checked(["docker","start",states[service]["name"]])

def pool_report():
    code=("import json;from datetime import datetime,timezone;from bonuspro.settings import Settings;"
          "from bonuspro.adapters.postgres.ledger import Ledger;from bonuspro.adapters.postgres.readiness import Readiness;"
          "import psycopg;"
          "l=Ledger(Settings.from_env().database_url);"
          "c=psycopg.connect(l.dsn,connect_timeout=5);"
          "rows=c.execute('SELECT tenant_id FROM bonuspro.worker_accounts ORDER BY tenant_id').fetchall();"
          "print(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),"
          "'accounts':{str(r[0]):Readiness(l).report(str(r[0])) for r in rows}}));c.close()")
    return json.loads(checked(["docker","exec","bonuspro-release-app-1","python","-c",code],capture=True,timeout=60))

def pool_confirmed(report,baseline):
    try:
        since=datetime.fromisoformat(baseline)
        return bool(report["accounts"]) and all(row["ready"] is True and row["last_processing_at"]
            and datetime.fromisoformat(row["last_processing_at"])>since for row in report["accounts"].values())
    except (KeyError,TypeError,ValueError):return False

def start_pool(command,image,*,wait_seconds=900):
    baseline=pool_report()["checked_at"]
    checked(command+["--profile","marketplace","up","-d","--no-build","--no-deps","--force-recreate",
                     "--wait","--wait-timeout","720","worker-pool"],timeout=900)
    deadline=time.monotonic()+wait_seconds
    report=pool_report()
    while report.get("accounts") and not pool_confirmed(report,baseline) and time.monotonic()<deadline:
        time.sleep(2);report=pool_report()
    state=checked(["docker","inspect","bonuspro-release-worker-pool-1","--format",
        "{{.Config.Image}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"],capture=True).decode().strip()
    complete=state==image+"|true|healthy" and pool_confirmed(report,baseline)
    print(json.dumps({"read_only":True,"business_acceptance_verified":False,"pool_ready":complete,
                      "active_accounts":len(report.get("accounts",{}))},ensure_ascii=False))
    if not complete:
        raise MarketplaceFailed("MARKETPLACE_PROCESSING_PENDING: pool was left running; complete account setup or inspect processing status")
    print("MARKETPLACE_PROCESSING_READY: fresh processing confirmed for the current active accounts")


def managed_config(root,current,tracked,source,original_schema):
    if current==tracked or current==(source/"compose.release.yaml").read_text():
        return True
    path=root/".marketplace-deployment.json"
    if path.is_symlink() or not path.is_file() or original_schema not in ("0017","0018"):
        return False
    mode=path.stat()
    if mode.st_uid!=0 or mode.st_mode&0o077:return False
    try:
        proof=json.loads(path.read_text())
        return (proof.get("compose_sha256")==sha256(current.encode()).hexdigest()
            and proof.get("schema") in ("0017","0018")
            and re.fullmatch(r"[0-9a-f]{40}",proof.get("source_revision","")) is not None
            and proof.get("image")=="bonuspro:"+proof["source_revision"][:12])
    except (TypeError,ValueError,OSError):return False

def record_configuration(root,source,revision,image,schema,backup):
    path=root/".marketplace-deployment.json"
    if path.is_symlink():raise MarketplaceFailed("Deployment record must be regular")
    body=json.dumps({"source_revision":revision,"image":image,"schema":schema,
        "compose_sha256":sha256((source/"compose.release.yaml").read_bytes()).hexdigest(),
        "backup":str(backup),"business_acceptance_verified":False},indent=2)+"\n"
    if path.exists():atomic_write(path,body,path.stat())
    else:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as output:output.write(body);output.flush();os.fsync(output.fileno())

@contextmanager
def deployment_lock(root):
    path=root/".marketplace-deployment.lock"
    try:
        descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    except OSError:
        raise MarketplaceFailed("Deployment lock must be a protected regular file") from None
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise MarketplaceFailed("Deployment lock must be owned by the operator and private")
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise MarketplaceFailed("Another marketplace deployment is running; wait for its result") from None
        try:yield
        finally:fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally:os.close(descriptor)

def finish(root,source,*,prepare_only=False):
    # Serialize before capturing old configuration or consulting live schema.
    with deployment_lock(root):
        return finish_locked(root,source,prepare_only=prepare_only)

def finish_locked(root,source,*,prepare_only=False):
    head=verify_source(source);revision=source_revision(source);image="bonuspro:"+revision[:12]
    db=Database();original=db.schema("bonuspro")
    if original not in ("0015","0017","0018"):raise MarketplaceFailed("Unsupported source schema; no migration was attempted")
    owner=db.owner("bonuspro")
    env=root/".env.release";config=root/"compose.release.yaml"
    for path in (env,config,root/"compose.https.yaml"):
        if path.is_symlink() or not path.is_file():raise MarketplaceFailed("Release configuration must be regular")
    old_env=env.read_text();old_config=config.read_text()
    tracked=checked(["git","-c","safe.directory="+str(root),"-C",str(root),"show","HEAD:compose.release.yaml"],capture=True).decode()
    if not managed_config(root,old_config,tracked,source,original):
        raise MarketplaceFailed("Existing Compose file has unrecognized local changes; inspect before replacing")
    states=managed_services()
    checked(["docker","build","--pull","-t",image,str(source)])
    backup=backup_config(root);target_env=backup/"target.env"
    fd=os.open(target_env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as output:output.write(replacement(old_env,image))
    command=compose(root,source,target_env)
    parsed=json.loads(checked(command+["--profile","marketplace","config","--format","json"],capture=True))
    check_pool_config(parsed,image)
    def migrate(database,target):
        identifier(database)
        code=("import os;from sqlalchemy.engine import make_url;from alembic import command;"
              "from alembic.config import Config;from bonuspro.settings import secret;"
              "u=make_url(secret('DATABASE_MIGRATION_URL')).set(drivername='postgresql+psycopg',database="+repr(database)+").update_query_dict({'options':'-c lock_timeout=5s -c statement_timeout=120s'});"
              "os.environ['DATABASE_MIGRATION_URL']=u.render_as_string(hide_password=False);"
              "command.upgrade(Config('alembic.ini'),"+repr(target)+")")
        checked(command+["--profile","maintenance","run","--rm","--no-deps","migrate","python","-c",code])
    stopped=False;migration_started=False;replacement_started=False;exposed=False;live_shape=None
    try:
        with expected_schemas(db,source,owner,migrate,head) as schemas:
            db.dump("bonuspro",backup/"database.dump")
            verify_restore(db,backup/"database.dump",source,owner,migrate,schemas,head)
            print("MARKETPLACE_RESTORE_VERIFIED: backup, exact schema bridge and protected history checked",flush=True)
            stopped=True;stop_services(states)
            # The final quiet checkpoint includes work completed during preflight.
            db.dump("bonuspro",backup/"database.pre-upgrade.dump")
            verify_restore(db,backup/"database.pre-upgrade.dump",source,owner,migrate,schemas,head)
            live_shape=db.schema_hash("bonuspro");migration_started=True
            proof=advance(db,"bonuspro",source,owner,migrate,schemas,head)
            # From this boundary a same-schema redeploy is also forward-only:
            # managed app names may now refer to the replacement immutable image.
            replacement_started=True
            atomic_write(env,replacement(old_env,image),env.stat())
            atomic_write(config,(source/"compose.release.yaml").read_text(),config.stat())
            record_configuration(root,source,revision,image,head,backup)
            actual=compose(root,root,env)
            checked(actual+["up","-d","--no-build","--no-deps","--force-recreate","--wait","--wait-timeout","120","app"])
            verify_local(source)
            exposed=True
            if states.get("web",{}).get("running"):checked(["docker","start",states["web"]["name"]])
            verify_public(source)
            print(json.dumps({**proof,"read_only":False,"business_acceptance_verified":False},ensure_ascii=False))
            if prepare_only:
                checked(actual+["--profile","marketplace","up","-d","--no-build","--no-deps",
                    "--force-recreate","--wait","--wait-timeout","720","worker-pool"],timeout=900)
                print("MARKETPLACE_APPLICATION_UPDATED_SETUP_PENDING: complete account settings; financial acceptance remains pending")
                return
            start_pool(actual,image)
    except (Exception,KeyboardInterrupt):
        unchanged=not migration_started
        observed=None
        if migration_started:
            try:
                observed=db.schema("bonuspro")
                unchanged=(not replacement_started and not exposed and observed==original and db.schema_hash("bonuspro")==live_shape)
            except Exception:unchanged=False
        if unchanged:
            if env.read_text()!=old_env:atomic_write(env,old_env,env.stat())
            if config.read_text()!=old_config:atomic_write(config,old_config,config.stat())
            if stopped:restore_old_services(states)
            raise MarketplaceFailed("MARKETPLACE_PRECHECK_FAILED: original schema/configuration retained. Protected backup: "+str(backup)) from None
        # No old application may be automatically restarted after a main schema
        # change, app/config replacement or possible financial exposure.
        atomic_write(env,replacement(old_env,image),env.stat())
        atomic_write(config,(source/"compose.release.yaml").read_text(),config.stat())
        record_configuration(root,source,revision,image,observed if observed in ("0017","0018") else head,backup)
        raise MarketplaceFailed("MARKETPLACE_NOT_CONFIRMED: main schema/image configuration retained; inspect services. Protected backup: "+str(backup)) from None

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"))
    parser.add_argument("--prepare-only",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run finish_marketplace.py with sudo")
    os.umask(0o077)
    try:finish(args.root.resolve(),Path(__file__).resolve().parents[1],prepare_only=args.prepare_only)
    except MarketplaceFailed as error:raise SystemExit(str(error)) from None
    except Exception:raise SystemExit("MARKETPLACE_NOT_CONFIRMED: no successful deployment was claimed") from None

if __name__=="__main__":main()
