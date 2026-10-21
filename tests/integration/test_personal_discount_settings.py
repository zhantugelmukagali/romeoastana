import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.domain.finance import Rules
from bonuspro.adapters.postgres.personal_discounts import PersonalDiscounts
import test_redemptions as fixture


class PersonalDiscountSettingsTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.RedemptionTests()
        self.fx.setUp()
        self.repo = PersonalDiscounts(self.fx.ledger)

    def save_policy(self, **changes):
        f = self.fx
        body = dict(enabled=True, external_policy="skip", allow_bonus_stack=False,
                    expected_version=0, request_id=str(uuid4()), actor_id=f.actor,
                    reason="Approved personal discount policy")
        return self.repo.save_policy(f.tenant, **(body | changes))

    def save_assignment(self, customer=None, **changes):
        f = self.fx
        body = dict(rate_bps=750, expected_version=0, request_id=str(uuid4()),
                    actor_id=f.actor, reason="Approved personal discount")
        return self.repo.save_assignment(f.tenant, customer or f.customer, **(body | changes))

    def counts(self):
        f = self.fx
        with f.ledger.transaction(f.tenant) as conn:
            return tuple(conn.execute("SELECT count(*) AS n FROM bonuspro." + table).fetchone()["n"]
                         for table in ("wallets", "ledger_transactions", "ledger_postings",
                                       "reservations", "personal_discount_applications"))

    def test_absent_policy_and_assignment_are_disabled_without_writes(self):
        before = self.counts()
        self.assertEqual(self.repo.policy(self.fx.tenant),
                         dict(version=0, enabled=False, external_policy="skip", allow_bonus_stack=False))
        a = self.repo.assignment(self.fx.tenant, self.fx.customer)
        self.assertEqual(a, dict(counterparty_id=self.fx.customer, version=0, rate_bps=None,
                                 effective_rate_bps=0, enabled=False))
        self.assertEqual(self.repo.assignments(self.fx.tenant, []), {})
        self.assertEqual(self.counts(), before)

    def test_save_configuration_does_not_create_wallet_activate_or_move_points(self):
        f = self.fx
        before = self.counts()
        unvisited_customer = str(uuid4())
        self.save_assignment(unvisited_customer, rate_bps=1250)
        self.save_policy()
        self.assertEqual(self.counts(), before)
        with f.ledger.transaction(f.tenant) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM bonuspro.customer_wallets WHERE tenant_id=%s AND counterparty_id=%s",
                (f.tenant, unvisited_customer)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT state FROM bonuspro.installations WHERE tenant_id=%s", (f.tenant,),
            ).fetchone()["state"], "Activated")

    def test_explicit_zero_and_reset_have_distinct_immutable_versions(self):
        f = self.fx
        self.save_policy()
        first = self.save_assignment()
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["actor_id"], f.actor)
        self.assertEqual(first["reason"], "Approved personal discount")
        self.assertEqual(self.repo.assignment(f.tenant, f.customer)["effective_rate_bps"], 750)
        zero = self.save_assignment(rate_bps=0, expected_version=1, reason="Individual zero rate")
        self.assertEqual(zero["rate_bps"], 0)
        cleared = self.save_assignment(rate_bps=None, expected_version=2, reason="Removed individual rate")
        self.assertIsNone(cleared["rate_bps"])
        self.assertEqual(cleared["version"], 3)
        history = self.repo.assignment_history(f.tenant, f.customer)["items"]
        self.assertEqual([a["rate_bps"] for a in history], [None, 0, 750])
        self.assertEqual(self.repo.assignment(f.tenant, f.customer)["effective_rate_bps"], 0)

    def test_disabled_policy_retains_assignment_without_applying_it(self):
        f = self.fx
        self.save_assignment(rate_bps=2500)
        self.assertEqual(self.repo.assignment(f.tenant, f.customer)["effective_rate_bps"], 0)
        self.save_policy()
        self.assertEqual(self.repo.assignment(f.tenant, f.customer)["effective_rate_bps"], 2500)
        self.save_policy(enabled=False, expected_version=1)
        a = self.repo.assignment(f.tenant, f.customer)
        self.assertEqual((a["rate_bps"], a["effective_rate_bps"], a["enabled"]), (2500, 0, False))

    def test_policy_retry_requires_same_actor_reason_expected_version_and_values(self):
        key = str(uuid4())
        first = self.save_policy(request_id=key)
        self.assertEqual(first, self.save_policy(request_id=key))
        for patch in (dict(enabled=False), dict(allow_bonus_stack=True), dict(external_policy="best"),
                      dict(actor_id=str(uuid4())), dict(reason="Changed reason"),
                      dict(expected_version=1)):
            with self.subTest(patch=patch), self.assertRaises(Conflict):
                self.save_policy(request_id=key, **patch)
        self.assertEqual(len(self.repo.policy_history(self.fx.tenant)["items"]), 1)

    def test_assignment_retry_cannot_change_customer_rate_actor_or_expected_version(self):
        key = str(uuid4())
        first = self.save_assignment(request_id=key)
        self.assertEqual(first, self.save_assignment(request_id=key))
        with self.assertRaises(Conflict):
            self.save_assignment(str(uuid4()), request_id=key)
        for patch in (dict(rate_bps=None), dict(rate_bps=0), dict(actor_id=str(uuid4())),
                      dict(reason="Changed"), dict(expected_version=1)):
            with self.subTest(patch=patch), self.assertRaises(Conflict):
                self.save_assignment(request_id=key, **patch)

    def test_stale_edits_cannot_replace_policy_or_customer_assignment(self):
        self.save_policy()
        self.save_assignment()
        with self.assertRaises(Conflict):
            self.save_policy(external_policy="sequential")
        with self.assertRaises(Conflict):
            self.save_assignment(rate_bps=1000)
        self.assertEqual(self.repo.policy(self.fx.tenant)["version"], 1)
        self.assertEqual(self.repo.assignment(self.fx.tenant, self.fx.customer)["rate_bps"], 750)

    def test_concurrent_assignment_edits_have_one_winner(self):
        def change(rate):
            try:
                return self.save_assignment(rate_bps=rate)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (500, 1500)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.repo.assignment_history(self.fx.tenant, self.fx.customer)["items"]), 1)

    def test_concurrent_policy_edits_have_one_winner(self):
        def change(policy):
            try:
                return self.save_policy(external_policy=policy)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ("best", "sequential")))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_same_request_concurrent_retries_create_one_assignment(self):
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.save_assignment(request_id=key), range(3)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(self.repo.assignment_history(self.fx.tenant, self.fx.customer)["items"]), 1)

    def test_tenant_isolation_and_assignment_batch(self):
        f = self.fx
        self.save_policy()
        self.save_assignment()
        second = str(uuid4())
        self.save_assignment(second, rate_bps=0)
        other_tenant = str(uuid4())
        self.assertEqual(self.repo.policy(other_tenant)["version"], 0)
        self.assertEqual(self.repo.assignment_history(other_tenant, f.customer)["items"], [])
        self.assertEqual(self.repo.policy_history(other_tenant)["items"], [])
        rows = self.repo.assignments(f.tenant, [f.customer, second, f.customer, str(uuid4())])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[f.customer]["effective_rate_bps"], 750)
        self.assertEqual(rows[second]["rate_bps"], 0)
        with f.ledger.transaction(other_tenant) as conn:
            for table in ("personal_discount_policy_versions", "customer_personal_discount_versions",
                          "personal_discount_applications", "personal_discount_voids"):
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro." + table).fetchone()["n"], 0)

    def test_invalid_rates_policy_values_and_versions_do_not_write(self):
        for rate in (-1, 10001, True, "500", 2.5):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.save_assignment(rate_bps=rate)
        for patch in (dict(enabled=1), dict(allow_bonus_stack=0), dict(external_policy="add"),
                      dict(expected_version=True), dict(expected_version=-1),
                      dict(expected_version=2147483647), dict(reason=" "),
                      dict(request_id="bad"), dict(actor_id="bad")):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.save_policy(**patch)
        self.assertEqual(self.repo.policy_history(self.fx.tenant)["items"], [])
        self.assertEqual(self.repo.assignment_history(self.fx.tenant, self.fx.customer)["items"], [])
        for ids in ([str(uuid4())] * 101, "bad", ["bad"]):
            with self.assertRaises(ValueError):
                self.repo.assignments(self.fx.tenant, ids)

    def test_assignment_history_is_bounded_and_cursor_stable(self):
        for i in range(51):
            self.save_assignment(rate_bps=i, expected_version=i)
        page = self.repo.assignment_history(self.fx.tenant, self.fx.customer)
        self.assertEqual(len(page["items"]), 50)
        self.assertEqual(page["next_cursor"], 2)
        next_page = self.repo.assignment_history(
            self.fx.tenant, self.fx.customer, page["next_cursor"])
        self.assertEqual([r["version"] for r in next_page["items"]], [1])
        self.assertIsNone(next_page["next_cursor"])

    def test_configuration_history_is_immutable_even_to_owner(self):
        f = self.fx
        self.save_policy()
        self.save_assignment()
        for table in ("personal_discount_policy_versions", "customer_personal_discount_versions"):
            with self.subTest(table=table), self.assertRaises(psycopg.Error):
                with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
                    conn.execute("DELETE FROM bonuspro." + table + " WHERE tenant_id=%s", (f.tenant,))
            with self.subTest(table=table), self.assertRaises(psycopg.Error):
                with f.ledger.transaction(f.tenant) as conn:
                    conn.execute("UPDATE bonuspro." + table + " SET reason='rewrite' WHERE tenant_id=%s", (f.tenant,))

    def test_settings_changes_cannot_rewrite_existing_bonus_plan(self):
        f = self.fx
        prepared = f.prepare()
        with f.ledger.transaction(f.tenant) as conn:
            before = conn.execute(
                "SELECT plan,source_hash,request_hash FROM bonuspro.redemption_attempts WHERE tenant_id=%s AND id=%s",
                (f.tenant, prepared["id"])).fetchone()
        funds = f.ledger.snapshot(f.tenant, f.wallet)
        self.save_policy(allow_bonus_stack=True)
        self.save_assignment()
        self.save_policy(enabled=False, expected_version=1)
        with f.ledger.transaction(f.tenant) as conn:
            after = conn.execute(
                "SELECT plan,source_hash,request_hash FROM bonuspro.redemption_attempts WHERE tenant_id=%s AND id=%s",
                (f.tenant, prepared["id"])).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(f.ledger.snapshot(f.tenant, f.wallet), funds)

    def application(self):
        """Minimal internal application for database guard tests, not API input."""
        f = self.fx
        self.save_policy()
        self.save_assignment(rate_bps=500)
        application_id = str(uuid4())
        with f.ledger.transaction(f.tenant) as conn:
            conn.execute(
                "INSERT INTO bonuspro.personal_discount_applications "
                "(tenant_id,id,order_id,counterparty_id,wallet_id,request_id,request_hash,source_hash,"
                "actor_id,marker_id,marker,policy_version,assignment_version,rate_bps,points,plan) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1,500,0,'{}'::jsonb)",
                (f.tenant, application_id, f.order, f.customer, f.wallet, str(uuid4()),
                 "a" * 64, f.saved.source_hash, f.actor, f.marker, "bonuspro:personal:" + application_id),
            )
        return application_id

    def test_orphan_personal_void_audit_cannot_commit(self):
        f = self.fx
        application = self.application()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute(
                    "INSERT INTO bonuspro.personal_discount_voids "
                    "(tenant_id,application_id,actor_id,reason,evidence_hash,proof_revision) "
                    "VALUES(%s,%s,%s,'cancel',%s,'revision')",
                    (f.tenant, application, f.actor, "a" * 64),
                )
        with f.ledger.transaction(f.tenant) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) AS n FROM bonuspro.personal_discount_voids").fetchone()["n"], 0)

    def test_application_state_requires_audit_and_saved_proof(self):
        f = self.fx
        application = self.application()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute("UPDATE bonuspro.personal_discount_applications SET state='voided' WHERE tenant_id=%s AND id=%s",
                             (f.tenant, application))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute("UPDATE bonuspro.personal_discount_applications SET state='confirmed' WHERE tenant_id=%s AND id=%s",
                             (f.tenant, application))
        with f.ledger.transaction(f.tenant) as conn:
            conn.execute("UPDATE bonuspro.personal_discount_applications SET state='confirmed',proof_revision='saved' "
                         "WHERE tenant_id=%s AND id=%s", (f.tenant, application))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute("UPDATE bonuspro.personal_discount_applications SET proof_revision='rewrite' "
                             "WHERE tenant_id=%s AND id=%s", (f.tenant, application))

    def test_personal_application_blocks_parallel_bonus_reservation_and_customer_rebinding(self):
        f = self.fx
        self.application()
        with self.assertRaises(psycopg.errors.CheckViolation):
            f.ledger.reserve(f.tenant, f.wallet, order_id=f.order, points=1,
                             eligible_minor=1000000, rules=Rules(500, 3000),
                             quote_hash="a" * 64, key="parallel", actor_id=f.actor)
        customer, wallet = str(uuid4()), str(uuid4())
        with f.ledger.transaction(f.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES(%s,%s)", (f.tenant, wallet))
            conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES(%s,%s,%s)",
                         (f.tenant, customer, wallet))
            with self.assertRaises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute("SELECT * FROM bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                                 (f.order, customer, "b" * 64, "2026-10-14 11:00:00.000", f.actor))


    def test_personal_application_cannot_overlap_an_existing_bonus_reservation(self):
        self.fx.prepare()
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.application()
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) AS n FROM bonuspro.personal_discount_applications").fetchone()["n"], 0)

    def test_database_rejects_skipped_configuration_versions(self):
        f = self.fx
        self.save_policy()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute(
                    "INSERT INTO bonuspro.personal_discount_policy_versions "
                    "(tenant_id,version,enabled,external_policy,allow_bonus_stack,request_id,request_hash,actor_id,reason) "
                    "VALUES(%s,3,true,'skip',false,%s,%s,%s,'invalid version')",
                    (f.tenant, str(uuid4()), "a" * 64, f.actor))
        self.assertEqual(self.repo.policy(f.tenant)["version"], 1)

    def test_configuration_is_not_reset_by_reading_a_different_customer(self):
        f = self.fx
        self.save_policy()
        self.save_assignment(rate_bps=10000)
        unrelated = self.repo.assignment(f.tenant, str(uuid4()))
        self.assertIsNone(unrelated["rate_bps"])
        self.assertEqual(unrelated["effective_rate_bps"], 0)
        self.assertEqual(self.repo.assignment(f.tenant, f.customer)["effective_rate_bps"], 10000)


    def test_personal_only_uuid_cannot_be_reused_as_a_bonus_reservation(self):
        f = self.fx
        application = self.application()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with f.ledger.transaction(f.tenant) as conn:
                conn.execute(
                    "INSERT INTO bonuspro.reservations "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,actor_id) "
                    "VALUES(%s,%s,%s,'collision',%s,%s,%s,1,%s)",
                    (f.tenant, application, f.wallet, "a" * 64, f.order, "b" * 64, f.actor))


if __name__ == "__main__":
    unittest.main()
