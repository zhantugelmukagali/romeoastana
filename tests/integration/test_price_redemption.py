"""Integral-price redemption through provider JSON, PostgreSQL proof, returns and voids."""
import os
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import test_widget_provider_contract as provider_fixture
from bonuspro.adapters.moysklad.fulfillment import Fulfillment
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.postgres.voids import Voids


class PriceRedemptionTests(unittest.TestCase):
    def setUp(self):
        self.widget = provider_fixture.WidgetProviderContractTests()
        self.widget.setUp()
        self.addCleanup(self.widget.doCleanups)
        self.fx = self.widget.fx

    def order(self, ordinary_bps=0):
        fx = self.fx
        goods = replace(fx.saved.lines[0], quantity=Decimal(1),
                        price=Decimal(9999000).normalize(), discount_bps=ordinary_bps)
        delivery = replace(fx.saved.lines[1],
            quantity=fx.saved.lines[1].quantity.normalize(),
            price=fx.saved.lines[1].price.normalize(), vat=fx.saved.lines[1].vat.normalize())
        fx.saved = replace(fx.saved, lines=(goods, delivery),
            snapshot=replace(fx.saved.snapshot, eligible_minor=goods.amount,
                             total_minor=goods.amount+delivery.amount,
                             revision="2026-10-19 10:01:00.000"))
        return goods, delivery

    def prepare(self, ordinary_bps=0):
        goods, delivery = self.order(ordinary_bps)
        quote = self.widget.client.get(self.widget.path, headers=self.widget.headers)
        self.assertEqual(quote.status_code, 200, quote.text)
        response = self.widget.client.post(self.widget.path+"/prepare",
            headers=self.widget.headers,
            json=dict(points=5000, request_id=str(uuid4()),
                      source_hash=quote.json()["saved_source_hash"]))
        self.assertEqual(response.status_code, 200, response.text)
        prepared = response.json()
        expected_price = 9499000 if ordinary_bps == 0 else 9443444
        self.assertEqual(prepared["update_state"]["positions"],
                         [{"id": goods.id, "price": expected_price}, {"id": delivery.id}])
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            row = conn.execute("SELECT plan FROM bonuspro.redemption_attempts "
                               "WHERE tenant_id=%s AND id=%s",
                               (self.fx.tenant, prepared["id"])).fetchone()
        before = {line["id"]: line for line in row["plan"]["before"]["lines"]}
        after = {line["id"]: line for line in row["plan"]["after"]["lines"]}
        self.assertEqual(before[goods.id]["price"], str(goods.price))
        self.assertEqual(after[goods.id]["price"], str(Decimal(expected_price).normalize()))
        self.assertEqual(after[goods.id]["discount_bps"], ordinary_bps)
        self.assertEqual(after[delivery.id], before[delivery.id])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant, self.fx.wallet),
                         dict(balance=5000, reserved=5000, available=0))
        return prepared, goods, delivery

    def persist(self, prepared):
        """Apply only the host update fields, then expose the saved provider document."""
        fx = self.fx
        updates = {line["id"]: line for line in prepared["update_state"]["positions"]}
        lines = []
        for line in fx.saved.lines:
            update = updates[line.id]
            discount = line.discount_bps
            if "discount" in update:
                discount = int(Decimal(str(update["discount"]))*100)
            lines.append(replace(line,
                price=Decimal(str(update.get("price", line.price))).normalize(),
                discount_bps=discount))
        marker = prepared["update_state"]["attributes"][0]["value"]
        fx.saved = replace(fx.saved, lines=tuple(lines), marker=marker,
            snapshot=replace(fx.saved.snapshot,
                eligible_minor=sum(line.amount for line in lines if line.kind != "service"),
                total_minor=sum(line.amount for line in lines),
                revision="2026-10-19 10:02:00.000"))
        return fx.saved

    def verify(self):
        return self.widget.client.post(self.widget.path+"/verify", headers=self.widget.headers)

    def test_integral_price_transport_saves_exact_plan_and_debits_once(self):
        prepared, goods, delivery = self.prepare()
        saved = self.persist(prepared)
        self.assertEqual(saved.lines[1], delivery)
        self.assertEqual(saved.lines[0].discount_bps, goods.discount_bps)
        self.assertEqual(saved.snapshot.eligible_minor, 9499000)
        response = self.verify()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["verified"])
        transaction = response.json()["transaction_id"]
        repeated = self.verify()
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["transaction_id"], transaction)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant, self.fx.wallet),
                         dict(balance=0, reserved=0, available=0))
        self.assertTrue(all(request.method == "GET" for request in self.widget.calls))

    def test_integral_price_keeps_existing_ordinary_discount_and_delivery(self):
        prepared, goods, delivery = self.prepare(ordinary_bps=1000)
        self.assertEqual(goods.amount, 8999100)
        saved = self.persist(prepared)
        self.assertEqual(saved.lines[0].price, Decimal(9443444))
        self.assertEqual(saved.lines[0].discount_bps, 1000)
        self.assertEqual(saved.snapshot.eligible_minor, 8499100)
        self.assertEqual(saved.lines[1], delivery)
        response = self.verify()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant, self.fx.wallet)["balance"], 0)

    def test_changed_saved_price_cannot_confirm_even_with_the_right_marker(self):
        prepared, _, _ = self.prepare()
        exact = self.persist(prepared)
        changed = replace(exact.lines[0], price=(exact.lines[0].price+1).normalize())
        self.fx.saved = replace(exact, lines=(changed, exact.lines[1]),
            snapshot=replace(exact.snapshot, eligible_minor=changed.amount,
                             total_minor=changed.amount+exact.lines[1].amount,
                             revision="2026-10-19 10:03:00.000"))
        rejected = self.verify()
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertFalse(rejected.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant, self.fx.wallet),
                         dict(balance=5000, reserved=5000, available=0))
        self.fx.saved = replace(exact,snapshot=replace(exact.snapshot,revision="2026-10-19 10:04:00.000"))
        confirmed = self.verify()
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["verified"])

    def test_accrual_and_partial_full_returns_use_original_goods_base(self):
        prepared, _, _ = self.prepare()
        self.persist(prepared)
        confirmed = self.verify()
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["verified"])
        fx = self.fx
        automation = Automation(fx.ledger)
        automation.configure(fx.tenant, fx.actor)
        created = datetime.now(timezone.utc)+timedelta(seconds=1)

        def process(quantity=None):
            returned = () if quantity is None else (
                replace(fx.saved.lines[0], id=str(uuid4()), quantity=Decimal(str(quantity))),)
            proof = Fulfillment(fx.saved, created, fx.saved.snapshot.total_minor,
                fx.saved.snapshot.total_minor, "return_review" if returned else "eligible",
                "a"*64, time.monotonic()+120, returned=returned,
                full_payment=True, full_shipment=True)
            automation.jobs.enqueue(fx.tenant, order_id=fx.order, key=str(uuid4()))
            job = automation.jobs.claim(fx.tenant, lease_seconds=300)
            self.assertIsNotNone(job)
            automation.complete(fx.tenant, job, proof)

        process()
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet)["balance"], 4999)
        with fx.ledger.transaction(fx.tenant) as conn:
            accrual = conn.execute("SELECT points,eligible_minor FROM bonuspro.order_accruals "
                                  "WHERE tenant_id=%s AND order_id=%s",
                                  (fx.tenant, fx.order)).fetchone()
        self.assertEqual((accrual["points"], accrual["eligible_minor"]), (4999, 9999000))
        process("0.5")
        process("0.5")  # Re-reading the same cumulative return cannot refund again.
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet)["balance"], 4999)
        with fx.ledger.transaction(fx.tenant) as conn:
            half = conn.execute("SELECT target_earn,target_refund,earn_delta,refund_delta "
                               "FROM bonuspro.settlement_events WHERE tenant_id=%s AND order_id=%s",
                               (fx.tenant, fx.order)).fetchall()
        self.assertEqual(len(half), 1)
        self.assertEqual((half[0]["target_earn"], half[0]["target_refund"],
                          half[0]["earn_delta"], half[0]["refund_delta"]),
                         (2499, 2500, -2500, 2500))
        process(1)
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet),
                         dict(balance=5000, reserved=0, available=5000))
        with fx.ledger.transaction(fx.tenant) as conn:
            totals = conn.execute("SELECT sum(earn_delta) earn,sum(refund_delta) refund "
                                 "FROM bonuspro.settlement_events WHERE tenant_id=%s AND order_id=%s",
                                 (fx.tenant, fx.order)).fetchone()
        self.assertEqual((int(totals["earn"]), int(totals["refund"])), (-4999, 5000))

    def test_void_requires_original_unit_price_after_marker_is_removed(self):
        prepared, _, _ = self.prepare()
        original = self.fx.saved
        discounted = self.persist(prepared)
        fx = self.fx
        # Synthetic CI clock fixture only; runtime permissions cannot age an attempt.
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            assert conn.info.dbname == "bonuspro_ci"
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute("UPDATE bonuspro.reservations SET created_at=clock_timestamp()-interval '16 minutes' "
                         "WHERE tenant_id=%s AND id=%s", (fx.tenant, prepared["id"]))
        service = Voids(fx.ledger)

        def proof(saved):
            cancelled = replace(saved, marker=None,
                snapshot=replace(saved.snapshot, cancelled=True, paid_minor=0, shipped_minor=0))
            return Fulfillment(cancelled, datetime.now(timezone.utc), 0, 0,
                "cancelled", "b"*64, time.monotonic()+120, cancel_safe=True)

        with self.assertRaises(Conflict):
            service.release(fx.tenant, prepared["id"], proof(discounted), actor=fx.actor,
                            reason="Cancelled; original price not restored")
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet)["reserved"], 5000)
        result = service.release(fx.tenant, prepared["id"], proof(original), actor=fx.actor,
                                 reason="Cancelled; original price restored and tabs closed")
        self.assertEqual(result, {"state": "released"})
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet),
                         dict(balance=5000, reserved=0, available=5000))


if __name__ == "__main__":
    unittest.main()
