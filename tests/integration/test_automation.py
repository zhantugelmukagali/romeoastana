import os,time,unittest
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import psycopg
import test_redemptions as fixture
import test_widget_api as api_fixture
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.moysklad.fulfillment import Fulfillment
from bonuspro.adapters.moysklad.remap import RetryLater,InvalidDocument
from bonuspro.worker import Worker

class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.RedemptionTests();self.fx.setUp()
        self.a=Automation(self.fx.ledger);self.tenant=self.fx.tenant
        self.a.configure(self.tenant,self.fx.actor)
    def proof(self,**patches):
        p=Fulfillment(self.fx.saved,datetime.now(timezone.utc)+timedelta(seconds=1),
            self.fx.saved.snapshot.total_minor,self.fx.saved.snapshot.total_minor,
            "eligible","a"*64,time.monotonic()+120)
        return replace(p,**patches)
    def job(self,order=None):
        self.a.jobs.enqueue(self.tenant,order_id=order or self.fx.order,key=str(uuid4()))
        return self.a.jobs.claim(self.tenant,lease_seconds=300)
    def run_proof(self,proof=None):
        self.a.complete(self.tenant,self.job(),proof or self.proof())
    def balance(self):return self.fx.ledger.snapshot(self.tenant,self.fx.wallet)
    def outcome(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT outcome FROM bonuspro.order_monitors WHERE order_id=%s",(self.fx.order,)).fetchone()["outcome"]
    def test_accrual_once_and_delivery_excluded(self):
        self.run_proof();self.run_proof()
        self.assertEqual(self.balance()["balance"],5500)
        with self.fx.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT * FROM bonuspro.order_accruals").fetchone()
            self.assertEqual((row["points"],row["eligible_minor"]),(500,1000000))
        self.assertEqual(self.outcome(),"credited")
    def test_pending_payment_shipment_cancel_or_return_never_credit(self):
        for outcome in ("waiting_payment","waiting_shipment","cancelled","return_review","document_review"):
            self.run_proof(self.proof(outcome=outcome))
            self.assertEqual(self.balance()["balance"],5000)
    def test_pinned_rule_survives_global_rule_change(self):
        self.fx.checkout.save_rules(self.tenant,earn_bps=1000,spend_bps=3000,delivery_policy="exclude",
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="New rule")
        self.run_proof()
        self.assertEqual(self.balance()["balance"],5500)
    def test_historical_order_cannot_earn(self):
        self.run_proof(self.proof(created_at=datetime(2020,1,1,tzinfo=timezone.utc)))
        self.assertEqual(self.balance()["balance"],5000);self.assertEqual(self.outcome(),"historical")
    def test_setup_idempotent_cannot_move_cutoff(self):
        first=self.a.configure(self.tenant,self.fx.actor)
        second=self.a.configure(self.tenant,str(uuid4()))
        self.assertEqual(first,second)
    def test_recover_saved_redemption_and_accrue_original_goods_base(self):
        prepared=self.fx.prepare();saved=self.fx.persisted(prepared)
        self.run_proof(self.proof(saved=saved))
        self.assertEqual(self.balance(),dict(balance=4500,reserved=0,available=4500))
        self.run_proof(self.proof(saved=saved))
        self.assertEqual(self.balance()["balance"],4500)
    def test_unverified_discount_stays_reserved(self):
        self.fx.prepare()
        self.run_proof()
        self.assertEqual(self.balance(),dict(balance=5000,reserved=1000,available=4000))
        self.assertEqual(self.outcome(),"redemption_pending")
    def test_expired_proof_rolls_back_credit_and_job_ack(self):
        job=self.job()
        with self.assertRaises(Conflict):self.a.complete(self.tenant,job,self.proof(deadline=0))
        self.assertEqual(self.balance()["balance"],5000)
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.jobs WHERE id=%s",(job["id"],)).fetchone()["state"],"running")
    def test_replaced_lease_cannot_credit(self):
        job=self.job();job["lease_id"]=uuid4()
        with self.assertRaises(Conflict):self.a.complete(self.tenant,job,self.proof())
        self.assertEqual(self.balance()["balance"],5000)
    def test_pause_blocks_credit_and_recovered_debit(self):
        prepared=self.fx.prepare()
        self.fx.install.change(self.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        with self.assertRaises(Conflict):self.run_proof(self.proof(saved=self.fx.persisted(prepared)))
        self.assertEqual(self.balance(),dict(balance=5000,reserved=1000,available=4000))
    def test_concurrent_jobs_cannot_double_credit(self):
        jobs=[self.job(),self.job()]
        proof=self.proof()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda job:self.a.complete(self.tenant,job,proof),jobs))
        self.assertEqual(self.balance()["balance"],5500)
    def test_changed_credited_order_blocks_new_spend_without_negative_balance(self):
        self.run_proof();self.run_proof(self.proof(outcome="waiting_payment"))
        self.assertEqual(self.balance()["balance"],5500)
        with self.assertRaises(Conflict):self.fx.prepare()
        self.assertEqual(self.outcome(),"document_review")
        with self.assertRaises(Conflict):
            self.fx.ledger.reserve(self.tenant,self.fx.wallet,order_id=str(uuid4()),points=1,eligible_minor=100000,
                rules=__import__("bonuspro.domain.finance",fromlist=["Rules"]).Rules(),
                quote_hash="a"*64,key=str(uuid4()),actor_id=self.fx.actor)
    def test_deleted_credited_document_records_review(self):
        self.run_proof();self.a.invalid(self.tenant,self.fx.order,"provider_document_missing")
        self.assertEqual(len(self.a.dashboard(self.tenant)["issues"]),1)
        with self.assertRaises(Conflict):self.fx.prepare()
    def test_discovery_cursor_and_order_hints_atomic(self):
        before=self.a.state(self.tenant)["scan_until"];order=str(uuid4());until=before+timedelta(minutes=1)
        with self.assertRaises(ValueError):
            self.a.discovery_complete(self.tenant,expected_cursor=before,until=until,orders=[order,"bad-uuid"])
        self.assertEqual(self.a.state(self.tenant)["scan_until"],before)
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.order_monitors WHERE order_id=%s",(order,)).fetchone())
        self.a.discovery_complete(self.tenant,expected_cursor=before,until=until,orders=[order])
        with self.assertRaises(Conflict):
            self.a.discovery_complete(self.tenant,expected_cursor=before,until=until,orders=[])
    def test_scheduler_coalesces_jobs_and_honors_retry_after(self):
        self.a.schedule(self.tenant)
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.a.hint_in(conn,self.tenant,self.fx.order)
        self.a.schedule(self.tenant)
        job=self.a.jobs.claim(self.tenant)
        self.assertIsNotNone(job);self.assertIsNone(self.a.jobs.claim(self.tenant))
        self.a.jobs.fail(self.tenant,str(job["id"]),str(job["lease_id"]),
                         error_code="provider_unavailable",retry_after=600)
        self.assertIsNone(self.a.jobs.claim(self.tenant))
        with self.fx.ledger.transaction(self.tenant) as conn:
            delay=conn.execute("SELECT extract(epoch FROM available_at-clock_timestamp()) d FROM bonuspro.jobs WHERE id=%s",(job["id"],)).fetchone()["d"]
            self.assertGreater(delay,590)
    def test_automatic_enrollment_no_widget_required(self):
        saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,order_id=str(uuid4()),counterparty_id=str(uuid4())))
        with self.fx.ledger.transaction(self.tenant) as conn:self.a.hint_in(conn,self.tenant,saved.snapshot.order_id)
        job=self.job(saved.snapshot.order_id)
        self.a.complete(self.tenant,job,self.proof(saved=saved))
        with self.fx.ledger.transaction(self.tenant) as conn:
            wallet=conn.execute("SELECT wallet_id FROM bonuspro.order_contexts WHERE order_id=%s",(saved.snapshot.order_id,)).fetchone()["wallet_id"]
        self.assertEqual(self.fx.ledger.snapshot(self.tenant,str(wallet))["balance"],500)
    def test_zero_earning_record_is_idempotent_without_zero_ledger_post(self):
        saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,eligible_minor=0))
        self.run_proof(self.proof(saved=saved));self.run_proof(self.proof(saved=saved))
        self.assertEqual(self.balance()["balance"],5000)
        with self.fx.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT points,credit_id FROM bonuspro.order_accruals").fetchone()
            self.assertEqual(row,dict(points=0,credit_id=None))
    def test_tenant_rls_hides_automation_and_immutable_records(self):
        self.run_proof()
        with self.fx.ledger.transaction(str(uuid4())) as conn:
            for table in ("automation_config","order_accruals","sync_state","order_monitors","reconciliation_issues"):
                self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro."+table).fetchone()["n"],0)
        with self.assertRaises(psycopg.Error):
            with self.fx.ledger.transaction(self.tenant) as conn:
                conn.execute("DELETE FROM bonuspro.order_accruals")
    def test_worker_recovers_queued_order(self):
        class Client:
            def order_ids_between(inner,start,end):return []
        worker=Worker(self.fx.ledger,self.fx.install,self.tenant,client_factory=lambda *a,**kw:Client())
        with patch("bonuspro.worker.FulfillmentReader.read",return_value=self.proof()):
            self.assertEqual(worker.cycle(),"processed")
        self.assertEqual(self.balance()["balance"],5500)
    def test_worker_rate_limit_keeps_job_and_cooldown(self):
        worker=Worker(self.fx.ledger,self.fx.install,self.tenant,client_factory=lambda *a,**kw:object())
        with patch("bonuspro.worker.FulfillmentReader.read",side_effect=RetryLater(600)):
            self.assertEqual(worker.cycle(),"provider_unavailable")
        self.assertEqual(self.balance()["balance"],5000)
        self.assertEqual(worker.cycle(),"cooldown")
    def test_worker_invalid_document_is_not_retried_as_success(self):
        worker=Worker(self.fx.ledger,self.fx.install,self.tenant,client_factory=lambda *a,**kw:object())
        with patch("bonuspro.worker.FulfillmentReader.read",side_effect=InvalidDocument("synthetic")):
            self.assertEqual(worker.cycle(),"processed")
        self.assertEqual(self.a.dashboard(self.tenant)["jobs"],{"dead":1})
        self.assertEqual(self.balance()["balance"],5000)

class AutomationApiTests(unittest.TestCase):
    def setUp(self):
        self.fx=api_fixture.WidgetAPITests();self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.client=self.fx.client;self.headers=self.fx.headers
    def test_admin_configuration_and_server_cutoff(self):
        path="/api/admin/automation"
        self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.post(path,headers=self.fx.auth("worker")).status_code,403)
        self.assertEqual(self.client.post(path,headers={**self.headers,"Origin":"https://evil.test"}).status_code,403)
        self.assertEqual(self.client.post(path,headers=self.headers,json={"starts_at":"2020-01-01"}).status_code,422)
        first=self.client.post(path,headers=self.headers)
        self.assertEqual(first.status_code,200,first.text)
        second=self.client.post(path,headers=self.headers)
        self.assertEqual(first.json(),second.json())
        result=self.client.get(path,headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()["configuration"]["starts_at"],first.json()["starts_at"])

if __name__=="__main__":unittest.main()
