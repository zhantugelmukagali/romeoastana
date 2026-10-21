"""Operational handoff fences use the same installation locks as lifecycle writes."""
import os,sys,time,unittest
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from pathlib import Path
from threading import Event
from unittest.mock import Mock
from uuid import uuid4
import psycopg
import test_imports as fixture
from bonuspro.adapters.postgres.activation import ActivationService
from bonuspro.adapters.postgres.checkout import Checkout
from bonuspro.adapters.postgres.redemptions import Redemptions
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"deploy"))
from start_processing import handoff_sql

class ProcessingHandoffTests(unittest.TestCase):
    def setUp(self):
        self.old=fixture.ImportTests();self.old.setUp()
        self.new=fixture.ImportTests();self.new.setUp()
        old,new=self.old,self.new
        batch=old.preview(37);old.commit(batch)
        old.install.change(old.tenant,self.claims(),"Suspend")
        Checkout(new.ledger).save_rules(new.tenant,earn_bps=500,spend_bps=3000,delivery_policy="exclude",
            expected_version=0,request_id=str(uuid4()),actor_id=new.actor,reason="New account")
        Redemptions(new.ledger).bind(new.tenant,str(uuid4()),new.actor)
        ActivationService(new.install).activate(new.tenant,str(uuid4()),new.actor,"Approved new account",Mock())

    def claims(self):
        self.event_iat=max(int(time.time()),getattr(self,"event_iat",0)+1)
        return dict(iat=self.event_iat,exp=self.event_iat+60,jti=str(uuid4()))

    def test_inactive_handoff_sql_preserves_both_accounts_financial_history_and_cutoffs(self):
        old,new=self.old,self.new
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"],autocommit=True) as conn:
            before=conn.execute("SELECT tenant_id,state FROM bonuspro.installations WHERE tenant_id IN (%s,%s) ORDER BY tenant_id",
                                (old.tenant,new.tenant)).fetchall()
            cutoff=conn.execute("SELECT starts_at FROM bonuspro.automation_config WHERE tenant_id=%s",(new.tenant,)).fetchone()
            conn.execute(handoff_sql(old.tenant,new.tenant))
            conn.execute("COMMIT")
            self.assertEqual(before,conn.execute("SELECT tenant_id,state FROM bonuspro.installations WHERE tenant_id IN (%s,%s) ORDER BY tenant_id",
                                                (old.tenant,new.tenant)).fetchall())
            self.assertEqual(cutoff,conn.execute("SELECT starts_at FROM bonuspro.automation_config WHERE tenant_id=%s",(new.tenant,)).fetchone())
        self.assertEqual(old.count("ledger_transactions"),1)
        self.assertEqual(old.count("opening_balances"),1)
        self.assertEqual(new.count("ledger_transactions"),0)
        self.assertEqual(new.count("opening_balances"),0)
        self.assertEqual(new.count("automation_config"),1)

    def test_resume_after_initial_check_is_rejected_before_handoff(self):
        self.old.install.change(self.old.tenant,self.claims(),"Resume","synthetic-new-token")
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"],autocommit=True) as conn:
            with self.assertRaises(psycopg.Error):conn.execute(handoff_sql(self.old.tenant,self.new.tenant))
            conn.execute("ROLLBACK")

    def test_handoff_holds_both_installation_locks_and_serializes_resume(self):
        old,new=self.old,self.new
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"],autocommit=True) as owner:
            owner.execute(handoff_sql(old.tenant,new.tenant))
            with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as observer:
                for tenant in (old.tenant,new.tenant):
                    self.assertFalse(observer.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                                                      ("installation/"+tenant,)).fetchone()[0])
            entered=Event()
            def resume():
                entered.set()
                return old.install.change(old.tenant,self.claims(),"Resume","synthetic-new-token")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(resume)
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(TimeoutError):future.result(timeout=0.2)
                finally:
                    owner.execute("COMMIT")
                self.assertEqual(future.result(timeout=3),"SettingsRequired")

    def test_incomplete_new_setup_and_missing_old_installation_are_rejected(self):
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"],autocommit=True) as conn:
            for previous,selected in ((str(uuid4()),self.new.tenant),(self.old.tenant,self.old.tenant)):
                with self.assertRaises(psycopg.Error):conn.execute(handoff_sql(previous,selected))
                conn.execute("ROLLBACK")

if __name__=="__main__":unittest.main()
