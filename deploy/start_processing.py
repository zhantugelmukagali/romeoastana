"""Start the existing schema-0016 tenant worker without changing keys or financial setup."""
import argparse,json,os,re,selectors,subprocess,time
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID
from update_cabinet import run,compose,atomic_write

class ProcessingFailed(RuntimeError):pass

def select_tenant(rows,requested=None):
    allowed={str(UUID(row["tenant_id"])) for row in rows if row["state"] in ("SettingsRequired","Activated")}
    if requested:
        selected=str(UUID(requested))
        if selected not in allowed:raise ProcessingFailed("Requested installation is not enabled")
        return selected
    if len(allowed)!=1:raise ProcessingFailed("Exactly one enabled installation is required; specify --tenant for the selected account")
    return next(iter(allowed))


INSTALLATIONS_SQL="""SELECT COALESCE(json_agg(json_build_object(
    'tenant_id',i.tenant_id,'state',i.state,
    'has_rules',EXISTS(SELECT 1 FROM bonuspro.rule_versions r WHERE r.tenant_id=i.tenant_id),
    'has_widget',EXISTS(SELECT 1 FROM bonuspro.widget_bindings b WHERE b.tenant_id=i.tenant_id),
    'has_cutoff',EXISTS(SELECT 1 FROM bonuspro.automation_config c WHERE c.tenant_id=i.tenant_id)
)),'[]'::json)::text FROM bonuspro.installations i"""

def installation_rows():
    # Local, read-only metadata only: no credentials, customers or documents.
    return json.loads(run(["docker","exec","bonuspro-release-db-1","psql","-X","-U","postgres",
                           "-d","bonuspro","-At","-v","ON_ERROR_STOP=1","-c",INSTALLATIONS_SQL],capture=True))

def require_setup(rows,tenant):
    row=next((row for row in rows if str(UUID(row["tenant_id"]))==tenant),{})
    missing=[name for name,ready in (
        ("rules",row.get("has_rules") is True),("widget",row.get("has_widget") is True),
        ("activation",row.get("state")=="Activated" and row.get("has_cutoff") is True)) if not ready]
    if missing:
        raise ProcessingFailed("PROCESSING_SETUP_REQUIRED: "+", ".join(missing)+"; no worker or launch boundary was changed")

def require_worker_handoff(rows,tenant,previous,replace_inactive_worker=False):
    if not previous:
        raise ProcessingFailed("Existing worker account is unknown; inspect before replacing")
    try:previous=str(UUID(previous))
    except (ValueError,TypeError,AttributeError):
        raise ProcessingFailed("Existing worker account is invalid; inspect before replacing") from None
    if previous==tenant:return None
    if not replace_inactive_worker:
        raise ProcessingFailed("Existing worker belongs to a different account; use --replace-inactive-worker only for an inactive previous installation")
    state=next((row["state"] for row in rows if str(UUID(row["tenant_id"]))==previous),None)
    if state not in ("Suspended","Uninstalled"):
        raise ProcessingFailed("Previous installation must be Suspended or Uninstalled; active or unknown accounts cannot be replaced")
    return previous

def handoff_sql(previous,tenant):
    previous,tenant=map(lambda value:str(UUID(value)),(previous,tenant))
    locks="".join("SELECT pg_advisory_xact_lock(hashtextextended('installation/"+value+"',0));\n"
                  for value in sorted({previous,tenant}))
    # These are the same locks used by lifecycle callbacks and worker writes.
    return ("BEGIN;\nSET LOCAL lock_timeout='5s';\nSET LOCAL statement_timeout='10s';\n"
            "SET LOCAL idle_in_transaction_session_timeout='300s';\n"+locks+
            "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM bonuspro.installations WHERE tenant_id='"+
            previous+"' AND state IN ('Suspended','Uninstalled')) THEN RAISE EXCEPTION 'Previous installation is not inactive'; END IF; "
            "IF NOT EXISTS(SELECT 1 FROM bonuspro.installations i WHERE i.tenant_id='"+tenant+
            "' AND i.state='Activated' AND EXISTS(SELECT 1 FROM bonuspro.rule_versions r WHERE r.tenant_id=i.tenant_id) "
            "AND EXISTS(SELECT 1 FROM bonuspro.widget_bindings b WHERE b.tenant_id=i.tenant_id) "
            "AND EXISTS(SELECT 1 FROM bonuspro.automation_config c WHERE c.tenant_id=i.tenant_id)) "
            "THEN RAISE EXCEPTION 'Selected installation setup is incomplete'; END IF; END $$;\n"
            "SELECT 'HANDOFF_LOCKED';\n")

