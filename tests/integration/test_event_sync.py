import os,time,unittest,json
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import psycopg
import test_redemptions as fixture
import test_automation as automation_fixture
import test_widget_api as api_fixture
from bonuspro.adapters.postgres.signals import Signals
from bonuspro.adapters.postgres.streams import Streams
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.moysklad.remap import EntityMissing,RetryLater,ScanTooLarge,BASE
from bonuspro.events import signature
from bonuspro.security import AuthenticationFailed

class EventSyncTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.RedemptionTests();self.fx.setUp()
        self.tenant=self.fx.tenant;self.ledger=self.fx.ledger
        self.signals=Signals(self.ledger);self.auto=Automation(self.ledger)
        self.auto.configure(self.tenant,self.fx.actor);self.generation=self.signals.generation(self.tenant)
    def event(self,kind="customerorder",id=None):
        return dict(kind=kind,id=id or self.fx.order,action="UPDATE")
    def ingest(self,events=None,id=None,generation=None):
        id=id or str(uuid4())
        self.signals.ingest(self.tenant,generation or self.generation,id,events or [self.event()])
        return id
    def balance(self):return self.ledger.snapshot(self.tenant,self.fx.wallet)
    def test_repeated_delivery_and_concurrent_delivery_are_one_inbox_without_money(self):
        id=str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.ingest(id=id),range(2)))
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],1)
        self.assertEqual(self.balance()["balance"],5000)
        with self.assertRaises(Conflict):self.ingest(events=[self.event(id=str(uuid4()))],id=id)
    def test_old_generation_and_suspended_installation_reject_new_delivery(self):
        with self.assertRaises(AuthenticationFailed):self.ingest(generation=str(uuid4()))
        self.fx.install.change(self.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        with self.assertRaises(AuthenticationFailed):self.ingest()
    def test_notification_only_schedules_authoritative_verification(self):
        order=str(uuid4());self.ingest([self.event(id=order)])
        self.assertEqual(self.signals.drain(self.tenant,object()),1)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM bonuspro.order_monitors WHERE order_id=%s",(order,)).fetchone())
        self.assertEqual(self.balance()["balance"],5000)
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],0)
    def test_deleted_payment_uses_all_prior_order_relationships(self):
        payment,other=str(uuid4()),str(uuid4())
        with self.ledger.transaction(self.tenant) as conn:
            self.signals.index_in(conn,self.tenant,self.fx.order,[("paymentin",payment)])
            self.signals.index_in(conn,self.tenant,other,[("paymentin",payment)])
        self.ingest([self.event("paymentin",payment)])
        class Client:
            def entity(inner,*a):raise EntityMissing()
        self.signals.drain(self.tenant,Client())
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.order_monitors WHERE hint_version>0").fetchone()["n"],2)
    def test_batch_progress_is_bounded_and_provider_failure_does_not_acknowledge(self):
        self.ingest([self.event(id=str(uuid4())) for _ in range(6)])
        self.assertEqual(self.signals.drain(self.tenant,object()),4)
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],1)
        self.assertEqual(self.signals.drain(self.tenant,object()),2)
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],0)
        self.ingest([self.event("paymentin",str(uuid4()))])
        class Client:
            def entity(inner,*a):raise RetryLater(30)
        with self.assertRaises(RetryLater):self.signals.drain(self.tenant,Client())
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],1)
    def test_resume_discards_old_envelope_and_restarts_stream_cursors(self):
        self.ingest()
        streams=Streams(self.ledger,self.signals);now=datetime.now(timezone.utc)+timedelta(hours=1)
        before=streams.next(self.tenant,now)
        self.fx.install.change(self.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Resume","synthetic")
        self.fx.activate()
        self.signals.drain(self.tenant,object())
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],0)
        after=streams.next(self.tenant,now)
        self.assertNotEqual(before["generation"],after["generation"])
        self.assertEqual(after["cursor_at"],self.auto.state(self.tenant)["starts_at"])
    def choose(self,stream):
        self.streams=Streams(self.ledger,self.signals)
        self.now=datetime.now(timezone.utc)+timedelta(hours=1)
        self.streams.next(self.tenant,self.now)
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("UPDATE bonuspro.sync_streams SET ready_at=%s",(self.now+timedelta(days=1),))
            conn.execute("UPDATE bonuspro.sync_streams SET ready_at=%s WHERE stream=%s",(self.now-timedelta(seconds=1),stream))
    def test_changed_payment_cursor_advances_only_with_durable_inbox(self):
        self.choose("paymentin");payment=str(uuid4())
        class Client:
            def changed_ids_between(inner,*a):return [payment]
        before=self.auto.state(self.tenant)["starts_at"]
        self.streams.scan(self.tenant,Client(),self.now)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertGreater(conn.execute("SELECT cursor_at FROM bonuspro.sync_streams WHERE stream='paymentin'").fetchone()["cursor_at"],before)
            self.assertEqual(conn.execute("SELECT events FROM bonuspro.webhook_inbox").fetchone()["events"],[self.event("paymentin",payment)])
        self.assertEqual(self.balance()["balance"],5000)
    def test_failure_or_overflow_preserves_cursor(self):
        self.choose("paymentin")
        before=self.auto.state(self.tenant)["starts_at"]
        class Failure:
            def changed_ids_between(inner,*a):raise RetryLater(30)
        with self.assertRaises(RetryLater):self.streams.scan(self.tenant,Failure(),self.now)
        class Overflow:
            def changed_ids_between(inner,*a):raise ScanTooLarge()
        self.assertFalse(self.streams.scan(self.tenant,Overflow(),self.now))
        with self.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT * FROM bonuspro.sync_streams WHERE stream='paymentin'").fetchone()
            self.assertEqual(row["cursor_at"],before);self.assertEqual(row["window_seconds"],43200)
    def test_creation_rescan_finds_late_new_order_without_rechecking_known_history(self):
        self.choose("created_rescan");order=str(uuid4())
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("UPDATE bonuspro.order_monitors SET next_due=%s WHERE order_id=%s",(self.now+timedelta(days=2),self.fx.order))
            previous=conn.execute("SELECT next_due FROM bonuspro.order_monitors WHERE order_id=%s",(self.fx.order,)).fetchone()["next_due"]
        class Client:
            def changed_ids_between(inner,kind,field,*a):
                assert (kind,field)==("customerorder","created")
                return [order,self.fx.order]
        self.streams.scan(self.tenant,Client(),self.now)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM bonuspro.order_monitors WHERE order_id=%s",(order,)).fetchone())
            self.assertEqual(conn.execute("SELECT next_due FROM bonuspro.order_monitors WHERE order_id=%s",(self.fx.order,)).fetchone()["next_due"],previous)
            row=conn.execute("SELECT ready_at,cursor_at FROM bonuspro.sync_streams WHERE stream='created_rescan'").fetchone()
            self.assertGreater(row["ready_at"],self.now)
            self.assertEqual(row["cursor_at"],self.auto.state(self.tenant)["starts_at"])
    def test_hint_during_lease_is_not_overwritten_by_completion(self):
        af=automation_fixture.AutomationTests();af.setUp()
        job=af.job()
        with af.fx.ledger.transaction(af.tenant) as conn:af.a.hint_in(conn,af.tenant,af.fx.order)
        af.a.complete(af.tenant,job,af.proof())
        with af.fx.ledger.transaction(af.tenant) as conn:
            delay=conn.execute("SELECT extract(epoch FROM next_due-now()) AS d FROM bonuspro.order_monitors WHERE order_id=%s",(af.fx.order,)).fetchone()["d"]
            self.assertLessEqual(delay,60)
        job=af.job();af.a.complete(af.tenant,job,af.proof())
        with af.fx.ledger.transaction(af.tenant) as conn:
            delay=conn.execute("SELECT extract(epoch FROM next_due-now()) AS d FROM bonuspro.order_monitors WHERE order_id=%s",(af.fx.order,)).fetchone()["d"]
            self.assertGreater(delay,86000)
    def test_tenant_rls_and_recent_inbox_deletion_guards(self):
        self.ingest();self.signals.drain(self.tenant,object())
        with self.ledger.transaction(str(uuid4())) as conn:
            for table in ("webhook_inbox","related_documents","sync_streams","webhook_setup"):
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro."+table).fetchone()["n"],0)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.tenant) as conn:conn.execute("DELETE FROM bonuspro.webhook_inbox")
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.tenant) as conn:conn.execute("UPDATE bonuspro.webhook_inbox SET events='[]'::jsonb")
    def test_cleanup_only_removes_old_terminal_technical_rows(self):
        self.ingest();self.signals.drain(self.tenant,object())
        active=self.ingest()
        job=self.auto.jobs.enqueue(self.tenant,order_id=self.fx.order,key=str(uuid4()))
        lease=self.auto.jobs.claim(self.tenant);self.auto.jobs.complete(self.tenant,job,str(lease["lease_id"]))
        # Only the isolated CI admin ages synthetic rows; production has no such privilege.
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute("UPDATE bonuspro.webhook_inbox SET completed_at=now()-interval '31 days' WHERE tenant_id=%s AND state='done'",(self.tenant,))
            conn.execute("UPDATE bonuspro.jobs SET updated_at=now()-interval '31 days' WHERE tenant_id=%s AND state='done'",(self.tenant,))
        self.signals.prune(self.tenant)
        self.assertEqual(self.signals.status(self.tenant)["inbox"]["pending"],1)
        self.assertEqual(self.balance()["balance"],5000)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.jobs").fetchone()["n"],0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.webhook_inbox").fetchone()["n"],1)

