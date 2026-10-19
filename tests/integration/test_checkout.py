import os
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import unittest
import psycopg
from bonuspro.adapters.postgres.checkout import Checkout, OrderSnapshot
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, VerifiedDiscount
from bonuspro.adapters.postgres.returns import Returns
from bonuspro.domain.finance import Rules


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.customer,self.order = (str(uuid4()) for _ in range(4))
        self.ledger=Ledger(os.environ["TEST_APP_DSN"])
        self.checkout=Checkout(self.ledger)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            assert conn.info.dbname=="bonuspro_ci"
            conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)",(self.tenant,))
        self.doc=OrderSnapshot(self.tenant,self.order,self.customer,"revision-1","KZT",1_100_000,1_000_000)

    def save(self, expected=0, earn=500, request=None):
        return self.checkout.save_rules(self.tenant,earn_bps=earn,spend_bps=3000,delivery_policy="exclude",
            expected_version=expected,request_id=request or str(uuid4()),actor_id=self.actor,reason="Approved rules")

    def preview(self, doc=None, points=0, version=1):
        return self.checkout.preview(self.tenant,doc or self.doc,requested_points=points,
                                     actor_id=self.actor,eligibility_version=version)

    def credit(self,wallet,points=5000):
        return self.ledger.credit(self.tenant,wallet,points=points,key=str(uuid4()),
                                  actor_id=self.actor,reason="Synthetic test credit")

    def test_no_implicit_configuration_or_wallet(self):
        self.assertIsNone(self.checkout.rules(self.tenant))
        with self.assertRaises(Conflict):self.preview()
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"],0)

    def test_rules_retry_and_immutable_history(self):
        key=str(uuid4())
        one=self.save(request=key)
        self.assertEqual(one,self.save(request=key))
        with self.assertRaises(Conflict):self.save(request=key,earn=1000)
        self.save(expected=1,earn=1000)
        self.assertEqual(self.checkout.rules(self.tenant)["version"],2)
        self.assertEqual([r["earn_bps"] for r in self.checkout.history(self.tenant)["items"]],[1000,500])
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
                conn.execute("UPDATE bonuspro.rule_versions SET earn_bps=0 WHERE tenant_id=%s",(self.tenant,))

    def test_concurrent_settings_change_has_one_winner(self):
        self.save();barrier=Barrier(2)
        def update(i):
            barrier.wait(timeout=10)
            try:return self.save(expected=1,earn=600+i)
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(update,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.checkout.rules(self.tenant)["version"],2)

    def test_old_order_pins_rules_new_order_uses_latest(self):
        self.save();first=self.preview()
        self.save(expected=1,earn=1000)
        self.assertEqual(self.preview()["potential_earn_points"],500)
        with self.assertRaises(Conflict):self.preview(version=2)
        newer=self.preview(doc=replace(self.doc,order_id=str(uuid4())),version=2)
        self.assertEqual(newer["potential_earn_points"],1000)
        self.assertEqual(newer["wallet_id"],first["wallet_id"])

    def test_goods_base_excludes_delivery_and_does_not_debit(self):
        self.save();wallet=self.preview()["wallet_id"];self.credit(wallet)
        result=self.preview(points=3000)
        self.assertEqual((result["max_spend_points"],result["payable_minor"],result["potential_earn_points"]),(3000,800000,500))
        self.assertEqual(self.ledger.snapshot(self.tenant,wallet),{"balance":5000,"reserved":0,"available":5000})
        with self.assertRaises(Conflict):self.preview(points=3001)

    def test_existing_reserves_reduce_available_amount(self):
        self.save();wallet=self.preview()["wallet_id"];self.credit(wallet,1000)
        self.ledger.reserve(self.tenant,wallet,order_id=str(uuid4()),points=800,eligible_minor=1000000,
                            rules=Rules(),quote_hash="a"*64,key="other",actor_id=self.actor)
        self.assertEqual(self.preview()["max_spend_points"],200)
        with self.assertRaises(Conflict):self.preview(points=201)

    def test_order_reservation_blocks_second_redemption(self):
        self.save();wallet=self.preview()["wallet_id"];self.credit(wallet)
        self.ledger.reserve(self.tenant,wallet,order_id=self.order,points=500,eligible_minor=1000000,
                            rules=Rules(),quote_hash="a"*64,key="same",actor_id=self.actor)
        result=self.preview()
        self.assertEqual((result["max_spend_points"],result["blocked_reason"]),(0,"order_has_redemption"))

    def test_return_review_blocks_preview_redemption(self):
        self.save();wallet=self.preview()["wallet_id"];credit=self.credit(wallet,1000)
        order=str(uuid4())
        rid=self.ledger.reserve(self.tenant,wallet,order_id=order,points=700,eligible_minor=1000000,
                               rules=Rules(),quote_hash="a"*64,key="spent",actor_id=self.actor)
        self.ledger.begin_verification(self.tenant,rid)
        self.ledger.confirm_discount(self.tenant,rid,proof=VerifiedDiscount(order,"a"*64,70000,"saved"))
        Returns(self.ledger).request(self.tenant,credit,return_id=str(uuid4()),points=1000,actor_id=self.actor)
        result=self.preview()
        self.assertEqual((result["available"],result["max_spend_points"],result["blocked_reason"]),(300,0,"return_review"))

    def test_paid_shipped_cancelled_and_foreign_currency_are_rejected(self):
        self.save()
        for patch in (dict(paid_minor=1),dict(shipped_minor=1),dict(cancelled=True),dict(currency="RUB")):
            with self.subTest(patch=patch),self.assertRaises(Conflict):self.preview(doc=replace(self.doc,**patch))
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.order_contexts").fetchone()["n"],0)

    def test_changed_customer_requires_reconciliation(self):
        self.save();wallet=self.preview()["wallet_id"];self.credit(wallet)
        with self.assertRaises(Conflict):self.preview(doc=replace(self.doc,counterparty_id=str(uuid4())))
        self.assertEqual(self.ledger.snapshot(self.tenant,wallet)["balance"],5000)

    def test_tenant_isolation_and_foreign_source_rejection(self):
        self.save();self.preview();other=str(uuid4())
        self.assertIsNone(self.checkout.rules(other))
        with self.assertRaises(Conflict):self.preview(doc=replace(self.doc,tenant_id=other))
        with self.ledger.transaction(other) as conn:
            for table in ("rule_versions","customer_wallets","order_contexts"):
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro."+table).fetchone()["n"],0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.ledger.transaction(other) as conn:
                conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                             (self.tenant,uuid4(),uuid4()))

    def test_concurrent_first_orders_share_one_customer_wallet(self):
        self.save();barrier=Barrier(2)
        def create(i):
            barrier.wait(timeout=10)
            return self.preview(doc=replace(self.doc,order_id=str(uuid4())))
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(create,range(2)))
        self.assertEqual(results[0]["wallet_id"],results[1]["wallet_id"])

    def test_changed_source_changes_quote_hash(self):
        self.save();one=self.preview()
        two=self.preview(doc=replace(self.doc,revision="revision-2",total_minor=1200000,eligible_minor=1100000))
        self.assertNotEqual(one["quote_hash"],two["quote_hash"])
        self.assertEqual(two["potential_earn_points"],550)

    def test_failed_preview_leaves_no_partial_binding(self):
        self.save()
        with self.assertRaises(Conflict):self.preview(points=1)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.customer_wallets").fetchone()["n"],0)

    def test_malformed_money_is_rejected_before_database(self):
        for patch in (dict(total_minor=True),dict(eligible_minor=1.5),dict(total_minor=-1),
                      dict(eligible_minor=1200000),dict(cancelled="false")):
            with self.subTest(patch=patch),self.assertRaises(ValueError):replace(self.doc,**patch)

    def test_rule_input_does_not_enable_delivery_or_float_percent(self):
        for patch in (dict(earn_bps=True),dict(earn_bps=500.0),dict(spend_bps=10001),dict(delivery_policy="include")):
            body=dict(earn_bps=500,spend_bps=3000,delivery_policy="exclude",expected_version=0,
                      request_id=str(uuid4()),actor_id=self.actor,reason="Test")
            body.update(patch)
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                self.checkout.save_rules(self.tenant,**body)

    def test_read_only_preview_never_accepts_points_or_an_untrusted_reason(self):
        self.save()
        doc=replace(self.doc,paid_minor=1)
        result=self.checkout.preview(self.tenant,doc,requested_points=0,actor_id=self.actor,
                                     eligibility_version=1,read_only_reason="order_fulfilled")
        self.assertEqual(result["max_spend_points"],0)
        self.assertEqual(result["blocked_reason"],"order_fulfilled")
        with self.assertRaises(Conflict):
            self.checkout.preview(self.tenant,doc,requested_points=1,actor_id=self.actor,
                                  eligibility_version=1,read_only_reason="order_fulfilled")
        with self.assertRaises(ValueError):
            self.checkout.preview(self.tenant,doc,requested_points=0,actor_id=self.actor,
                                  eligibility_version=1,read_only_reason="untrusted")

if __name__=="__main__":unittest.main()
