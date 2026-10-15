"""Schema-0013 cabinet update with immutable image, static verification and rollback."""
import argparse,json,os,re,stat,subprocess
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen

class UpdateFailed(RuntimeError):pass

def run(args,*,capture=False):
    result=subprocess.run(args,check=True,timeout=600,stdout=subprocess.PIPE if capture else None,text=True)
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

def verify_public(source):
    base="https://loyalty.addwise.kz"
    with urlopen(base+"/health/ready",timeout=15) as response:
        if response.status!=200 or json.loads(response.read(1024)).get("status")!="ready":raise UpdateFailed("readiness failed")
    for name in ("portal.js","portal.css","shell.js","app.css","widget.js","widget.css","settings.js","settings.css","readiness.css","settings-tabs.js"):
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

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"));args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run update_cabinet.py with sudo")
    os.umask(0o077);root=args.root.resolve();source=Path(__file__).resolve().parents[1]
    try:
        schema=run(["docker","exec","bonuspro-release-db-1","psql","-U","postgres","-d","bonuspro","-At","-c","SELECT version_num FROM public.alembic_version"],capture=True)
        if schema!="0013" or 'revision != [("0013",)]' not in (source/"backend/bonuspro/api/app.py").read_text():
            raise UpdateFailed("Schema 0013 and its compatible source branch are required; no migration was attempted")
        run(["git","-c","safe.directory="+str(source),"-C",str(source),"diff","--exit-code","HEAD"])
        sha=run(["git","-C",str(source),"rev-parse","HEAD"],capture=True)
        if not re.fullmatch("[0-9a-f]{40}",sha):raise UpdateFailed("invalid source revision")
        image="bonuspro:"+sha[:12]
        run(["docker","build","--pull","-t",image,str(source)])
        run(["docker","run","--rm","--network","none","--read-only","--cap-drop","ALL",
             "--security-opt","no-new-privileges:true",image,"python","-c",
             "from bonuspro.adapters.postgres.portal import Portal; from pathlib import Path; assert Path('/app/web/assets/portal.js').read_bytes()"])
        switch(root,source,image)
        print("CABINET_READY: "+image+" · https://loyalty.addwise.kz/admin")
        print("No database migrations or secret changes were performed.")
    except UpdateFailed as error:raise SystemExit(str(error))
    except Exception:raise SystemExit("UPDATE_FAILED: no successful cabinet update was confirmed") from None

if __name__=="__main__":main()
