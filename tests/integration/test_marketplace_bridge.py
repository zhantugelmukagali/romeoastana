"""Disposable PostgreSQL proof of compatible history, backup restore and the main bridge."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
import finish_marketplace as upgrade

@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS")=="true","Disposable GitHub PostgreSQL service only")
class MarketplaceBridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.admin=os.environ["TEST_ADMIN_DSN"]
        if conninfo_to_dict(self.admin).get("dbname")!="bonuspro_ci":
            raise RuntimeError("Bridge fixture requires the disposable bonuspro_ci database")
        result=subprocess.run(["docker","ps","--filter","ancestor=postgres:17.6-alpine","--format","{{.ID}}"],
                              check=True,capture_output=True,text=True,timeout=10)
        containers=result.stdout.splitlines()
        self.assertEqual(len(containers),1,"Expected the disposable PostgreSQL service")
        self.db=upgrade.Database(containers[0])
        self.owner="bonuspro_bridge_"+uuid4().hex[:12]
        self.password="ci-only-bridge"
        with psycopg.connect(self.admin,autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(
                sql.Identifier(self.owner),sql.Literal(self.password)))
        self.addCleanup(self.drop_owner)
        self.head=upgrade.verify_source(ROOT)

    def drop_owner(self):
        with psycopg.connect(self.admin,autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(self.owner)))

    def migrate(self,database,target):
        url=make_url(self.admin).set(drivername="postgresql+psycopg",username=self.owner,
                                      password=self.password,database=database)
        with patch.dict(os.environ,{"DATABASE_MIGRATION_URL":url.render_as_string(hide_password=False)}):
            command.upgrade(Config(str(ROOT/"alembic.ini")),target)

    def seed(self,database):
        tenant,actor,customer,next_customer,wallet,next_wallet,order,marker,generation,activation,application=(
            str(uuid4()) for _ in range(11))
        self.db.sql(database,'''
BEGIN;
SELECT set_config('bonuspro.tenant_id','''+"'"+tenant+"'"+''',true);
INSERT INTO bonuspro.tenants(id) VALUES ('''+"'"+tenant+"'"+''');
INSERT INTO bonuspro.wallets(tenant_id,id) VALUES
 ('''+"'"+tenant+"','"+wallet+"'"+'''),('''+"'"+tenant+"','"+next_wallet+"'"+''');
INSERT INTO bonuspro.installations(tenant_id,app_id,state,token_ciphertext,generation,last_event_iat,last_event_digest)
 VALUES ('''+"'"+tenant+"','"+str(uuid4())+"'"+''','Activated','synthetic-fixture','''+"'"+generation+"'"+''',1,repeat('a',64));
INSERT INTO bonuspro.rule_versions
 (tenant_id,version,earn_bps,spend_bps,delivery_policy,request_id,request_hash,actor_id,reason)
 VALUES ('''+"'"+tenant+"'"+''',1,500,3000,'exclude','''+"'"+str(uuid4())+"'"+''',repeat('b',64),'''+"'"+actor+"'"+''','Fixture rules');
INSERT INTO bonuspro.widget_bindings(tenant_id,marker_id,actor_id)
 VALUES ('''+"'"+tenant+"','"+marker+"','"+actor+"'"+''');
INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES
 ('''+"'"+tenant+"','"+customer+"','"+wallet+"'"+'''),('''+"'"+tenant+"','"+next_customer+"','"+next_wallet+"'"+''');
INSERT INTO bonuspro.order_contexts
 (tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by,binding_revision)
 VALUES ('''+"'"+tenant+"','"+order+"','"+customer+"','"+wallet+"'"+''',1,repeat('c',64),'''+"'"+actor+"'"+''','2026-10-19 10:00:00.000');
SELECT bonuspro.rebind_order_customer('''+"'"+order+"','"+next_customer+"'"+''',repeat('d',64),'2026-10-19 10:00:01.000','''+"'"+actor+"'"+''');
INSERT INTO bonuspro.ledger_transactions
 (tenant_id,id,wallet_id,operation_key,request_hash,kind,delta,actor_id,reason)
 VALUES ('''+"'"+tenant+"','"+application+"','"+next_wallet+"'"+''','fixture-credit',repeat('e',64),'credit',1000,'''+"'"+actor+"'"+''','Preserved credit');
INSERT INTO bonuspro.ledger_postings(tenant_id,transaction_id,wallet_id,account,amount) VALUES
 ('''+"'"+tenant+"','"+application+"','"+next_wallet+"'"+''','customer',1000),
 ('''+"'"+tenant+"','"+application+"','"+next_wallet+"'"+''','system',-1000);
INSERT INTO bonuspro.personal_discount_policy_versions
 (tenant_id,version,enabled,external_policy,allow_bonus_stack,request_id,request_hash,actor_id,reason)
 VALUES ('''+"'"+tenant+"'"+''',1,true,'skip',false,'''+"'"+str(uuid4())+"'"+''',repeat('f',64),'''+"'"+actor+"'"+''','Preserved policy');
INSERT INTO bonuspro.customer_personal_discount_versions
 (tenant_id,counterparty_id,version,rate_bps,request_id,request_hash,actor_id,reason)
 VALUES ('''+"'"+tenant+"','"+next_customer+"'"+''',1,1000,'''+"'"+str(uuid4())+"'"+''',repeat('a',64),'''+"'"+actor+"'"+''','Preserved assignment');
INSERT INTO bonuspro.personal_discount_applications
 (tenant_id,id,order_id,counterparty_id,wallet_id,request_id,request_hash,source_hash,actor_id,
  marker_id,marker,policy_version,assignment_version,rate_bps,points,plan)
 VALUES ('''+"'"+tenant+"','"+application+"','"+order+"','"+next_customer+"','"+next_wallet+"','"+str(uuid4())+"'"+''',
 repeat('b',64),repeat('c',64),'''+"'"+actor+"','"+marker+"'"+''','fixture-marker',1,1,1000,0,'{"before":{},"after":{}}');
UPDATE bonuspro.personal_discount_applications SET state='confirmed',proof_revision='2026-10-19 10:00:02.000'
 WHERE tenant_id='''+"'"+tenant+"'"+''' AND id='''+"'"+application+"'"+''';
INSERT INTO bonuspro.activation_requests(tenant_id,id,generation,actor_id,rule_version,marker_id,reason)
 VALUES ('''+"'"+tenant+"','"+activation+"','"+generation+"','"+actor+"'"+''',1,'''+"'"+marker+"'"+''','Preserved launch approval');
INSERT INTO bonuspro.activation_completions(tenant_id,request_id) VALUES ('''+"'"+tenant+"','"+activation+"'"+''');
INSERT INTO bonuspro.automation_config(tenant_id,starts_at,actor_id)
 VALUES ('''+"'"+tenant+"'"+''','2026-10-19T00:00:00Z','''+"'"+actor+"'"+''');
INSERT INTO bonuspro.sync_state(tenant_id,scan_until) VALUES ('''+"'"+tenant+"'"+''','2026-10-19T00:00:00Z');
COMMIT;
''')
        return tenant,next_wallet

    def test_nonempty_backup_restore_and_main_upgrade_preserve_all_original_rows_and_runtime_balance(self):
        with self.db.temporary(self.owner) as original:
            upgrade.legacy_baseline(self.db,original,ROOT,self.owner,self.migrate)
            tenant,wallet=self.seed(original)
            manifest=self.db.manifest(original);before=self.db.data_hashes(original,manifest)
            self.assertGreater(before["ledger_transactions"]["rows"],0)
            self.assertGreater(before["order_customer_rebindings"]["rows"],0)
            self.assertGreater(before["personal_discount_applications"]["rows"],0)
            with upgrade.expected_schemas(self.db,ROOT,self.owner,self.migrate,self.head) as schemas:
                self.assertEqual(self.db.schema_hash(original),schemas["0015"])
                with tempfile.TemporaryDirectory() as directory:
                    dump=Path(directory)/"database.dump"
                    self.db.dump(original,dump)
                    proof=upgrade.verify_restore(self.db,dump,ROOT,self.owner,self.migrate,schemas,self.head)
                    self.assertEqual(proof["schema"],self.head)
                proof=upgrade.advance(self.db,original,ROOT,self.owner,self.migrate,schemas,self.head)
                self.assertEqual(proof["schema"],self.head)
                self.assertEqual(self.db.data_hashes(original,manifest),before)
                self.assertEqual(self.db.sql(original,"SELECT count(*) FROM bonuspro.worker_accounts"),"1")
                self.assertEqual(self.db.sql(original,"SELECT starts_at::text FROM bonuspro.automation_config"),
                                 "2026-10-19 00:00:00+00")
                from bonuspro.adapters.postgres.ledger import Ledger
                app_url=make_url(os.environ["TEST_APP_DSN"]).set(database=original)
                ledger=Ledger(app_url.render_as_string(hide_password=False))
                self.assertEqual(ledger.snapshot(tenant,wallet)["balance"],1000)
                with ledger.transaction(str(uuid4())) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.ledger_transactions").fetchone()["n"],0)
                # Replay on the proven current main schema changes no history.
                upgrade.advance(self.db,original,ROOT,self.owner,self.migrate,schemas,self.head)
                self.assertEqual(self.db.data_hashes(original,manifest),before)

    def test_unknown_compatible_shape_is_rejected_before_event_tables_or_stamp_are_created(self):
        with self.db.temporary(self.owner) as original:
            upgrade.legacy_baseline(self.db,original,ROOT,self.owner,self.migrate)
            with upgrade.expected_schemas(self.db,ROOT,self.owner,self.migrate,self.head) as schemas:
                self.db.sql(original,"ALTER TABLE bonuspro.wallets ADD COLUMN unreviewed integer")
                with self.assertRaises(upgrade.MarketplaceFailed):
                    upgrade.bridge_compatible(self.db,original,ROOT,self.owner,schemas["0015"])
                self.assertEqual(self.db.schema(original),"0015")
                self.assertEqual(self.db.sql(original,"SELECT to_regclass('bonuspro.worker_accounts') IS NULL"),"t")
                self.assertEqual(self.db.sql(original,"SELECT to_regclass('bonuspro.webhook_inbox') IS NULL"),"t")

    def test_failed_next_migration_keeps_committed_bridge_and_financial_history_for_forward_recovery(self):
        if self.head=="0017":self.skipTest("Follow-up main migration is not present")
        with self.db.temporary(self.owner) as original:
            upgrade.legacy_baseline(self.db,original,ROOT,self.owner,self.migrate)
            self.seed(original);manifest=self.db.manifest(original);before=self.db.data_hashes(original,manifest)
            with upgrade.expected_schemas(self.db,ROOT,self.owner,self.migrate,self.head) as schemas:
                def unavailable(*args):raise RuntimeError("synthetic migration interruption")
                with self.assertRaises(RuntimeError):
                    upgrade.advance(self.db,original,ROOT,self.owner,unavailable,schemas,self.head)
                self.assertEqual(self.db.schema(original),"0017")
                self.assertEqual(self.db.data_hashes(original,manifest),before)
                upgrade.advance(self.db,original,ROOT,self.owner,self.migrate,schemas,self.head)
                self.assertEqual(self.db.schema(original),self.head)
                self.assertEqual(self.db.data_hashes(original,manifest),before)

if __name__=="__main__":unittest.main()
