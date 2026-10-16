import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, NotFound, VerifiedDiscount
from bonuspro.adapters.postgres.returns import Returns
from bonuspro.domain.finance import Rules


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.wallet, self.actor = (str(uuid4()) for _ in range(3))
        self.ledger = Ledger(os.environ["TEST_APP_DSN"])
        self.returns = Returns(self.ledger)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            self.assertEqual(conn.info.dbname, "bonuspro_ci")
            conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)", (self.tenant,))
        self.ledger.create_wallet(self.tenant, self.wallet)
        self.credit = self.ledger.credit(self.tenant, self.wallet, points=1000, key="initial",
                                        actor_id=self.actor, reason="Synthetic accrual")

    def request(self, points=500, returned=None):
        return self.returns.request(self.tenant, self.credit, return_id=returned or str(uuid4()),
                                    points=points, actor_id=self.actor)

    def reserve(self, points=700):
        order = str(uuid4())
        rid = self.ledger.reserve(self.tenant, self.wallet, order_id=order, points=points,
                                  eligible_minor=1_000_000, rules=Rules(), quote_hash="b"*64,
                                  key=str(uuid4()), actor_id=self.actor)
        return order, rid

    def spend(self, points=700):
        order, rid = self.reserve(points)
        self.ledger.begin_verification(self.tenant, rid)
        self.ledger.confirm_discount(self.tenant, rid, proof=VerifiedDiscount(order,"b"*64,points*100,"v1"))

    def resolve(self, case, decision="waive", role="admin", reason="Administrator reviewed purchase"):
        return self.returns.resolve(self.tenant, case["id"], decision=decision,
                                    actor_id=self.actor, role=role, reason=reason)

    def test_sufficient_balance_reverses_once_with_original_credit_link(self):
        returned = str(uuid4())
        case = self.request(returned=returned)
        self.assertEqual(case["state"], "reversed")
        self.assertEqual(case, self.request(returned=returned))
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],500)
        with self.ledger.transaction(self.tenant) as conn:
            tx=conn.execute("SELECT * FROM bonuspro.ledger_transactions WHERE id=%s",
                            (case["transaction_id"],)).fetchone()
            self.assertEqual(str(tx["credit_ref_id"]),self.credit)
            self.assertEqual(tx["delta"],-500)

    def test_insufficient_balance_creates_review_without_debt_and_blocks_new_spend(self):
        self.spend()
        case=self.request(points=1000)
        self.assertEqual(case["state"],"pending")
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],300)
        with self.assertRaises(Conflict):
            self.reserve(100)
        with self.assertRaises(Conflict):
            self.resolve(case,decision="reverse")
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],300)

    def test_reserved_funds_are_not_taken_by_return(self):
        order,rid=self.reserve()
        case=self.request(points=500)
        self.assertEqual(case["state"],"pending")
        self.ledger.begin_verification(self.tenant,rid)
        self.ledger.confirm_discount(self.tenant,rid,proof=VerifiedDiscount(order,"b"*64,70000,"v1"))
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],300)

    def test_admin_waiver_keeps_history_and_unblocks_wallet(self):
        self.spend()
        case=self.request(points=500)
        result=self.resolve(case)
        self.assertEqual(result["state"],"waived")
        self.assertEqual(result,self.resolve(case))
        self.reserve(100)
        with self.assertRaises(Conflict):
            self.resolve(case,decision="reverse")

    def test_admin_can_reverse_after_balance_is_restored(self):
        self.spend()
        case=self.request(points=500)
        self.ledger.credit(self.tenant,self.wallet,points=300,key="later",actor_id=self.actor,reason="Later purchase")
        result=self.resolve(case,decision="reverse")
        self.assertEqual(result["state"],"reversed")
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],100)
        self.assertEqual(result,self.resolve(case,decision="reverse"))

    def test_staff_cannot_resolve_or_use_blank_reason(self):
        self.spend()
        case=self.request()
        for role in ("cashier","worker","individual"):
            with self.assertRaises(PermissionError):
                self.resolve(case,role=role)
        with self.assertRaises(ValueError):
            self.resolve(case,reason=" ")

    def test_partial_returns_cannot_exceed_original_even_after_waiver(self):
        self.spend()
        case=self.request(points=800)
        self.resolve(case)
        with self.assertRaises(Conflict):
            self.request(points=201)
        self.assertEqual(self.request(points=200)["state"],"reversed")

    def test_concurrent_duplicate_reversal_debits_once(self):
        returned=str(uuid4())
        barrier=Barrier(2)
        def attempt(_):
            barrier.wait(timeout=10)
            return self.request(returned=returned)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],500)

    def test_concurrent_returns_cannot_exceed_original_credit(self):
        barrier=Barrier(2)
        def attempt(_):
            barrier.wait(timeout=10)
            try:
                return self.request(points=700)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_same_return_changed_amount_is_rejected(self):
        returned=str(uuid4())
        self.request(returned=returned)
        with self.assertRaises(Conflict):
            self.request(returned=returned,points=501)

    def test_foreign_tenant_cannot_reverse_or_resolve(self):
        case=self.request()
        foreign=str(uuid4())
        with self.assertRaises(NotFound):
            self.returns.request(foreign,self.credit,return_id=str(uuid4()),points=100,actor_id=self.actor)
        with self.assertRaises(NotFound):
            self.returns.resolve(foreign,case["id"],decision="waive",role="admin",actor_id=self.actor,reason="Test")

    def test_pending_and_resolved_cases_are_immutable_except_resolution(self):
        self.spend()
        case=self.request()
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE bonuspro.return_cases SET points=1 WHERE tenant_id=%s AND id=%s",
                             (self.tenant,case["id"]))
        self.resolve(case)
        with self.ledger.transaction(self.tenant) as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE bonuspro.return_cases SET state='pending' WHERE id=%s",(case["id"],))

    def test_reversal_without_matching_case_is_rejected(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.transaction(self.tenant) as conn:
                self.ledger._post(conn,self.tenant,self.wallet,"forged","f"*64,-100,"reversal",
                                  self.actor,"Synthetic invalid reversal",credit_ref=self.credit)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],1000)
