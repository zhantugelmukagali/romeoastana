"""Read-only VPS inspection. Does not migrate, restart containers or display secrets."""
import json,socket,subprocess
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

def command(args):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=15,check=False)
        return result.stdout.strip() if result.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):return None

def main():
    report={"read_only":True,"expected_schema":"0018","business_acceptance_verified":False}
    report["checkout"]=command(["git","rev-parse","HEAD"])
    report["tracked_changes"]=bool(command(["git","status","--porcelain","--untracked-files=no"]))
    for service in ("app","db","web","worker"):
        name="bonuspro-release-"+service+"-1"
        value=command(["docker","inspect","--format",
            '{{.Config.Image}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}',name])
        report[service]=value or "not inspected or absent"
    report["schema"]=command(["docker","exec","bonuspro-release-db-1","psql","-U","postgres","-d","bonuspro","-At",
                              "-c","SELECT version_num FROM public.alembic_version"])
    try:
        report["ipv4"]=sorted({r[4][0] for r in socket.getaddrinfo("loyalty.addwise.kz",443,socket.AF_INET)})
    except OSError:report["ipv4"]=[]
    try:
        with urlopen("https://loyalty.addwise.kz/health/ready",timeout=10) as response:
            report["https_ready"]=response.status==200 and json.loads(response.read(1024)).get("status")=="ready"
    except (OSError,ValueError,URLError):report["https_ready"]=False
    stat=Path("/proc/meminfo")
    if stat.exists():
        memory={row.split(":")[0]:row.split(":")[1].strip() for row in stat.read_text().splitlines()}
        report["memory"]={k:memory.get(k) for k in ("MemAvailable","SwapTotal","SwapFree")}
    report["schema_matches_checkout"]=report["schema"]=="0018"
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
