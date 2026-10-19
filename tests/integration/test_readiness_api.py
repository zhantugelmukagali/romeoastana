import unittest
from uuid import uuid4
import test_portal_api as fixture
from bonuspro.adapters.postgres.redemptions import Redemptions

class ReadinessAPITests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PortalAPITests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_configuration_is_read_from_database_without_financial_mutation(self):
        path="/api/admin/readiness";f=self.f
        before=f.fx.ledger.snapshot(f.fx.tenant,f.fx.wallet)
        r=f.client.get(path,headers=f.headers);self.assertEqual(r.status_code,200,r.text)
        report=r.json();self.assertFalse(report["ready"])
        self.assertFalse(any(x["complete"] for x in report["checks"]))
        saved=f.client.put("/api/admin/rules",headers=f.headers,json=dict(earn_bps=500,spend_bps=3000,
            expected_version=0,request_id=str(uuid4()),reason="Approved operating rules"))
        self.assertEqual(saved.status_code,200,saved.text)
        Redemptions(f.fx.ledger).bind(f.fx.tenant,str(uuid4()),f.fx.user)
        report=f.client.get(path,headers=f.headers).json()
        self.assertEqual([x["complete"] for x in report["checks"]],[True,True,False,False])
        self.assertIsNone(report["last_processing_at"])
        self.assertEqual(f.fx.ledger.snapshot(f.fx.tenant,f.fx.wallet),before)
        self.assertNotIn("synthetic-remap",str(report))
    def test_anonymous_staff_and_mutations_are_rejected(self):
        path="/api/admin/readiness";f=self.f
        self.assertEqual(f.client.get(path).status_code,401)
        self.assertEqual(f.client.get(path,headers=f.fx.headers("worker")).status_code,403)
        self.assertEqual(f.client.post(path,headers=f.headers,json={"ready":True}).status_code,405)


class ProcessingEvidenceReadinessTests(unittest.TestCase):
    def setUp(self):
        import test_automation
        from bonuspro.adapters.postgres.readiness import Readiness
        self.f=test_automation.AutomationTests();self.f.setUp()
        self.ledger=self.f.fx.ledger;self.tenant=self.f.tenant
        self.readiness=Readiness(self.ledger)
        self.f.a.cooldown(self.tenant,0,None)
    def report(self):return self.readiness.report(self.tenant)
    def test_a_fresh_cycle_with_failed_documents_is_not_processing_ready(self):
        from unittest.mock import patch
        from bonuspro.worker import Worker
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        worker=Worker(self.ledger,self.f.fx.install,self.tenant,client_factory=lambda *a,**kw:object())
        with patch("bonuspro.worker.FulfillmentReader.read",side_effect=InvalidDocument("private provider body")):
            self.assertEqual(worker.cycle(),"processed")
        report=self.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["processing_status"],"provider_documents")
        self.assertEqual(report["attention"]["provider_errors"],1)
        self.assertNotIn("private",str(report))
        self.assertEqual(self.ledger.snapshot(self.tenant,self.f.fx.wallet)["balance"],5000)
        # A repaired current order clears attention even though its old dead job remains.
        with self.ledger.transaction(self.tenant) as conn:
            self.f.a.outcome_in(conn,self.tenant,self.f.fx.order,"credited")
        self.assertTrue(self.report()["ready"])
    def test_a_recent_heartbeat_does_not_hide_an_unscanned_period(self):
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("UPDATE bonuspro.sync_state SET scan_until=clock_timestamp()-interval '11 minutes' WHERE tenant_id=%s",(self.tenant,))
        report=self.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["processing_status"],"discovery_delayed")
        self.assertGreaterEqual(report["scan_lag_seconds"],660)
    def test_only_stale_verifications_and_active_backlogs_require_attention(self):
        self.f.fx.prepare()
        self.assertTrue(self.report()["ready"])
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.reservations "
                "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,state,actor_id,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,1,'verifying',%s,clock_timestamp()-interval '16 minutes')",
                (self.tenant,str(uuid4()),self.f.fx.wallet,str(uuid4()),"a"*64,str(uuid4()),"a"*64,self.f.fx.actor))
        report=self.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["attention"]["stale_redemptions"],1)
        self.assertEqual(report["processing_status"],"unverified_redemptions")
    def test_active_old_queue_is_reported_without_permanent_historical_failure(self):
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.jobs "
                "(tenant_id,id,operation_key,request_hash,kind,entity_id,created_at) "
                "VALUES (%s,%s,%s,%s,'reconcile_order',%s,clock_timestamp()-interval '16 minutes')",
                (self.tenant,str(uuid4()),str(uuid4()),"a"*64,self.f.fx.order))
        job=self.f.a.jobs.claim(self.tenant)
        self.assertEqual(self.report()["processing_status"],"queue_delayed")
        self.f.a.jobs.fail(self.tenant,str(job["id"]),str(job["lease_id"]),error_code="invalid_document",permanent=True)
        self.assertTrue(self.report()["ready"])
    def test_outstanding_document_issues_are_visible_but_normal_waiting_is_not_failure(self):
        for outcome in ("waiting_payment","waiting_shipment","return_review","cancelled"):
            with self.ledger.transaction(self.tenant) as conn:
                self.f.a.outcome_in(conn,self.tenant,self.f.fx.order,outcome)
            self.assertTrue(self.report()["ready"])
        with self.ledger.transaction(self.tenant) as conn:
            self.f.a.issue_in(conn,self.tenant,self.f.fx.order,self.f.fx.wallet,"changed_credited_order","a"*64)
        report=self.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["attention"]["unresolved_issues"],1)
    def test_attention_is_scoped_to_the_selected_account(self):
        import test_automation
        other=test_automation.AutomationTests();other.setUp()
        other.a.invalid(other.tenant,other.fx.order,"provider_document_missing")
        self.assertTrue(self.report()["ready"])
        self.assertEqual(self.report()["attention"]["provider_errors"],0)

    def test_unexpected_worker_failure_invalidates_previous_success_without_leaking_text(self):
        from unittest.mock import patch
        from bonuspro.worker import Worker
        worker=Worker(self.ledger,self.f.fx.install,self.tenant,client_factory=lambda *a,**kw:object())
        self.assertTrue(self.report()["ready"])
        with patch("bonuspro.worker.FulfillmentReader.read",side_effect=RuntimeError("private credential or provider body")):
            with self.assertRaises(RuntimeError):
                worker.cycle()
        report=self.report()
        self.assertFalse(report["ready"])
        self.assertEqual(report["processing_status"],"provider_unavailable")
        self.assertNotIn("private",str(report))
        self.assertEqual(self.f.a.state(self.tenant)["last_error"],"provider_unavailable")
        self.assertEqual(self.ledger.snapshot(self.tenant,self.f.fx.wallet)["balance"],5000)
