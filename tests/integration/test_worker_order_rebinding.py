"""Worker recovery after a saved buyer change with no order financial history."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import uuid4

import test_automation as fixtures
from bonuspro.adapters.moysklad.fulfillment import MSK
from bonuspro.adapters.moysklad.remap import BASE,OrderEvidence
from bonuspro.domain.finance import Rules
from bonuspro.worker import Worker


class WorkerOrderRebindingTests(unittest.TestCase):
    def setUp(self):
        self.test=fixtures.AutomationTests()
        self.test.setUp()
        self.fx=self.test.fx
        self.a=self.test.a
        self.tenant=self.fx.tenant

    def current_context(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT * FROM bonuspro.order_contexts WHERE tenant_id=%s AND order_id=%s",
                                (self.tenant,self.fx.order)).fetchone()

    def assert_no_new_identity(self,customer,wallet_count):
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT wallet_id FROM bonuspro.customer_wallets "
                "WHERE tenant_id=%s AND counterparty_id=%s",(self.tenant,customer)).fetchone())
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.wallets WHERE tenant_id=%s",
                                          (self.tenant,)).fetchone()["n"],wallet_count)
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                "WHERE tenant_id=%s AND order_id=%s",(self.tenant,self.fx.order)).fetchone()["n"],0)

    def changed_saved(self,customer):
        s=self.fx.saved.snapshot
        return replace(self.fx.saved,snapshot=replace(s,counterparty_id=customer,
            revision="buyer-saved-with-widget-closed",paid_minor=s.total_minor,shipped_minor=s.total_minor))

    def wallet_count(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT count(*) n FROM bonuspro.wallets WHERE tenant_id=%s",
                                (self.tenant,)).fetchone()["n"]

    def test_real_document_graph_rebinds_preview_then_credits_only_the_saved_buyer(self):
        fx=self.fx
        original=self.current_context()
        old_funds=fx.ledger.snapshot(self.tenant,fx.wallet)
        customer=str(uuid4())
        fx.checkout.save_rules(self.tenant,earn_bps=1000,spend_bps=3000,delivery_policy="exclude",
            expected_version=1,request_id=str(uuid4()),actor_id=fx.actor,reason="Changed after first preview")
        currency,demand,payment=(str(uuid4()) for _ in range(3))
        def ref(kind,identity):
            return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+identity}}
        s=fx.saved.snapshot
        doc=dict(id=fx.order,accountId=fx.tenant,applicable=True,
            created=(datetime.now(timezone.utc)+timedelta(seconds=1)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            updated="2026-10-19 12:00:00.000",sum=s.total_minor,payedSum=s.total_minor,shippedSum=s.total_minor,
            agent=ref("counterparty",customer),organization=ref("organization",fx.saved.organization_id),
            rate={"currency":ref("currency",currency),"value":1},vatIncluded=True,vatEnabled=False,name="001")
        lines=tuple(dict(id=x.id,assortment=ref(x.kind,x.assortment),price=x.price,quantity=x.quantity,
            discount=Decimal(x.discount_bps)/100,vat=x.vat,vatEnabled=x.vat_enabled) for x in fx.saved.lines)
        ship={**doc,"id":demand,"customerOrder":ref("customerorder",fx.order)}
        pay={**doc,"id":payment,"meta":{"type":"paymentin"},
             "operations":[{**ref("customerorder",fx.order),"linkedSum":s.total_minor}]}
        class Client:
            tenant=fx.tenant
            def order_evidence(self,identity):
                return OrderEvidence(copy.deepcopy(doc),copy.deepcopy(lines),(copy.deepcopy(pay),),(copy.deepcopy(ship),))
            def positions(self,*args):return copy.deepcopy(lines)
            def linked(self,*args):return []
            def entity(self,*args):return {"isoCode":"KZT"}
            def order_ids_between(self,*args):return []
        worker=Worker(fx.ledger,fx.install,self.tenant,client_factory=lambda *a,**kw:Client())
        self.assertEqual(worker.cycle(),"processed")
        context=self.current_context()
        self.assertEqual(str(context["counterparty_id"]),customer)
        for field in ("rule_version","initial_source_hash","enrolled_by","created_at"):
            self.assertEqual(context[field],original[field],field)
        new_wallet=str(context["wallet_id"])
        self.assertNotEqual(new_wallet,fx.wallet)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),old_funds)
        self.assertEqual(fx.ledger.snapshot(self.tenant,new_wallet),dict(balance=500,reserved=0,available=500))
        with fx.ledger.transaction(self.tenant) as conn:
            accrual=conn.execute("SELECT wallet_id,points,rule_version FROM bonuspro.order_accruals "
                "WHERE tenant_id=%s AND order_id=%s",(self.tenant,fx.order)).fetchone()
            audit=conn.execute("SELECT * FROM bonuspro.order_customer_rebindings "
                "WHERE tenant_id=%s AND order_id=%s",(self.tenant,fx.order)).fetchall()
        self.assertEqual((str(accrual["wallet_id"]),accrual["points"],accrual["rule_version"]),(new_wallet,500,1))
        self.assertEqual(len(audit),1)
        self.assertEqual(str(audit[0]["old_counterparty_id"]),fx.customer)
        self.assertEqual(str(audit[0]["new_counterparty_id"]),customer)
        self.assertEqual(str(audit[0]["actor_id"]),fx.actor)

    def test_first_preview_binding_rejects_older_worker_buyer_without_any_audit(self):
        fx=self.fx
        old_funds=fx.ledger.snapshot(self.tenant,fx.wallet)
        fx.order=str(uuid4())
        old=replace(fx.saved,snapshot=replace(fx.saved.snapshot,order_id=fx.order,
            revision="2026-10-19 10:00:00.000",
            paid_minor=fx.saved.snapshot.total_minor,shipped_minor=fx.saved.snapshot.total_minor))
        fx.saved=old
        buyer=str(uuid4())
        newer=replace(old.snapshot,counterparty_id=buyer,revision="2026-10-19 10:01:00.000")
        quote=fx.checkout.preview(self.tenant,newer,requested_points=0,actor_id=fx.actor,
            eligibility_version=1,read_only_reason="order_fulfilled")
        before=self.current_context()
        self.test.run_proof(self.test.proof(saved=old))
        self.assertEqual(self.test.outcome(),"document_review")
        self.assertEqual(self.current_context(),before)
        self.assertEqual(str(before["counterparty_id"]),buyer)
        self.assertEqual(before["binding_revision"],newer.revision)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),old_funds)
        self.assertEqual(fx.ledger.snapshot(self.tenant,quote["wallet_id"]),dict(balance=0,reserved=0,available=0))
        with fx.ledger.transaction(self.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",
                                          (self.tenant,fx.order)).fetchone())
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                "WHERE tenant_id=%s AND order_id=%s",(self.tenant,fx.order)).fetchone()["n"],0)

    def test_old_worker_proof_cannot_reverse_a_newer_binding_and_fresh_edit_can_continue(self):
        fx=self.fx
        customer=str(uuid4())
        newer=replace(fx.saved.snapshot,counterparty_id=customer,revision="2026-10-19 12:00:00.000")
        quote=fx.checkout.preview(self.tenant,newer,requested_points=0,actor_id=fx.actor,eligibility_version=1)
        new_wallet=quote["wallet_id"]
        old_funds=fx.ledger.snapshot(self.tenant,fx.wallet)
        old_saved=replace(fx.saved,snapshot=replace(fx.saved.snapshot,revision="2026-10-19 11:00:00.000",
            paid_minor=fx.saved.snapshot.total_minor,shipped_minor=fx.saved.snapshot.total_minor))
        self.test.run_proof(self.test.proof(saved=old_saved))
        self.assertEqual(self.test.outcome(),"document_review")
        self.assertEqual(str(self.current_context()["counterparty_id"]),customer)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),old_funds)
        self.assertEqual(fx.ledger.snapshot(self.tenant,new_wallet)["balance"],0)
        with fx.ledger.transaction(self.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",
                                          (self.tenant,fx.order)).fetchone())
        # A genuinely later saved buyer change can still be reconciled automatically.
        latest_customer=str(uuid4())
        latest=self.changed_saved(latest_customer)
        latest=replace(latest,snapshot=replace(latest.snapshot,revision="2026-10-19 13:00:00.000"))
        self.test.run_proof(self.test.proof(saved=latest))
        self.assertEqual(self.test.outcome(),"credited")
        context=self.current_context()
        self.assertEqual(str(context["counterparty_id"]),latest_customer)
        self.assertEqual(fx.ledger.snapshot(self.tenant,str(context["wallet_id"]))["balance"],500)
        self.assertEqual(fx.ledger.snapshot(self.tenant,new_wallet)["balance"],0)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),old_funds)
        with fx.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                "WHERE tenant_id=%s AND order_id=%s",(self.tenant,fx.order)).fetchone()["n"],2)

    def test_rejected_rebind_rolls_back_destination_wallet_and_keeps_reservation(self):
        fx=self.fx
        original=self.current_context()
        customer=str(uuid4())
        rid=fx.ledger.reserve(self.tenant,fx.wallet,order_id=fx.order,points=1,eligible_minor=1000000,
            rules=Rules(),quote_hash="a"*64,key=str(uuid4()),actor_id=fx.actor)
        funds=fx.ledger.snapshot(self.tenant,fx.wallet)
        wallets=self.wallet_count()
        self.test.run_proof(self.test.proof(saved=self.changed_saved(customer)))
        self.assertEqual(self.test.outcome(),"document_review")
        self.assertEqual(self.current_context(),original)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),funds)
        self.assert_no_new_identity(customer,wallets)
        with fx.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s",
                                          (self.tenant,rid)).fetchone()["state"],"held")
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",
                                           (self.tenant,fx.order)).fetchone())

    def test_credited_customer_change_remains_review_without_rebinding_history(self):
        self.test.run_proof()
        fx=self.fx
        original=self.current_context()
        funds=fx.ledger.snapshot(self.tenant,fx.wallet)
        customer=str(uuid4())
        wallets=self.wallet_count()
        self.test.run_proof(self.test.proof(saved=self.changed_saved(customer)))
        self.assertEqual(self.test.outcome(),"document_review")
        self.assertEqual(self.current_context(),original)
        self.assertEqual(fx.ledger.snapshot(self.tenant,fx.wallet),funds)
        self.assert_no_new_identity(customer,wallets)

    def test_existing_redemption_plan_keeps_its_original_customer_and_reserve(self):
        self.fx.prepare()
        original=self.current_context()
        customer=str(uuid4())
        funds=self.fx.ledger.snapshot(self.tenant,self.fx.wallet)
        wallets=self.wallet_count()
        self.test.run_proof(self.test.proof(saved=self.changed_saved(customer)))
        self.assertEqual(self.test.outcome(),"redemption_pending")
        self.assertEqual(self.current_context(),original)
        self.assertEqual(self.fx.ledger.snapshot(self.tenant,self.fx.wallet),funds)
        self.assert_no_new_identity(customer,wallets)


if __name__=="__main__":unittest.main()
