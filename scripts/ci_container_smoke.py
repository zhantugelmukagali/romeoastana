"""Disposable CI compose smoke test, never a production deployment command."""
import base64
import os
from pathlib import Path
import subprocess
import urllib.request
import urllib.error
import tempfile

def run(*args):
    subprocess.run(args,check=True,timeout=240)

def main():
    if os.environ.get("GITHUB_ACTIONS")!="true":
        raise SystemExit("This destructive cleanup script is restricted to disposable GitHub runners.")
    root=Path("deploy/secrets")
    if root.exists():
        raise SystemExit("Unexpected existing secrets directory")
    root.mkdir(parents=True)
    values={"db_password":"ci-only-postgres","vendor_secret":"ci-only-vendor-"+"x"*40,
            "token_encryption_key":base64.b64encode(b"k"*32).decode()}
    for name,user,password in (("admin_database_url","postgres","ci-only-postgres"),
                               ("app_database_url","bonuspro_runtime","ci-only-app"),
                               ("lifecycle_database_url","bonuspro_installation","ci-only-lifecycle"),
                               ("migration_database_url","bonuspro_migrator","ci-only-migrations")):
        values[name]="postgresql://"+user+":"+password+"@db:5432/bonuspro"
    for name,value in values.items():
        path=root/name
        path.write_text(value)
        path.chmod(0o400)
        run("sudo","chown","10001:10001",str(path))
    os.environ.update(BONUSPRO_IMAGE="bonuspro:ci",PUBLIC_ORIGIN="https://ci.bonuspro.test",
                      MOYSKLAD_APP_ID="11111111-1111-4111-8111-111111111111",MOYSKLAD_APP_UID="ci.test")
    command=["docker","compose","-p","bonuspro-ci-container","-f","compose.release.yaml"]
    try:
        run(*command,"config","--quiet")
        run(*command,"up","-d","--wait","--wait-timeout","120","db")
        run(*command,"--profile","maintenance","run","--rm","bootstrap")
        run(*command,"--profile","maintenance","run","--rm","migrate")
        run(*command,"--profile","maintenance","run","--rm","migrate")
        run(*command,"up","-d","--wait","--wait-timeout","120","app")
        for path in ("/health/ready","/admin","/assets/app.js"):
            request=urllib.request.Request("http://127.0.0.1:18080"+path,headers={"Host":"ci.bonuspro.test"})
            with urllib.request.urlopen(request,timeout=5) as response:
                assert response.status==200,path
                assert response.headers["X-Content-Type-Options"]=="nosniff"
        request=urllib.request.Request("http://127.0.0.1:18080/api/admin/returns",headers={"Host":"ci.bonuspro.test"})
        try:
            urllib.request.urlopen(request,timeout=5)
            raise AssertionError("Anonymous financial data access was allowed")
        except urllib.error.HTTPError as error:
            assert error.code==401
        run(*command,"exec","-T","app","python","-c",
            "import os; assert os.getuid()==10001; assert not os.access('/app',os.W_OK)")
        # Restore a non-empty balanced journal into an independent database.
        tenant="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        wallet="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        actor="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        run(*command,"exec","-T","db","psql","-U","postgres","-d","bonuspro","-v","ON_ERROR_STOP=1","-c",
            "INSERT INTO bonuspro.tenants(id) VALUES ('"+tenant+"')")
        seed=("from bonuspro.settings import secret; from bonuspro.adapters.postgres.ledger import Ledger; "
              "l=Ledger(secret('DATABASE_URL')); l.create_wallet('"+tenant+"','"+wallet+"'); "
              "l.credit('"+tenant+"','"+wallet+"',points=1000,key='restore-test',actor_id='"+actor+"',reason='Synthetic restore fixture')")
        run(*command,"exec","-T","app","python","-c",seed)
        with tempfile.TemporaryFile() as archive:
            subprocess.run([*command,"exec","-T","db","pg_dump","-U","postgres","-d","bonuspro","-Fc"],
                           stdout=archive,check=True,timeout=60)
            assert archive.tell()>0
            run(*command,"exec","-T","db","createdb","-U","postgres","bonuspro_restore")
            archive.seek(0)
            subprocess.run([*command,"exec","-T","db","pg_restore","-U","postgres","-d","bonuspro_restore","--exit-on-error"],
                           stdin=archive,check=True,timeout=60)
        verify=("from bonuspro.settings import secret; from bonuspro.adapters.postgres.ledger import Ledger; "
                "dsn=secret('DATABASE_URL').rsplit('/',1)[0]+'/bonuspro_restore'; l=Ledger(dsn); "
                "assert l.snapshot('"+tenant+"','"+wallet+"')['balance']==1000; "
                "print('Backup restored: balanced journal and runtime RLS role verified')")
        run(*command,"exec","-T","app","python","-c",verify)
        print("Container, least-privilege bootstrap, repeat migration, readiness and anonymous access checks passed.")
    finally:
        run(*command,"logs","--tail","50")
        run(*command,"down","--volumes","--remove-orphans")

if __name__=="__main__":
    main()
