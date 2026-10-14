"""Decrypt and restore an encrypted checkpoint into a temporary database only."""
import argparse,hashlib,hmac,json,os,re,subprocess,tempfile
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4
ROOT=Path("/home/ubuntu/bonuspro-release")
DEST=Path("/home/ubuntu/bonuspro-backups/encrypted")
DB="bonuspro-release-db-1"

def command(args,**kwargs):
    return subprocess.run(args,check=True,timeout=1800,stderr=subprocess.DEVNULL,**kwargs)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("backup")
    parser.add_argument("--image",required=True)
    parser.add_argument("--expected-schema",required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run verify_backup_vps.py with sudo")
    if not re.fullmatch(r"bonuspro:[0-9a-f]{12,40}",args.image):raise SystemExit("Use an immutable tested image")
    if not re.fullmatch("[0-9]{4}",args.expected_schema):raise SystemExit("Explicit schema revision required")
    source=Path(args.backup)
    if source.is_symlink() or source.resolve().parent!=DEST.resolve() or not source.name.endswith(".pgdump.bpbk"):
        raise SystemExit("Use a checkpoint from the protected backup directory")
    os.umask(0o077)
    name="bonuspro_restore_check_"+uuid4().hex
    created=False
    try:
        checksum=source.with_suffix(source.suffix+".sha256")
        if checksum.is_symlink():raise ValueError()
        expected=checksum.read_text().split()[0];digest=hashlib.sha256()
        with source.open("rb") as value:
            for chunk in iter(lambda:value.read(1024*1024),b""):digest.update(chunk)
        if not hmac.compare_digest(expected,digest.hexdigest()):raise ValueError()
        with tempfile.TemporaryFile() as decrypted,source.open("rb") as encrypted:
            command(["docker","run","--rm","--network","none","--read-only","--log-driver","none",
                "--cap-drop","ALL","--security-opt","no-new-privileges:true",
                "--mount","type=bind,src="+str(ROOT/"deploy/secrets/token_encryption_key")+",dst=/run/backup-key,readonly",
                "-i",args.image,"python","-m","bonuspro.backup_stream","decrypt","--key-file","/run/backup-key"],
                stdin=encrypted,stdout=decrypted)
            # Do not restore streamed unauthenticated tail data. Decrypt succeeded first.
            decrypted.seek(0)
            command(["docker","exec",DB,"createdb","-U","postgres",name],stdout=subprocess.DEVNULL)
            created=True
            command(["docker","exec","-i",DB,"pg_restore","-U","postgres","-d",name,"--exit-on-error"],
                stdin=decrypted,stdout=subprocess.DEVNULL)
        sql="""SELECT version_num FROM public.alembic_version;
SELECT count(*) FROM (
 SELECT t.tenant_id,t.id
 FROM bonuspro.ledger_transactions t
 LEFT JOIN bonuspro.ledger_postings p ON p.tenant_id=t.tenant_id AND p.transaction_id=t.id
 GROUP BY t.tenant_id,t.id,t.delta
 HAVING count(p.transaction_id)<>2 OR sum(p.amount)<>0
 OR sum(p.amount) FILTER(WHERE p.account='customer') IS DISTINCT FROM t.delta::numeric
) invalid;
"""
        result=command(["docker","exec","-i",DB,"psql","-U","postgres","-d",name,"-At","-v","ON_ERROR_STOP=1"],
            input=sql.encode(),stdout=subprocess.PIPE).stdout.decode().splitlines()
        if result!=[args.expected_schema,"0"]:raise ValueError()
        marker=source.with_suffix(source.suffix+".verified.json")
        marker.write_text(json.dumps(dict(restored_at=datetime.now(timezone.utc).isoformat(),
            schema=args.expected_schema,sha256=digest.hexdigest(),offsite_verified=False))+"\n")
        print("ENCRYPTED_BACKUP_RESTORE_OK")
    except Exception:
        raise SystemExit("RESTORE_CHECK_FAILED: checkpoint has not been confirmed")
    finally:
        if created:
            try:command(["docker","exec",DB,"dropdb","-U","postgres",name],stdout=subprocess.DEVNULL)
            except Exception:print("Temporary restore database cleanup required")
if __name__=="__main__":main()
