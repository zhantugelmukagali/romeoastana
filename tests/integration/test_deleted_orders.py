"""A missing empty draft is a bounded observation, never permission to erase money."""
import unittest
from uuid import uuid4
from unittest.mock import patch
import test_automation as fixtures
from bonuspro.adapters.moysklad.remap import EntityMissing
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.postgres.readiness import Readiness
from bonuspro.domain.finance import Rules
from bonuspro.worker import Worker

class DeletedDraftTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AutomationTests();self.f.setUp()
        self.fx=self.f.fx;self.a=self.f.a;self.tenant=self.f.tenant
    def cycle(self,error):
        class Client:
            def order_ids_between(self,start,end):return []
        worker=Worker(self.fx.ledger,self.fx.install,self.tenant,client_factory=lambda *a,**kw:Client())
        with patch("bonuspro.worker.FulfillmentReader.read",side_effect=error):
            self.assertEqual(worker.cycle(),"processed")
    def missing(self,kind="customerorder",identity=None):
        return EntityMissing(requested_kind=kind,requested_id=identity or self.fx.order)
    def hint(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.a.hint_in(conn,self.tenant,self.fx.order)

    def test_deleted_uncredited_draft_is_done_cancelled_and_does_not_break_readiness(self):
        before=self.f.balance()
        self.cycle(self.missing())
        self.assertEqual(self.f.outcome(),"cancelled")
        self.assertEqual(self.f.balance(),before)
        self.assertTrue(Readiness(self.fx.ledger).report(self.tenant)["ready"])
        with self.fx.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT extract(epoch FROM next_due-clock_timestamp()) delay "
                             "FROM bonuspro.order_monitors WHERE order_id=%s",(self.fx.order,)).fetchone()
            self.assertGreater(row["delay"],250)
            self.assertLessEqual(row["delay"],301)
            self.assertEqual(conn.execute("SELECT state,last_error FROM bonuspro.jobs").fetchone(),
                             dict(state="done",last_error=None))
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.reconciliation_issues").fetchone()["n"],0)

    def test_related_unknown_or_other_order_404_remains_provider_error(self):
        for error in (self.missing("paymentin"),self.missing(identity=str(uuid4())),EntityMissing()):
            with self.subTest(error_kind=error.requested_kind):
                self.hint()
                self.cycle(error)
                self.assertEqual(self.f.outcome(),"provider_error")
                self.assertFalse(Readiness(self.fx.ledger).report(self.tenant)["ready"])
                self.assertEqual(self.f.balance(),dict(balance=5000,reserved=0,available=5000))

    def test_deleted_order_with_unverified_attempt_keeps_reserved_points_and_review_state(self):
        prepared=self.fx.prepare()
        before=self.f.balance()
        self.cycle(self.missing())
        self.assertEqual(self.f.outcome(),"provider_error")
        self.assertEqual(self.f.balance(),before)
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.reservations WHERE id=%s",
                                          (prepared["id"],)).fetchone()["state"],"verifying")
        self.assertFalse(Readiness(self.fx.ledger).report(self.tenant)["ready"])

    def test_released_reservation_still_counts_as_financial_history(self):
        identity=self.fx.ledger.reserve(self.tenant,self.fx.wallet,order_id=self.fx.order,
            points=1,eligible_minor=1000000,rules=Rules(),quote_hash="a"*64,
            key=str(uuid4()),actor_id=self.fx.actor)
        self.fx.ledger.release_unapplied(self.tenant,identity)
        before=self.f.balance()
        self.cycle(self.missing())
        self.assertEqual(self.f.outcome(),"provider_error")
        self.assertEqual(self.f.balance(),before)
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.reservations WHERE id=%s",
                                          (identity,)).fetchone()["state"],"released")

    def test_deleted_credited_order_keeps_journal_and_creates_reconciliation_issue(self):
        self.f.run_proof()
        before=self.f.balance();self.hint()
        self.cycle(self.missing())
        self.assertEqual(self.f.outcome(),"provider_error")
        self.assertEqual(self.f.balance(),before)
        self.assertEqual(len(self.a.dashboard(self.tenant)["issues"]),1)
        self.assertFalse(Readiness(self.fx.ledger).report(self.tenant)["ready"])

    def test_replaced_job_lease_cannot_cancel_a_monitor(self):
        job=self.f.job()
        job["lease_id"]=uuid4()
        with self.assertRaises(Conflict):
            self.a.invalid(self.tenant,self.fx.order,"provider_document_missing",
                           missing_order=True,job=job)
        self.assertNotEqual(self.f.outcome(),"cancelled")
        self.assertEqual(self.f.balance(),dict(balance=5000,reserved=0,available=5000))

if __name__=="__main__":unittest.main()
