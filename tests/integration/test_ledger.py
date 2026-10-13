import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, NotFound, VerifiedDiscount
from bonuspro.domain.finance import Rules


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.admin_dsn = os.environ["TEST_ADMIN_DSN"]
        self.app_dsn = os.environ["TEST_APP_DSN"]
        self.tenant, self.wallet, self.actor = (str(uuid4()) for _ in range(3))
        self.ledger = Ledger(self.app_dsn)
        with psycopg.connect(self.admin_dsn) as conn:
            self.assertEqual(conn.info.dbname, "bonuspro_ci")
            conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)", (self.tenant,))
        self.ledger.create_wallet(self.tenant, self.wallet)

    def credit(self, points=1000, key="seed"):
        return self.ledger.credit(self.tenant, self.wallet, points=points, key=key,
                                  actor_id=self.actor, reason="Synthetic test opening balance")

    def reserve(self, points=700, key="reserve", order=None):
        return self.ledger.reserve(self.tenant, self.wallet, order_id=order or str(uuid4()),
                                   points=points, eligible_minor=1_000_000, rules=Rules(),
                                   quote_hash="a"*64, key=key, actor_id=self.actor)

    def test_opening_balance_is_balanced_and_idempotent(self):
        first = self.credit()
        self.assertEqual(first, self.credit())
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet),
                         {"balance": 1000, "reserved": 0, "available": 1000})
        with self.ledger.transaction(self.tenant) as conn:
            row = conn.execute("SELECT count(*) AS n, sum(amount) AS s FROM bonuspro.ledger_postings").fetchone()
            self.assertEqual((row["n"], row["s"]), (2, 0))

    def test_idempotency_conflict_does_not_change_balance(self):
        self.credit()
        with self.assertRaises(Conflict):
            self.credit(points=2000)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 1000)

    def test_reservation_replay_and_changed_payload(self):
        self.credit()
        order = str(uuid4())
        first = self.reserve(order=order)
        self.assertEqual(first, self.reserve(order=order))
        with self.assertRaises(Conflict):
            self.reserve(points=701, order=order)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["reserved"], 700)

    def test_concurrent_reservations_cannot_overspend(self):
        self.credit()
        barrier = Barrier(2)
        def attempt(index):
            barrier.wait(timeout=10)
            try:
                return self.reserve(key=f"concurrent-{index}")
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet),
                         {"balance": 1000, "reserved": 700, "available": 300})

    def test_concurrent_credit_replay_posts_once(self):
        barrier = Barrier(2)
        def attempt(_):
            barrier.wait(timeout=10)
            return self.credit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 1000)

    def test_one_active_reservation_per_order(self):
        self.credit(3000)
        order = str(uuid4())
        self.reserve(order=order)
        with self.assertRaises(Conflict):
            self.reserve(order=order, key="second")
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["reserved"], 700)

    def test_release_before_apply_is_repeatable(self):
        self.credit()
        rid = self.reserve()
        self.ledger.release_unapplied(self.tenant, rid)
        self.ledger.release_unapplied(self.tenant, rid)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["available"], 1000)

    def test_unknown_external_result_keeps_funds_reserved(self):
        self.credit()
        rid = self.reserve()
        self.ledger.begin_verification(self.tenant, rid)
        with self.assertRaises(Conflict):
            self.ledger.release_unapplied(self.tenant, rid)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["reserved"], 700)

    def test_confirm_only_matching_saved_document(self):
        self.credit()
        order = str(uuid4())
        rid = self.reserve(order=order)
        proof = VerifiedDiscount(order, "a"*64, 70_000, "revision-1")
        with self.assertRaises(Conflict):
            self.ledger.confirm_discount(self.tenant, rid, proof=proof)
        self.ledger.begin_verification(self.tenant, rid)
        for bad in (VerifiedDiscount(order, "b"*64, 70_000, "revision-1"),
                    VerifiedDiscount(order, "a"*64, 69_999, "revision-1"),
                    VerifiedDiscount(str(uuid4()), "a"*64, 70_000, "revision-1")):
            with self.assertRaises(Conflict):
                self.ledger.confirm_discount(self.tenant, rid, proof=bad)
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 1000)
        tx = self.ledger.confirm_discount(self.tenant, rid, proof=proof)
        self.assertEqual(tx, self.ledger.confirm_discount(self.tenant, rid, proof=proof))
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet),
                         {"balance": 300, "reserved": 0, "available": 300})

    def test_simultaneous_confirmations_debit_once(self):
        self.credit()
        order = str(uuid4())
        rid = self.reserve(order=order)
        self.ledger.begin_verification(self.tenant, rid)
        proof = VerifiedDiscount(order, "a"*64, 70_000, "revision-1")
        barrier = Barrier(2)
        def attempt(_):
            barrier.wait(timeout=10)
            return self.ledger.confirm_discount(self.tenant, rid, proof=proof)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 300)

    def test_cross_tenant_read_and_write_are_denied(self):
        self.credit()
        other = str(uuid4())
        with psycopg.connect(self.admin_dsn) as conn:
            conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)", (other,))
        with self.ledger.transaction(other) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.ledger_postings").fetchone()["n"], 0)
        with self.assertRaises(NotFound):
            self.ledger.snapshot(other, self.wallet)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.ledger.transaction(other) as conn:
                conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)", (self.tenant, uuid4()))

    def test_missing_tenant_context_is_fail_closed(self):
        self.credit()
        with psycopg.connect(self.app_dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bonuspro.ledger_postings").fetchone()[0], 0)

    def test_context_does_not_leak_across_transactions(self):
        self.credit()
        with psycopg.connect(self.app_dsn, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('bonuspro.tenant_id', %s, true)", (self.tenant,))
                self.assertEqual(conn.execute("SELECT count(*) FROM bonuspro.ledger_postings").fetchone()[0], 2)
            with conn.transaction():
                self.assertEqual(conn.execute("SELECT count(*) FROM bonuspro.ledger_postings").fetchone()[0], 0)

    def test_application_cannot_mutate_or_truncate_ledger(self):
        self.credit()
        for statement in ("DELETE FROM bonuspro.ledger_postings",
                          "UPDATE bonuspro.ledger_transactions SET reason='changed'",
                          "TRUNCATE bonuspro.ledger_postings"):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.ledger.transaction(self.tenant) as conn:
                    conn.execute(statement)

    def test_owner_trigger_also_rejects_journal_mutation(self):
        self.credit()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.admin_dsn) as conn:
                conn.execute("UPDATE bonuspro.ledger_transactions SET reason='changed' WHERE tenant_id=%s",
                             (self.tenant,))

    def test_unbalanced_transaction_rolls_back(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.transaction(self.tenant) as conn:
                conn.execute(
                    "INSERT INTO bonuspro.ledger_transactions "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,kind,delta,actor_id,reason) "
                    "VALUES (%s,%s,%s,'broken',%s,'credit',100,%s,'test')",
                    (self.tenant, uuid4(), self.wallet, "a"*64, self.actor))
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 0)

    def test_committed_reservation_requires_journal(self):
        self.credit()
        rid = self.reserve()
        self.ledger.begin_verification(self.tenant, rid)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.transaction(self.tenant) as conn:
                conn.execute("UPDATE bonuspro.reservations SET state='committed',proof_revision='r' "
                             "WHERE tenant_id=%s AND id=%s", (self.tenant, rid))
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["reserved"], 700)

    def test_superuser_application_connection_is_rejected(self):
        with self.assertRaises(RuntimeError):
            Ledger(self.admin_dsn).snapshot(self.tenant, self.wallet)

    def test_released_reservation_cannot_start_application(self):
        self.credit()
        rid = self.reserve()
        self.ledger.release_unapplied(self.tenant, rid)
        with self.assertRaises(Conflict):
            self.ledger.begin_verification(self.tenant, rid)

    def test_wallet_lock_privilege_does_not_allow_mutation(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.transaction(self.tenant) as conn:
                conn.execute("UPDATE bonuspro.wallets SET created_at=now() WHERE tenant_id=%s AND id=%s",
                             (self.tenant, self.wallet))

    def test_reservation_identity_cannot_be_changed(self):
        self.credit()
        rid = self.reserve()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.admin_dsn) as conn:
                conn.execute("UPDATE bonuspro.reservations SET points=1 WHERE tenant_id=%s AND id=%s",
                             (self.tenant, rid))
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["reserved"], 700)

    def test_one_sided_journal_is_rejected_at_commit(self):
        tx = str(uuid4())
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.transaction(self.tenant) as conn:
                conn.execute(
                    "INSERT INTO bonuspro.ledger_transactions "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,kind,delta,actor_id,reason) "
                    "VALUES (%s,%s,%s,'one-side',%s,'credit',100,%s,'test')",
                    (self.tenant, tx, self.wallet, "a"*64, self.actor))
                conn.execute(
                    "INSERT INTO bonuspro.ledger_postings(tenant_id,transaction_id,wallet_id,account,amount) "
                    "VALUES (%s,%s,%s,'customer',100)", (self.tenant, tx, self.wallet))
        self.assertEqual(self.ledger.snapshot(self.tenant, self.wallet)["balance"], 0)


if __name__ == "__main__":
    unittest.main()
