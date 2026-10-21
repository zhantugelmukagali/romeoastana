"""Root-only encrypted PostgreSQL checkpoint. No rotation until offsite policy exists."""
import argparse,hashlib,json,os,re,subprocess,time
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path("/home/ubuntu/bonuspro-release")
DEST=Path("/home/ubuntu/bonuspro-backups/encrypted")

def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image")
    group.add_argument("--image-from-env",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run backup_vps.py with sudo")
    if args.image_from_env:
        values=[line.split("=",1)[1] for line in (ROOT/".env.release").read_text().splitlines() if line.startswith("BONUSPRO_IMAGE=")]
        if len(values)!=1:raise SystemExit("Exactly one image tag required in release environment")
        args.image=values[0]
    if not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}",args.image):raise SystemExit("Use an immutable tested image tag")
    os.umask(0o077)
    key=ROOT/"deploy/secrets/token_encryption_key"
    if key.is_symlink() or not key.is_file():raise SystemExit("Backup key file is unavailable")
    if DEST.is_symlink():raise SystemExit("Backup destination must not be a symlink")
    DEST.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(DEST,0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid())
    partial=DEST/(stamp+".partial");final=DEST/(stamp+".pgdump.bpbk")
    dump=None;encryption=None
    try:
        with partial.open("xb") as target:
            dump=subprocess.Popen(["docker","exec","bonuspro-release-db-1","pg_dump","-U","postgres","-d","bonuspro","-Fc"],
                                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            encryption=subprocess.Popen(["docker","run","--rm","--network","none","--read-only","--log-driver","none",
                "--cap-drop","ALL","--security-opt","no-new-privileges:true",
                "--mount","type=bind,src="+str(key)+",dst=/run/backup-key,readonly",
                "-i",args.image,"python","-m","bonuspro.backup_stream","encrypt","--key-file","/run/backup-key"],
                stdin=dump.stdout,stdout=target,stderr=subprocess.DEVNULL)
            dump.stdout.close()
            encryption_code=encryption.wait(timeout=1800)
            dump_code=dump.wait(timeout=30)
            if encryption_code or dump_code or target.tell()<68:raise RuntimeError("backup pipeline failed")
            target.flush();os.fsync(target.fileno())
        digest=hashlib.sha256()
        with partial.open("rb") as source:
            for chunk in iter(lambda:source.read(1024*1024),b""):digest.update(chunk)
        os.replace(partial,final)
        checksum=final.with_suffix(final.suffix+".sha256")
        checksum.write_text(digest.hexdigest()+"  "+final.name+"\n")
        with checksum.open("rb") as source:os.fsync(source.fileno())
        directory=os.open(DEST,os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        print(json.dumps(dict(status="encrypted_backup_created",file=str(final),sha256=digest.hexdigest(),
            offsite_verified=False,restoration_verified=False)))
    except Exception:
        # Commands/exception bodies can contain deployment details; emit a safe fixed code.
        raise SystemExit("BACKUP_FAILED: no successful backup was confirmed")
    finally:
        for process in (encryption,dump):
            if process and process.poll() is None:process.kill();process.wait()
        partial.unlink(missing_ok=True)
if __name__=="__main__":main()
