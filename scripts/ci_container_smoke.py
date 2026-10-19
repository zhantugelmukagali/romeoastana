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


def check_compatible_upgrade(command):
    """Exercise the production 0013 upgrade and rollback against disposable roles."""
    source=Path.cwd()
    if not (source/"migrations/versions/0014_order_rebinding.sql").is_file():
        return  # Main branch has a separate newer schema and deployment path.
    code=("import os;from alembic import command;from alembic.config import Config;"
          "from bonuspro.settings import secret;"
          "os.environ['DATABASE_MIGRATION_URL']=secret('DATABASE_MIGRATION_URL').replace('postgresql://','postgresql+psycopg://',1);"
          "command.upgrade(Config('alembic.ini'),'0013')")
    run(*command,"--profile","maintenance","run","--rm","migrate","python","-c",code)
    import sys
    sys.path.insert(0,str(source/"deploy"))
    import update_cabinet as update
    update.DB_CONTAINER="bonuspro-ci-container-db-1"
    tenant="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    wallet="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
    actor="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"
    update.database_sql("bonuspro","INSERT INTO bonuspro.tenants(id) VALUES ('"+tenant+"')")
    seed=("from bonuspro.settings import secret;from bonuspro.adapters.postgres.ledger import Ledger;"
          "l=Ledger(secret('DATABASE_URL'));l.create_wallet('"+tenant+"','"+wallet+"');"
          "l.credit('"+tenant+"','"+wallet+"',points=17,key='upgrade-restore',actor_id='"+actor+"',reason='Synthetic upgrade fixture')")
    run(*command,"run","--rm","--no-deps","app","python","-c",seed)
    before=update.financial_counts("bonuspro")
    assert before["transactions"]==1 and before["postings"]==2
    with tempfile.TemporaryDirectory() as directory:
        backup=Path(directory)
        update.dump_database(backup/"database.dump")
        update.verify_restore(backup,source)
    assert update.database_schema()=="0013"
    update.apply_compatible_migrations("bonuspro",source)
    assert update.database_schema()=="0015"
    update.rollback_compatible_migrations(source,"0013")
    assert update.database_schema()=="0013"
    assert update.financial_counts("bonuspro")==before
    update.apply_rebinding_migration("bonuspro",source)
    assert update.database_schema()=="0014"
    update.rollback_rebinding_migration()
    assert update.database_schema()=="0013"
    assert update.financial_counts("bonuspro")==before
    update.apply_rebinding_migration("bonuspro",source)
    # A FORCE RLS audit row belonging to another tenant must still prevent a
    # downgrade. Checking as the owner with no tenant would otherwise hide it.
    customer="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3"
    next_customer="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee4"
    next_wallet="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee5"
    order="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee6"
    request="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee7"
    update.database_sql("bonuspro",
        "BEGIN;SELECT set_config('bonuspro.tenant_id','"+tenant+"',true);"
        "INSERT INTO bonuspro.wallets(tenant_id,id) VALUES ('"+tenant+"','"+next_wallet+"');"
        "INSERT INTO bonuspro.rule_versions(tenant_id,version,earn_bps,spend_bps,delivery_policy,request_id,request_hash,actor_id,reason) "
        "VALUES ('"+tenant+"',1,500,3000,'exclude','"+request+"','"+("a"*64)+"','"+actor+"','Synthetic audit fixture');"
        "INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES "
        "('"+tenant+"','"+customer+"','"+wallet+"'),('"+tenant+"','"+next_customer+"','"+next_wallet+"');"
        "INSERT INTO bonuspro.order_contexts(tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by,binding_revision) "
        "VALUES ('"+tenant+"','"+order+"','"+customer+"','"+wallet+"',1,'"+("a"*64)+"','"+actor+"','2026-10-19 10:00:00.000');"
        "SET LOCAL ROLE bonuspro_runtime;"
        "SELECT bonuspro.rebind_order_customer('"+order+"','"+next_customer+"','"+("a"*64)+"','2026-10-19 10:00:01.000','"+actor+"');COMMIT;")
    try:
        update.rollback_rebinding_migration()
    except update.UpdateFailed:
        pass
    else:
        raise AssertionError("Rollback erased rebinding history hidden by FORCE RLS")
    assert update.database_schema()=="0014"
    assert update.database_sql("bonuspro","SELECT count(*) FROM bonuspro.order_customer_rebindings",capture=True)=="1"
    assert update.financial_counts("bonuspro")==before
    update.validate_restored_journal("bonuspro")
    # A schema-0014 customer may already have immutable rebinding history.
    # Its personal-discount upgrade/rollback must preserve that history.
    with tempfile.TemporaryDirectory() as directory:
        backup=Path(directory)
        update.dump_database(backup/"database.dump")
        update.verify_restore(backup,source)
    assert update.database_schema()=="0014"
    update.apply_personal_discount_migration("bonuspro",source)
    assert update.database_schema()=="0015"
    update.rollback_compatible_migrations(source,"0014")
    assert update.database_schema()=="0014"
    assert update.database_sql("bonuspro","SELECT count(*) FROM bonuspro.order_customer_rebindings",capture=True)=="1"
    update.apply_personal_discount_migration("bonuspro",source)
    update.database_sql("bonuspro",
        "BEGIN;SET LOCAL ROLE bonuspro_runtime;"
        "SELECT set_config('bonuspro.tenant_id','"+tenant+"',true);"
        "INSERT INTO bonuspro.personal_discount_policy_versions "
        "(tenant_id,version,enabled,external_policy,allow_bonus_stack,request_id,request_hash,actor_id,reason) "
        "VALUES ('"+tenant+"',1,true,'skip',false,'ffffffff-ffff-4fff-8fff-fffffffffff1','"+("f"*64)+"','"+actor+"','Synthetic personal policy');COMMIT;")
    try:
        update.rollback_personal_discount_migration(source)
    except update.UpdateFailed:
        pass
    else:
        raise AssertionError("Rollback erased personal-discount configuration hidden by FORCE RLS")
    assert update.database_schema()=="0015"
    assert update.database_sql("bonuspro","SELECT count(*) FROM bonuspro.personal_discount_policy_versions",capture=True)=="1"
    assert update.database_sql("bonuspro","SELECT count(*) FROM bonuspro.order_customer_rebindings",capture=True)=="1"
    assert update.financial_counts("bonuspro")==before
    update.validate_restored_journal("bonuspro")
    print("Compatible 0013/0014 upgrades through 0015: protected restore, exact migrations, empty rollback, runtime RLS and history rollback refusal passed.")

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
                      MOYSKLAD_APP_ID="11111111-1111-4111-8111-111111111111",MOYSKLAD_APP_UID="ci.test",
                      BONUSPRO_TENANT_ID="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    command=["docker","compose","-p","bonuspro-ci-container","-f","compose.release.yaml"]
    try:
        run(*command,"config","--quiet")
        run(*command,"up","-d","--wait","--wait-timeout","120","db")
        run(*command,"--profile","maintenance","run","--rm","bootstrap")
        check_compatible_upgrade(command)
        run(*command,"--profile","maintenance","run","--rm","migrate")
        run(*command,"--profile","maintenance","run","--rm","migrate")
        run(*command,"up","-d","--wait","--wait-timeout","120","app")
        for path in ("/health/ready","/admin","/assets/app.js"):
            request=urllib.request.Request("http://127.0.0.1:18080"+path,headers={"Host":"ci.bonuspro.test"})
            with urllib.request.urlopen(request,timeout=5) as response:
                assert response.status==200,path
                assert response.headers["X-Content-Type-Options"]=="nosniff"
        from ci_static_transport import check_static_transport
        check_static_transport()
        request=urllib.request.Request("http://127.0.0.1:18080/api/admin/returns",headers={"Host":"ci.bonuspro.test"})
        try:
            urllib.request.urlopen(request,timeout=5)
            raise AssertionError("Anonymous financial data access was allowed")
        except urllib.error.HTTPError as error:
            assert error.code==401
        run(*command,"exec","-T","app","python","-c",
            "import os; assert os.getuid()==10001; assert not os.access('/app',os.W_OK)")
        run(*command,"--profile","automation","run","--rm","worker","python","-m","bonuspro.worker","--once")
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