@contextmanager
def locked_worker_handoff(previous,tenant):
    # Keep the inactive-state check valid while stopping the old process and
    # replacing its managed override. Release before the new worker takes locks.
    process=subprocess.Popen(["docker","exec","-i","bonuspro-release-db-1","psql","-X","-qAt",
                              "-U","postgres","-d","bonuspro","-v","ON_ERROR_STOP=1"],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        try:
            process.stdin.write(handoff_sql(previous,tenant).encode());process.stdin.flush()
        except OSError:
            raise ProcessingFailed("Worker handoff lock was not confirmed; no account switch was attempted") from None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            received=b"";deadline=time.monotonic()+15
            while b"HANDOFF_LOCKED\n" not in received:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not selector.select(remaining):
                    raise ProcessingFailed("Worker handoff lock was not confirmed; no account switch was attempted")
                chunk=os.read(process.stdout.fileno(),4096)
                if not chunk or len(received)+len(chunk)>4096:
                    raise ProcessingFailed("Worker handoff eligibility changed; no account switch was attempted")
                received+=chunk
        yield
        process.communicate(input=b"COMMIT;\n\\q\n",timeout=10)
        if process.returncode:
            raise ProcessingFailed("Worker handoff completion was not confirmed; inspect worker configuration")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

def check_image(config,current_image):
    worker=config.get("services",{}).get("worker")
    if not worker or worker.get("image")!=current_image:
        raise ProcessingFailed("Worker must use the same immutable image as the running application")
    if not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}",current_image):
        raise ProcessingFailed("Immutable application image required")

def processing_confirmed(report,started_after):
    """A previous container's heartbeat cannot confirm the newly started worker."""
    try:
        heartbeat=datetime.fromisoformat(report["last_processing_at"])
        baseline=datetime.fromisoformat(started_after)
        return bool(report["ready"] and heartbeat.tzinfo is not None and baseline.tzinfo is not None
                    and heartbeat>baseline)
    except (KeyError,TypeError,ValueError):
        return False

def wait_for_report(fetch,started_after,*,timeout=60,clock=time.monotonic,pause=time.sleep):
    deadline=clock()+timeout
    report=fetch()
    while not processing_confirmed(report,started_after):
        activated=any(x.get("id")=="activation" and x.get("complete") for x in report.get("checks",[]))
        remaining=deadline-clock()
        if not activated or remaining<=0:
            break
        pause(min(2,remaining))
        report=fetch()
    return report

def require_fresh_processing(report,started_after):
    if not processing_confirmed(report,started_after):
        print("PROCESSING_STARTED: worker is running; fresh successful processing is not yet confirmed.",flush=True)
        raise ProcessingFailed("PROCESSING_NOT_CONFIRMED: complete the outstanding setup or processing checks; the worker was left running")
    print("PROCESSING_READY: automatic order processing has reported a new successful cycle",flush=True)

def worker_override(tenant):
    tenant=str(UUID(tenant))
    return '# Managed by BonusPro start_processing.py\nservices:\n  worker:\n    environment:\n      BONUSPRO_TENANT_ID: "'+tenant+'"\n    healthcheck:\n      test: [CMD, python, -m, bonuspro.worker, --health]\n      interval: 15s\n      timeout: 5s\n      start_period: 600s\n      retries: 6\n'

def start_worker(command):
    # One bounded cycle can read discovery plus four 90-second document graphs.
    # Keep liveness tied to a completed cycle while allowing that documented work.
    run(command+["up","-d","--no-build","--no-deps","--wait","--wait-timeout","720","worker"],timeout=900)

def check_worker_override(path,tenant,previous_tenant=None):
    text=worker_override(tenant)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProcessingFailed("Worker override must be a regular file")
    if not path.exists():
        if previous_tenant:raise ProcessingFailed("Previous managed worker override is required for an account handoff")
        return None
    current=path.read_text()
    expected=worker_override(previous_tenant) if previous_tenant else text
    allowed=(expected,expected.replace("start_period: 600s","start_period: 45s"))
    if current not in allowed:
        raise ProcessingFailed("Existing worker override differs; inspect before replacing")
    return current

