"""Start the existing schema-0016 tenant worker without changing keys or financial setup."""
import argparse,json,os,re,time
from datetime import datetime
from pathlib import Path
from uuid import UUID
from update_cabinet import run,compose

class ProcessingFailed(RuntimeError):pass

def select_tenant(rows,requested=None):
    allowed={str(UUID(row["tenant_id"])) for row in rows if row["state"] in ("SettingsRequired","Activated")}
    if requested:
        selected=str(UUID(requested))
        if selected not in allowed:raise ProcessingFailed("Requested installation is not enabled")
        return selected
    if len(allowed)!=1:raise ProcessingFailed("Exactly one enabled installation is required; specify --tenant for the selected account")
    return next(iter(allowed))

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

def worker_override(tenant):
    tenant=str(UUID(tenant))
    return '# Managed by BonusPro start_processing.py\nservices:\n  worker:\n    environment:\n      BONUSPRO_TENANT_ID: "'+tenant+'"\n    healthcheck:\n      test: [CMD, python, -m, bonuspro.worker, --health]\n      interval: 15s\n      timeout: 5s\n      start_period: 45s\n      retries: 6\n'

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"));parser.add_argument("--tenant");args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run with sudo")
    os.umask(0o077);root=args.root.resolve()
    try:
        schema=run(["docker","exec","bonuspro-release-db-1","psql","-U","postgres","-d","bonuspro","-At","-c","SELECT version_num FROM public.alembic_version"],capture=True)
        if schema!="0016":raise ProcessingFailed("This helper requires schema 0016; no migration was attempted")
        rows=json.loads(run(["docker","exec","bonuspro-release-db-1","psql","-U","postgres","-d","bonuspro","-At","-c",
            "SELECT COALESCE(json_agg(json_build_object('tenant_id',tenant_id,'state',state)),'[]'::json)::text FROM bonuspro.installations WHERE state IN ('SettingsRequired','Activated')"],capture=True))
        tenant=select_tenant(rows,args.tenant)
        if run(["docker","inspect","bonuspro-release-app-1","--format","{{.State.Health.Status}}"],capture=True)!="healthy":
            raise ProcessingFailed("Application health must be confirmed before worker startup")
        image=run(["docker","inspect","bonuspro-release-app-1","--format","{{.Config.Image}}"],capture=True)
        config=json.loads(run(compose(root,"--profile","automation","config","--format","json"),capture=True))
        check_image(config,image)
        # An existing worker must not be silently reassigned to another account.
        existing=run(["docker","ps","-a","--filter","name=^/bonuspro-release-worker-1$","--format","{{.ID}}"],capture=True)
        if existing:
            env=json.loads(run(["docker","inspect",existing,"--format","{{json .Config.Env}}"],capture=True))
            previous=next((x.split("=",1)[1] for x in env if x.startswith("BONUSPRO_TENANT_ID=")),"")
            if previous and previous!=tenant:raise ProcessingFailed("Existing worker belongs to a different account")
        override=root/"compose.processing.yaml"
        if override.is_symlink():raise ProcessingFailed("Worker override must be a regular file")
        text=worker_override(tenant)
        if override.exists() and override.read_text()!=text:raise ProcessingFailed("Existing worker override differs; inspect before replacing")
        if not override.exists():
            fd=os.open(override,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"w") as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        command=compose(root,"-f",str(override),"--profile","automation")
        run(command+["config","--quiet"])
        code=("import json;from bonuspro.settings import Settings;from bonuspro.adapters.postgres.ledger import Ledger;"
              "from bonuspro.adapters.postgres.readiness import Readiness;"
              "print(json.dumps(Readiness(Ledger(Settings.from_env().database_url)).report("+repr(tenant)+")))")
        def report():
            return json.loads(run(["docker","exec","bonuspro-release-app-1","python","-c",code],capture=True))
        started_after=report()["checked_at"]
        run(command+["up","-d","--no-build","--no-deps","--wait","--wait-timeout","180","worker"])
        print("Waiting for a new processing cycle; existing orders are checked without changing program setup.",flush=True)
        result=wait_for_report(report,started_after)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if processing_confirmed(result,started_after):print("PROCESSING_READY: automatic order processing has reported a new successful cycle")
        else:print("PROCESSING_STARTED: finish the incomplete steps in BonusPro settings; processing is not yet confirmed")
        print("No migrations, key regeneration or program activation were performed. Active programs can now process orders.")
    except ProcessingFailed as error:raise SystemExit(str(error))
    except Exception:raise SystemExit("PROCESSING_NOT_CONFIRMED: inspect app/worker health; no successful processing was claimed") from None

if __name__=="__main__":main()