class EventApiTests(unittest.TestCase):
    def setUp(self):
        self.fx=api_fixture.WidgetAPITests();self.fx.setUp()
        self.addCleanup(self.fx.doCleanups);self.client=self.fx.client
        self.signals=Signals(self.fx.fx.ledger)
        tenant=self.fx.fx.tenant;app=self.fx.fx.install.config.app_id
        generation=self.signals.generation(tenant)
        tag=signature(self.fx.fx.install.config.encryption_key,app,tenant,generation)
        self.path="/hooks/remap/"+app+"/"+tenant+"/"+generation+"/"+tag+"?requestId="+str(uuid4())
        self.body={"events":[{"accountId":tenant,"action":"UPDATE","meta":{"type":"customerorder","href":BASE+"/entity/customerorder/"+self.fx.fx.order},"points":999999}]}
    def test_callback_capability_accepts_no_browser_session_and_does_not_change_balance(self):
        for _ in range(2):
            response=self.client.post(self.path,json=self.body)
            self.assertEqual(response.status_code,204,response.text)
        self.assertEqual(self.signals.status(self.fx.fx.tenant)["inbox"]["pending"],1)
        self.assertEqual(self.fx.fx.ledger.snapshot(self.fx.fx.tenant,self.fx.fx.wallet)["balance"],5000)
    def test_missing_or_changed_capability_and_foreign_account_are_rejected(self):
        path=self.path.replace(self.path.split("/")[-1].split("?")[0],"0"*64)
        self.assertEqual(self.client.post(path,json=self.body).status_code,401)
        body=json.loads(json.dumps(self.body));body["events"][0]["accountId"]=str(uuid4())
        self.assertEqual(self.client.post(self.path,json=body).status_code,422)
        self.assertEqual(self.signals.status(self.fx.fx.tenant)["inbox"]["pending"],0)
    def test_setup_requires_admin_and_origin(self):
        path="/api/admin/webhooks/setup"
        self.assertEqual(self.client.post(path).status_code,403)
        self.assertEqual(self.client.post(path,headers=self.fx.auth("worker")).status_code,403)
        self.assertEqual(self.client.post(path,headers={**self.fx.headers,"Origin":"https://evil.test"}).status_code,403)

if __name__=="__main__":unittest.main()