def backup_worker_override(path,current,purpose):
    backup=path.with_name(path.name+".before-"+purpose+"-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid()))
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as stream:
        stream.write(current);stream.flush();os.fsync(stream.fileno())
    return backup

def ensure_worker_override(path,tenant,previous_tenant=None):
    text=worker_override(tenant)
    current=check_worker_override(path,tenant,previous_tenant)
    backup=None
    if current is not None:
        if current==text:return None
        backup=backup_worker_override(path,current,"tenant" if previous_tenant else "health")
        atomic_write(path,text,path.stat())
    else:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
    return backup

def prepare_worker_override(root,rows,tenant,previous,existing,replace_inactive_worker=False):
    override=root/"compose.processing.yaml"
    handoff=require_worker_handoff(rows,tenant,previous,replace_inactive_worker) if existing else None
    # Validate before stopping any process; validate again before writing.
    check_worker_override(override,tenant,handoff)
    if handoff:
        with locked_worker_handoff(handoff,tenant):
            current=check_worker_override(override,tenant,handoff)
            backup=backup_worker_override(override,current,"tenant")
            run(["docker","stop","--timeout","120",existing],timeout=180)
            if check_worker_override(override,tenant,handoff)!=current:
                raise ProcessingFailed("Worker override changed during handoff; inspect before replacing")
            atomic_write(override,worker_override(tenant),override.stat())
    else:
        backup=ensure_worker_override(override,tenant)
    return override,backup

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"));parser.add_argument("--tenant");parser.add_argument("--replace-inactive-worker",action="store_true",help="Explicitly replace a managed worker only when its previous installation is inactive");args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run with sudo")
    os.umask(0o077);root=args.root.resolve()
    try:
        schema=run(["docker","exec","bonuspro-release-db-1","psql","-U","postgres","-d","bonuspro","-At","-c","SELECT version_num FROM public.alembic_version"],capture=True)
        if schema!="0016":raise ProcessingFailed("This helper requires schema 0016; no migration was attempted")
        rows=installation_rows()
        tenant=select_tenant(rows,args.tenant)
        require_setup(rows,tenant)
        if run(["docker","inspect","bonuspro-release-app-1","--format","{{.State.Health.Status}}"],capture=True)!="healthy":
            raise ProcessingFailed("Application health must be confirmed before worker startup")
        image=run(["docker","inspect","bonuspro-release-app-1","--format","{{.Config.Image}}"],capture=True)
        config=json.loads(run(compose(root,"--profile","automation","config","--format","json"),capture=True))
        check_image(config,image)
        # An existing worker must not be silently reassigned to another account.
        existing=run(["docker","ps","-a","--filter","name=^/bonuspro-release-worker-1$","--format","{{.ID}}"],capture=True)
        previous=None
        if existing:
            env=json.loads(run(["docker","inspect",existing,"--format","{{json .Config.Env}}"],capture=True))
            previous=next((x.split("=",1)[1] for x in env if x.startswith("BONUSPRO_TENANT_ID=")),"")
        override,override_backup=prepare_worker_override(root,rows,tenant,previous,existing,args.replace_inactive_worker)
        if override_backup:print("Managed worker settings updated; protected backup: "+str(override_backup))
        command=compose(root,"-f",str(override),"--profile","automation")
        run(command+["config","--quiet"])
        code=("import json;from bonuspro.settings import Settings;from bonuspro.adapters.postgres.ledger import Ledger;"
              "from bonuspro.adapters.postgres.readiness import Readiness;"
              "print(json.dumps(Readiness(Ledger(Settings.from_env().database_url)).report("+repr(tenant)+")))")
        def report():
            return json.loads(run(["docker","exec","bonuspro-release-app-1","python","-c",code],capture=True))
        started_after=report()["checked_at"]
        start_worker(command)
        print("Waiting for a new processing cycle; existing orders are checked without changing program setup.",flush=True)
        result=wait_for_report(report,started_after)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        require_fresh_processing(result,started_after)
        print("No migrations, key regeneration or program activation were performed. Active programs can now process orders.")
    except ProcessingFailed as error:raise SystemExit(str(error))
    except Exception:raise SystemExit("PROCESSING_NOT_CONFIRMED: inspect app/worker health; no successful processing was claimed") from None

if __name__=="__main__":main()
