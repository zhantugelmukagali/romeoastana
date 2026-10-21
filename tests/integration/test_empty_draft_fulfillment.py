"""Real REMAP transport -> fulfillment -> worker/PG zero-empty shipment regressions."""
import copy
import unittest
from datetime import timedelta
from uuid import uuid4

import httpx
import test_automation as fixtures
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader, MSK
from bonuspro.adapters.moysklad.remap import BASE, InvalidDocument, RemapClient, RetryLater
from bonuspro.worker import Worker


def ref(kind, identity):
    return {"meta": {"type": kind, "href": BASE + "/entity/" + kind + "/" + identity}}


class EmptyDraftFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AutomationTests()
        self.base.setUp()
        self.fx, self.auto = self.base.fx, self.base.a
        self.tenant, self.order = self.fx.tenant, self.fx.order
        self.currency, self.shipment, self.payment, self.empty = (str(uuid4()) for _ in range(4))
        cutoff = self.auto.state(self.tenant)["starts_at"]
        stamp = (cutoff + timedelta(seconds=1)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        saved = self.fx.saved
        self.order_doc = dict(
            id=self.order, accountId=self.tenant, meta=ref("customerorder", self.order)["meta"],
            applicable=True, sum=saved.snapshot.total_minor, payedSum=saved.snapshot.total_minor,
            shippedSum=saved.snapshot.total_minor, created=stamp, updated=stamp,
            agent=ref("counterparty", self.fx.customer),
            organization=ref("organization", saved.organization_id),
            rate={"currency": ref("currency", self.currency)},
            vatEnabled=False, vatIncluded=True, attributes=[], name="zero-empty fixture",
            demands=[ref("demand", self.shipment)], payments=[ref("paymentin", self.payment)],
        )
        ship = {**copy.deepcopy(self.order_doc), "id": self.shipment,
                "meta": ref("demand", self.shipment)["meta"],
                "customerOrder": ref("customerorder", self.order), "payments": [], "returns": []}
        pay = {**copy.deepcopy(self.order_doc), "id": self.payment,
               "meta": ref("paymentin", self.payment)["meta"],
               "operations": [{**ref("customerorder", self.order),
                               "linkedSum": saved.snapshot.total_minor}]}
        self.docs = {("customerorder", self.order): self.order_doc,
                     ("demand", self.shipment): ship, ("paymentin", self.payment): pay}
        rows = [dict(id=line.id, accountId=self.tenant,
                     assortment=ref(line.kind, line.assortment), quantity=int(line.quantity),
                     price=int(line.price), discount=line.discount_bps / 100,
                     vat=int(line.vat), vatEnabled=line.vat_enabled) for line in saved.lines]
        shipped_rows = copy.deepcopy(rows)
        for row in shipped_rows:
            row["id"] = str(uuid4())
        self.rows = {("customerorder", self.order): rows,
                     ("demand", self.shipment): shipped_rows}
        self.requests, self.bad_positions = [], set()
        self.order_reads, self.on_order = 0, None

    def add_empty(self, *, posted=True):
        empty = {**copy.deepcopy(self.order_doc), "id": self.empty,
                 "meta": ref("demand", self.empty)["meta"], "applicable": posted,
                 "sum": 0, "payedSum": 0, "shippedSum": 0,
                 "customerOrder": ref("customerorder", self.order), "payments": [], "returns": []}
        self.docs[("demand", self.empty)] = empty
        self.rows[("demand", self.empty)] = []
        self.order_doc["demands"] = [ref("demand", self.shipment), ref("demand", self.empty)]
        return empty

    def provider(self, request):
        self.assertEqual(request.method, "GET")
        self.requests.append(request)
        prefix = "/api/remap/1.2/entity/"
        self.assertTrue(request.url.path.startswith(prefix))
        parts = request.url.path[len(prefix):].split("/")
        if len(parts) == 1:
            # Real worker discovery/stream requests have no new documents.
            return httpx.Response(200, json={"meta": {"size": 0}, "rows": []})
        kind, identity = parts[:2]
        if len(parts) == 3 and parts[2] == "positions":
            rows = self.rows[(kind, identity)]
            return httpx.Response(200, json={"meta": {"size": len(rows)},
                "rows": None if (kind, identity) in self.bad_positions else copy.deepcopy(rows)})
        self.assertEqual(len(parts), 2)
        if kind == "currency":
            self.assertEqual(identity, self.currency)
            return httpx.Response(200, json=dict(id=self.currency,
                meta=ref("currency", self.currency)["meta"], isoCode="KZT", code="398",
                default=True, rate=1, multiplicity=1, indirect=False, archived=False, system=True))
        if (kind, identity) == ("customerorder", self.order):
            self.order_reads += 1
            if self.on_order:
                self.on_order(self.order_reads)
        return httpx.Response(200, json=copy.deepcopy(self.docs[(kind, identity)]))

    def client(self, token=None, tenant=None, **options):
        options["minimum_interval"] = 0
        return RemapClient(token or "synthetic", tenant or self.tenant,
            transport=httpx.MockTransport(self.provider), **options)

    def read(self):
        return FulfillmentReader(self.client()).read(self.order, marker_id=self.fx.marker)

    def cycle(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.auto.hint_in(conn, self.tenant, self.order)
        return Worker(self.fx.ledger, self.fx.install, self.tenant,
                      client_factory=self.client).cycle(max_jobs=1)

    def state(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT outcome FROM bonuspro.order_monitors WHERE order_id=%s",
                                (self.order,)).fetchone()["outcome"]

    def financial_state(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return {table: conn.execute(
                "SELECT to_jsonb(t)::text AS row FROM bonuspro." + table +
                " t ORDER BY to_jsonb(t)::text").fetchall()
                for table in ("ledger_transactions", "ledger_postings", "order_accruals",
                              "reservations", "redemption_attempts", "personal_discount_applications",
                              "settlement_events", "settlement_cases")}

    def issues(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT id,reason,resolved_at FROM bonuspro.reconciliation_issues "
                                "WHERE order_id=%s ORDER BY id", (self.order,)).fetchall()

    def credit_once(self):
        self.assertEqual(self.cycle(), "processed")
        self.assertEqual(self.state(), "credited")
        self.assertEqual(self.fx.ledger.snapshot(self.tenant, self.fx.wallet),
                         dict(balance=5500, reserved=0, available=5500))
        with self.fx.ledger.transaction(self.tenant) as conn:
            row = conn.execute("SELECT points,eligible_minor FROM bonuspro.order_accruals "
                               "WHERE order_id=%s", (self.order,)).fetchone()
        self.assertEqual(row, dict(points=500, eligible_minor=1000000))
        self.assertEqual(self.issues(), [])

    def assert_review_without_financial_change(self, before):
        with self.assertRaises(InvalidDocument):
            self.read()
        self.assertEqual(self.cycle(), "processed")
        self.assertEqual(self.state(), "provider_error")
        self.assertEqual(self.financial_state(), before)
        self.assertEqual(self.fx.ledger.snapshot(self.tenant, self.fx.wallet),
                         dict(balance=5500, reserved=0, available=5500))
        self.assertTrue(self.issues())
        self.assertTrue(all(row["resolved_at"] is None for row in self.issues()))

    def test_extra_posted_and_unposted_zero_empty_demands_preserve_one_accrual(self):
        self.credit_once()
        before = self.financial_state()
        for posted in (True, False):
            with self.subTest(posted=posted):
                self.add_empty(posted=posted)
                proof = self.read()
                self.assertEqual((proof.outcome, proof.paid_minor, proof.shipped_minor),
                                 ("eligible", 1200000, 1200000))
                self.assertTrue(proof.full_payment and proof.full_shipment)
                self.assertIn(("demand", self.empty), proof.related)
                self.assertFalse(proof.presale_safe)
                self.assertFalse(proof.cancel_safe)
                self.assertEqual(self.cycle(), "processed")
                self.assertEqual(self.state(), "credited")
                self.assertEqual(self.financial_state(), before)
                self.assertEqual(self.issues(), [])
                with self.fx.ledger.transaction(self.tenant) as conn:
                    self.assertIsNotNone(conn.execute(
                        "SELECT 1 FROM bonuspro.related_documents WHERE kind='demand' "
                        "AND document_id=%s AND order_id=%s", (self.empty, self.order)).fetchone())
        self.assertEqual(self.fx.ledger.snapshot(self.tenant, self.fx.wallet),
                         dict(balance=5500, reserved=0, available=5500))

    def test_only_zero_empty_shipment_cannot_create_the_first_accrual(self):
        self.add_empty()
        self.order_doc["demands"] = [ref("demand", self.empty)]
        self.order_doc["shippedSum"] = 0
        before = self.financial_state()
        with self.assertRaises(InvalidDocument):
            self.read()
        self.assertEqual(self.cycle(), "processed")
        self.assertEqual(self.state(), "provider_error")
        self.assertEqual(self.financial_state(), before)
        self.assertEqual(self.fx.ledger.snapshot(self.tenant, self.fx.wallet)["balance"], 5000)
        self.assertEqual(self.issues(), [])

    def test_replacing_the_only_real_shipment_with_zero_empty_never_reverses_prior_earn(self):
        self.credit_once()
        before = self.financial_state()
        self.add_empty()
        self.order_doc["demands"] = [ref("demand", self.empty)]
        self.order_doc["shippedSum"] = 0
        self.assert_review_without_financial_change(before)

    def test_nonzero_or_malformed_empty_demand_still_requires_review(self):
        self.credit_once()
        before = self.financial_state()
        for case in ("nonzero", "boolean_sum", "bad_posting_flag", "bad_positions",
                     "bad_attributes", "bad_revision"):
            with self.subTest(case=case):
                self.bad_positions.clear()
                empty = self.add_empty()
                if case == "nonzero":
                    empty["sum"] = 1
                elif case == "boolean_sum":
                    empty["sum"] = False
                elif case == "bad_posting_flag":
                    empty["applicable"] = "true"
                elif case == "bad_positions":
                    self.bad_positions.add(("demand", self.empty))
                elif case == "bad_attributes":
                    empty["attributes"] = "invalid metadata"
                else:
                    empty["updated"] = ""
                self.assert_review_without_financial_change(before)

    def test_empty_demand_with_posted_payment_or_any_return_is_not_ignored(self):
        self.credit_once()
        before = self.financial_state()
        for allocation in (0, 1, None):
            with self.subTest(allocation=allocation):
                empty = self.add_empty()
                identity = str(uuid4())
                if allocation is None:
                    returned = {**copy.deepcopy(empty), "id": identity,
                                "meta": ref("salesreturn", identity)["meta"],
                                "applicable": False, "demand": ref("demand", self.empty)}
                    self.docs[("salesreturn", identity)] = returned
                    self.rows[("salesreturn", identity)] = []
                    empty["returns"] = [ref("salesreturn", identity)]
                else:
                    payment = {**copy.deepcopy(self.docs[("paymentin", self.payment)]),
                               "id": identity, "meta": ref("paymentin", identity)["meta"],
                               "sum": allocation,
                               "operations": [{**ref("demand", self.empty), "linkedSum": allocation}]}
                    self.docs[("paymentin", identity)] = payment
                    empty["payments"] = [ref("paymentin", identity)]
                self.assert_review_without_financial_change(before)

    def test_zero_empty_demand_keeps_party_currency_and_tax_validation(self):
        self.credit_once()
        before = self.financial_state()
        for field, value in (("customerOrder", ref("customerorder", str(uuid4()))),
                             ("agent", ref("counterparty", str(uuid4()))),
                             ("rate", {"currency": ref("currency", str(uuid4()))}),
                             ("vatEnabled", None), ("vatEnabled", True)):
            with self.subTest(field=field, value=value):
                self.add_empty()[field] = value
                self.assert_review_without_financial_change(before)

    def test_empty_demand_posting_change_between_graph_reads_retries_without_finance(self):
        self.add_empty(posted=False)
        before = self.financial_state()
        def post_on_second_graph(reads):
            if reads == 3:
                self.docs[("demand", self.empty)]["applicable"] = True
        self.on_order = post_on_second_graph
        with self.assertRaises(RetryLater) as conflict:
            self.read()
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.financial_state(), before)
        self.order_reads = 0
        self.docs[("demand", self.empty)]["applicable"] = False
        self.assertEqual(self.cycle(), "conflict")
        self.assertEqual(self.financial_state(), before)
        self.assertEqual(self.issues(), [])
        with self.fx.ledger.transaction(self.tenant) as conn:
            job = conn.execute("SELECT state,last_error FROM bonuspro.jobs WHERE entity_id=%s "
                               "ORDER BY created_at DESC,id DESC LIMIT 1", (self.order,)).fetchone()
        self.assertEqual(job, dict(state="queued", last_error="conflict"))

    def test_benign_empty_demand_does_not_clear_an_existing_issue(self):
        self.credit_once()
        before = self.financial_state()
        self.add_empty()["sum"] = 1
        self.assert_review_without_financial_change(before)
        issues = self.issues()
        self.add_empty()
        self.assertEqual(self.cycle(), "processed")
        self.assertEqual(self.financial_state(), before)
        self.assertEqual(self.issues(), issues)


if __name__ == "__main__":
    unittest.main()
