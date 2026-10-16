import os,time,unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import psycopg
import test_automation as fixture
from bonuspro.adapters.postgres.ledger import Conflict,VerifiedDiscount
from bonuspro.domain.finance import Rules

class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.AutomationTests();self.fx.setUp()
        self.a=self.fx.a;self.tenant=self.fx.tenant;self.sale=self.fx.fx
        self.saved=self.sale.saved
    def proof(self,qty=None,**kwargs):
        returned=() if qty is None else (replace(self.saved.lines[0],id=str(uuid4()),quantity=Decimal(str(qty))),)
        return self.fx.proof(saved=self.saved,returned=returned,full_payment=True,full_shipment=True,
                             outcome="return_review" if returned else "eligible",**kwargs)
    def process(self,proof=None):self.a.complete(self.tenant,self.fx.job(),proof or self.proof())
    def funds(self):return self.fx.balance()
    def prepare_sale(self):
        operation=self.sale.prepare();self.saved=self.sale.persisted(operation);self.process()
    def cases(self,state="pending"):return self.a.settlements.list_cases(self.tenant,state=state)["items"]
    def spend_all(self):
        n=self.funds()["available"];order=str(uuid4())
        rid=self.sale.ledger.reserve(self.tenant,self.sale.wallet,order_id=order,points=n,
            eligible_minor=n*100,rules=Rules(500,10000),quote_hash="b"*64,key=str(uuid4()),actor_id=self.sale.actor)
        self.sale.ledger.begin_verification(self.tenant,rid)
        self.sale.ledger.confirm_discount(self.tenant,rid,proof=VerifiedDiscount(order,"b"*64,n*100,"saved"))
    def resolve(self,case,proof,decision="waive",reason="Administrator decision"):
        return self.a.resolve_settlement(self.tenant,case["id"],proof,decision=decision,reason=reason,actor=self.sale.actor)
    def test_partial_full_and_cancelled_return_are_cumulative(self):
        self.prepare_sale()
        self.assertEqual(self.funds()["balance"],4500)
        half=self.proof("0.5");self.process(half);self.process(half)
        self.assertEqual(self.funds()["balance"],4750)
        self.process(self.proof(1));self.assertEqual(self.funds()["balance"],5000)
        self.process(self.proof());self.assertEqual(self.funds()["balance"],4500)
    def test_sale_first_seen_with_return_only_posts_remaining_result(self):
        self.process(self.proof("0.5"))
        self.assertEqual(self.funds()["balance"],5250)
        self.process(self.proof("0.5"))
        self.assertEqual(self.funds()["balance"],5250)
    def test_unpaid_after_accrual_reverses_and_repayment_restores(self):
        self.process();self.assertEqual(self.funds()["balance"],5500)
        p=replace(self.proof(),full_payment=False,outcome="waiting_payment")
        self.process(p);self.assertEqual(self.funds()["balance"],5000)
        self.process();self.assertEqual(self.funds()["balance"],5500)
    def test_insufficient_return_stays_pending_even_if_balance_later_grows(self):
        self.process();self.spend_all();p=self.proof(1);self.process(p)
        self.assertEqual(self.funds()["balance"],0);case=self.cases()[0]
        self.sale.ledger.credit(self.tenant,self.sale.wallet,points=500,key="later",actor_id=self.sale.actor,reason="Later purchase")
        self.process(p)
        self.assertEqual(self.funds()["balance"],500)
        self.resolve(case,p,decision="apply")
        self.assertEqual(self.funds()["balance"],0)
        self.assertEqual(self.resolve(case,p,decision="apply")["state"],"applied")
    def test_waiver_not_credited_twice_when_return_is_undone(self):
        self.process();self.spend_all();p=self.proof(1);self.process(p);case=self.cases()[0]
        self.resolve(case,p)
        self.assertEqual(self.funds()["balance"],0)
        self.process();self.process(p)
        self.assertEqual(self.funds()["balance"],0)
    def test_partial_waiver_does_not_waive_future_additional_returns(self):
        self.process();self.spend_all();p=self.proof("0.5");self.process(p)
        self.resolve(self.cases()[0],p)
        self.process(self.proof(1))
        self.assertEqual(self.cases()[0]["earn_delta"],-250)
    def test_return_undo_after_refunded_points_spent_requires_admin(self):
        self.prepare_sale();p=self.proof(1);self.process(p);self.spend_all()
        self.process()
        case=self.cases()[0]
        self.assertEqual((case["earn_delta"],case["refund_delta"],case["total_delta"]),(500,-1000,-500))
        self.assertEqual(self.funds()["balance"],0)
        with self.assertRaises(Conflict):self.resolve(case,self.proof(),decision="apply")
    def test_changed_return_supersedes_case_and_rejects_old_decision(self):
        self.process();self.spend_all();p=self.proof(1);self.process(p);case=self.cases()[0]
        with self.assertRaises(Conflict):self.resolve(case,self.proof("0.5"))
        self.process(self.proof("0.5"))
        self.assertEqual(len(self.cases("superseded")),1)
        self.assertEqual(self.cases()[0]["earn_delta"],-250)
    def test_cancelled_unpaid_unshipped_order_refunds_confirmed_redemption(self):
        operation=self.sale.prepare();self.saved=self.sale.persisted(operation)
        self.saved=replace(self.saved,snapshot=replace(self.saved.snapshot,cancelled=True))
        p=replace(self.proof(),outcome="cancelled",full_payment=False,full_shipment=False,cancel_safe=True)
        self.process(p);self.assertEqual(self.funds()["balance"],5000)
        self.assertEqual(self.funds()["reserved"],0)
    def test_parallel_reconciliation_posts_once(self):
        self.prepare_sale();jobs=[self.fx.job(),self.fx.job()];proof=self.proof(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda j:self.a.complete(self.tenant,j,proof),jobs))
        self.assertEqual(self.funds()["balance"],5000)
    def test_zero_net_adjustment_keeps_components_without_zero_posting(self):
        # 5% spend matches 5% earn: refund and earning reversal cancel, still audited.
        operation=self.sale.prepare(points=500)
        saved=self.sale.saved
        self.saved=replace(saved,lines=(replace(saved.lines[0],discount_bps=500),saved.lines[1]),
            marker="bonuspro:v1:"+operation["id"],
            snapshot=replace(saved.snapshot,total_minor=1150000,eligible_minor=950000,revision="saved"))
        self.process();self.process(self.proof(1))
        self.assertEqual(self.funds()["balance"],5000)
        with self.sale.ledger.transaction(self.tenant) as c:
            e=c.execute("SELECT * FROM bonuspro.settlement_events").fetchone()
            self.assertEqual((e["earn_delta"],e["refund_delta"]),(-500,500))
            self.assertIsNone(c.execute("SELECT 1 FROM bonuspro.ledger_transactions WHERE settlement_event_id=%s",(e["id"],)).fetchone())
    def test_pending_case_blocks_spending_and_is_tenant_isolated(self):
        self.process();self.spend_all();self.process(self.proof(1))
        self.assertEqual(self.a.settlements.list_cases(str(uuid4()))["items"],[])
        self.sale.ledger.credit(self.tenant,self.sale.wallet,points=1000,key="new",actor_id=self.sale.actor,reason="Later")
        with self.assertRaises(Conflict):self.sale.prepare()
    def test_journal_cannot_forge_settlement_without_event(self):
        with self.assertRaises(psycopg.Error):
            with self.sale.ledger.transaction(self.tenant) as c:
                self.sale.ledger._post(c,self.tenant,self.sale.wallet,"bad","f"*64,100,"settlement",self.sale.actor,"invalid")
    def test_corrected_issue_resolves_and_can_recur_as_new_episode(self):
        self.process()
        bad=replace(self.proof(),outcome="document_review")
        self.process(bad)
        issue=self.a.dashboard(self.tenant)["issues"][0]
        with self.assertRaises(Conflict):self.a.resolve_issue(self.tenant,str(issue["id"]),bad,actor=self.sale.actor,reason="Checked")
        self.a.resolve_issue(self.tenant,str(issue["id"]),self.proof(),actor=self.sale.actor,reason="Corrected")
        self.assertEqual(self.a.dashboard(self.tenant)["issues"],[])
        self.process(bad)
        self.assertNotEqual(self.a.dashboard(self.tenant)["issues"][0]["id"],issue["id"])

if __name__=="__main__":unittest.main()
