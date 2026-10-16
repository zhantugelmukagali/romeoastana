import os,time,unittest
from uuid import uuid4
from dataclasses import replace
import psycopg
from bonuspro.adapters.postgres.voids import Voids
from bonuspro.adapters.postgres.ledger import Conflict
import test_automation as fixture

class VoidTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.AutomationTests();self.fx.setUp();self.sale=self.fx.fx
        self.v=Voids(self.sale.ledger);self.p=self.sale.prepare()
        self.proof=self.fx.proof(saved=replace(self.sale.saved,snapshot=replace(self.sale.saved.snapshot,cancelled=True)),
                                  outcome="cancelled",cancel_safe=True,paid_minor=0,shipped_minor=0)
    def age(self):
        # Synthetic clock fixture only: change creation time under CI administrator, not runtime privileges.
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            assert conn.info.dbname=="bonuspro_ci"
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute("UPDATE bonuspro.reservations SET created_at=clock_timestamp()-interval '16 minutes' WHERE tenant_id=%s AND id=%s",
                         (self.sale.tenant,self.p["id"]))
    def release(self,proof=None):
        return self.v.release(self.sale.tenant,self.p["id"],proof or self.proof,actor=self.sale.actor,reason="Order cancelled; tabs closed")
    def test_requires_original_cancelled_document_and_minimum_age(self):
        with self.assertRaises(Conflict):self.release()
        self.age()
        for p in (replace(self.proof,cancel_safe=False),replace(self.proof,paid_minor=1),
                  replace(self.proof,saved=self.sale.saved),replace(self.proof,saved=self.sale.persisted(self.p)),
                  replace(self.proof,deadline=time.monotonic()-1)):
            with self.assertRaises(Conflict):self.release(p)
        self.assertEqual(self.fx.balance()["reserved"],1000)
        self.assertEqual(self.release(),{"state":"released"});self.release()
        self.assertEqual(self.fx.balance(),dict(balance=5000,reserved=0,available=5000))
    def test_committed_discount_cannot_be_released(self):
        self.age();self.sale.redemptions.confirm_saved(self.sale.tenant,self.sale.persisted(self.p))
        with self.assertRaises(Conflict):self.release()
        self.assertEqual(self.fx.balance()["balance"],4000)
    def test_direct_release_without_audited_void_is_rejected(self):
        self.age()
        with self.assertRaises(psycopg.Error):
            with self.sale.ledger.transaction(self.sale.tenant) as conn:
                conn.execute("UPDATE bonuspro.reservations SET state='released' WHERE id=%s",(self.p["id"],))
    def test_late_old_marker_blocks_wallet_and_corrected_document_can_be_reviewed(self):
        self.age();self.release()
        late=self.fx.proof(saved=self.sale.persisted(self.p))
        self.fx.run_proof(late)
        issues=self.fx.a.dashboard(self.sale.tenant)["issues"];self.assertEqual(len(issues),1)
        self.assertEqual(issues[0]["reason"],"unrecognized_bonus_marker")
        self.assertEqual(self.fx.balance()["balance"],5000)
        self.fx.a.resolve_issue(self.sale.tenant,str(issues[0]["id"]),self.proof,actor=self.sale.actor,reason="Restored cancelled original")
        self.assertEqual(self.fx.a.dashboard(self.sale.tenant)["issues"],[])
    def test_void_history_is_tenant_scoped_and_immutable(self):
        self.age();self.release()
        with self.sale.ledger.transaction(str(uuid4())) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.redemption_voids").fetchone()["n"],0)
        with self.assertRaises(psycopg.Error):
            with self.sale.ledger.transaction(self.sale.tenant) as conn:conn.execute("DELETE FROM bonuspro.redemption_voids")

if __name__=="__main__":unittest.main()
