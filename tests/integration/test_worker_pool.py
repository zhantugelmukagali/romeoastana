import os,time,unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import psycopg
import test_redemptions as fixture
from bonuspro.pool import WorkerPool
from bonuspro.adapters.postgres.ledger import Conflict

class WorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.RedemptionTests();self.fx.setUp()
    def accounts(self):
        with psycopg.connect(os.environ["TEST_APP_DSN"]) as conn:
            return {str(row[0]) for row in conn.execute("SELECT tenant_id FROM bonuspro.worker_accounts").fetchall()}
    def test_activation_is_registered_and_suspend_removes_account(self):
        self.assertIn(self.fx.tenant,self.accounts())
        self.fx.install.change(self.fx.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        self.assertNotIn(self.fx.tenant,self.accounts())
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],5000)
    def test_resume_waits_for_admin_activation_before_routing(self):
        self.fx.install.change(self.fx.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Resume","synthetic")
        self.assertNotIn(self.fx.tenant,self.accounts())
        self.fx.activate()
        self.assertIn(self.fx.tenant,self.accounts())
    def test_runtime_can_read_only_uuid_directory_not_modify_it(self):
        with self.assertRaises(psycopg.Error):
            with psycopg.connect(os.environ["TEST_APP_DSN"]) as conn:
                conn.execute("DELETE FROM bonuspro.worker_accounts WHERE tenant_id=%s",(self.fx.tenant,))
        with self.assertRaises(psycopg.Error):
            with psycopg.connect(os.environ["TEST_APP_DSN"]) as conn:
                conn.execute("INSERT INTO bonuspro.worker_accounts(tenant_id) VALUES (%s)",(str(uuid4()),))
        with psycopg.connect(os.environ["TEST_APP_DSN"]) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bonuspro.installations").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT count(*) FROM bonuspro.ledger_transactions").fetchone()[0],0)
    def test_round_robin_reaches_every_active_account_and_wraps(self):
        pool=WorkerPool(self.fx.ledger,self.fx.install)
        expected=self.accounts();seen=set()
        for _ in range((len(expected)+1)//2):
            selected=pool.accounts();self.assertLessEqual(len(selected),2);seen.update(selected)
        self.assertEqual(seen,expected)
        self.assertTrue(pool.accounts())
    def test_two_account_cycle_sets_exact_tenant_context_and_bounds_job_batch(self):
        barrier=Barrier(2);outer=self
        other=fixture.RedemptionTests();other.setUp()
        class Probe:
            def __init__(inner,ledger,installations,tenant):inner.ledger=ledger;inner.tenant=tenant
            def cycle(inner,*,max_jobs):
                outer.assertEqual(max_jobs,1)
                barrier.wait(timeout=10)
                with inner.ledger.transaction(inner.tenant) as conn:
                    outer.assertEqual(str(conn.execute("SELECT bonuspro.current_tenant() AS id").fetchone()["id"]),inner.tenant)
                return "processed"
        pool=WorkerPool(self.fx.ledger,self.fx.install,factory=Probe)
        self.assertEqual(pool.cycle(),dict(accounts=2,failed=0))
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],5000)
    def test_one_account_failure_does_not_abort_other_account(self):
        class Failed:
            def __init__(inner,*a):pass
            def cycle(inner,**kw):raise RuntimeError("not printed credential")
        pool=WorkerPool(self.fx.ledger,self.fx.install,factory=Failed)
        result=pool.cycle()
        self.assertEqual(result["failed"],result["accounts"])
        self.assertLessEqual(result["accounts"],2)
if __name__=="__main__":unittest.main()
