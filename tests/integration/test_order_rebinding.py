"""PostgreSQL guards for correcting a preview-only order's saved customer."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier
from uuid import uuid4

import psycopg
import test_redemptions as fixtures
from bonuspro.adapters.postgres.ledger import Conflict, fingerprint
from bonuspro.domain.finance import Rules


class OrderRebindingTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RedemptionTests()
        self.fx.setUp()

    def buyer(self, fx=None):
        fx = fx or self.fx
        customer = str(uuid4())
        snapshot = replace(fx.saved.snapshot, order_id=str(uuid4()), counterparty_id=customer)
        quote = fx.checkout.preview(fx.tenant, snapshot, requested_points=0,
                                   actor_id=fx.actor, eligibility_version=1)
        wallet = quote["wallet_id"]
        fx.ledger.credit(fx.tenant, wallet, points=700, key=str(uuid4()),
                         actor_id=fx.actor, reason="Independent destination balance")
        return customer, wallet

    def context(self, fx=None):
        fx = fx or self.fx
        with fx.ledger.transaction(fx.tenant) as conn:
            return conn.execute("SELECT * FROM bonuspro.order_contexts "
                                "WHERE tenant_id=%s AND order_id=%s",
                                (fx.tenant, fx.order)).fetchone()

    def rebind(self, customer, fx=None, tenant=None):
        fx = fx or self.fx
        with fx.ledger.transaction(tenant or fx.tenant) as conn:
            return conn.execute("SELECT * FROM bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                                (fx.order, customer, "b"*64, "2026-10-19 10:01:00.000", fx.actor)).fetchone()

    def test_preview_rebinds_legacy_context_without_moving_money_or_rule_pin(self):
        fx = self.fx
        customer, wallet = self.buyer()
        original = self.context()
        old_funds = fx.ledger.snapshot(fx.tenant, fx.wallet)
        new_funds = fx.ledger.snapshot(fx.tenant, wallet)
        fx.checkout.save_rules(fx.tenant, earn_bps=1000, spend_bps=3000,
                               delivery_policy="exclude", expected_version=1,
                               request_id=str(uuid4()), actor_id=fx.actor, reason="Later rules")
        saved = replace(fx.saved.snapshot, counterparty_id=customer, revision="2026-10-19 10:01:00.000")
        quote = fx.checkout.preview(fx.tenant, saved, requested_points=0,
                                    actor_id=fx.actor, eligibility_version=1)
        self.assertEqual((quote["counterparty_id"], quote["wallet_id"], quote["rule_version"]),
                         (customer, wallet, 1))
        current = self.context()
        for field in ("rule_version", "initial_source_hash", "enrolled_by", "created_at"):
            self.assertEqual(current[field], original[field], field)
        self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet), old_funds)
        self.assertEqual(fx.ledger.snapshot(fx.tenant, wallet), new_funds)
        with fx.ledger.transaction(fx.tenant) as conn:
            audit = conn.execute("SELECT * FROM bonuspro.order_customer_rebindings "
                                 "WHERE tenant_id=%s AND order_id=%s",
                                 (fx.tenant, fx.order)).fetchall()
        self.assertEqual(len(audit), 1)
        change = audit[0]
        self.assertEqual(str(change["old_counterparty_id"]), fx.customer)
        self.assertEqual(str(change["new_counterparty_id"]), customer)
        self.assertEqual(str(change["old_wallet_id"]), fx.wallet)
        self.assertEqual(str(change["new_wallet_id"]), wallet)
        self.assertEqual(str(change["actor_id"]), fx.actor)
        self.assertEqual(change["rule_version"], 1)
        self.assertEqual(change["source_hash"], fingerprint(asdict(saved)))
        self.assertEqual(change["source_revision"], saved.revision)
        fx.checkout.preview(fx.tenant, saved, requested_points=0,
                            actor_id=fx.actor, eligibility_version=1)
        with fx.ledger.transaction(fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                                          "WHERE tenant_id=%s AND order_id=%s",
                                          (fx.tenant, fx.order)).fetchone()["n"], 1)

    def test_every_reservation_state_blocks_even_the_direct_function(self):
        for state in ("held", "released", "verifying", "committed"):
            with self.subTest(state=state):
                fx = fixtures.RedemptionTests()
                fx.setUp()
                customer, _ = self.buyer(fx)
                if state == "committed":
                    prepared = fx.prepare()
                    fx.redemptions.confirm_saved(fx.tenant, fx.persisted(prepared))
                else:
                    rid = fx.ledger.reserve(fx.tenant, fx.wallet, order_id=fx.order, points=1,
                                            eligible_minor=1000000, rules=Rules(),
                                            quote_hash="a"*64, key=str(uuid4()), actor_id=fx.actor)
                    if state == "released":
                        fx.ledger.release_unapplied(fx.tenant, rid)
                    elif state == "verifying":
                        fx.ledger.begin_verification(fx.tenant, rid)
                before = self.context(fx)
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.rebind(customer, fx)
                self.assertEqual(self.context(fx), before)

    def test_zero_accrual_settlements_and_resolved_issues_are_financial_history(self):
        for footprint in ("zero_accrual", "settlement_event", "settlement_case", "resolved_issue"):
            with self.subTest(footprint=footprint):
                fx = fixtures.RedemptionTests()
                fx.setUp()
                customer, _ = self.buyer(fx)
                with fx.ledger.transaction(fx.tenant) as conn:
                    if footprint == "zero_accrual":
                        conn.execute("INSERT INTO bonuspro.order_accruals "
                                     "(tenant_id,order_id,wallet_id,points,eligible_minor,financial_hash,evidence_hash,rule_version) "
                                     "VALUES (%s,%s,%s,0,0,%s,%s,1)",
                                     (fx.tenant, fx.order, fx.wallet, "a"*64, "b"*64))
                    elif footprint == "settlement_event":
                        conn.execute("INSERT INTO bonuspro.settlement_events "
                                     "(tenant_id,id,order_id,wallet_id,target_earn,target_refund,earn_delta,refund_delta,evidence_hash,actor_id,decision,reason) "
                                     "VALUES (%s,%s,%s,%s,0,0,0,0,%s,%s,'automatic','History guard fixture')",
                                     (fx.tenant, uuid4(), fx.order, fx.wallet, "a"*64, fx.actor))
                    elif footprint == "settlement_case":
                        conn.execute("INSERT INTO bonuspro.settlement_cases "
                                     "(tenant_id,id,order_id,wallet_id,evidence_hash,target_earn,target_refund,earn_delta,refund_delta) "
                                     "VALUES (%s,%s,%s,%s,%s,0,0,0,0)",
                                     (fx.tenant, uuid4(), fx.order, fx.wallet, "a"*64))
                    else:
                        conn.execute("INSERT INTO bonuspro.reconciliation_issues "
                                     "(tenant_id,order_id,wallet_id,reason,evidence_hash,resolved_at,resolved_by,resolution_reason) "
                                     "VALUES (%s,%s,%s,'changed_credited_order',%s,clock_timestamp(),%s,'Already reviewed')",
                                     (fx.tenant, fx.order, fx.wallet, "a"*64, fx.actor))
                with self.assertRaises(psycopg.errors.CheckViolation):
                    self.rebind(customer, fx)
                self.assertEqual(str(self.context(fx)["counterparty_id"]), fx.customer)

    def test_unknown_destination_does_not_create_an_implicit_wallet(self):
        fx = self.fx
        customer = str(uuid4())
        with self.assertRaises(psycopg.Error):
            self.rebind(customer)
        with fx.ledger.transaction(fx.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.customer_wallets "
                                           "WHERE tenant_id=%s AND counterparty_id=%s",
                                           (fx.tenant, customer)).fetchone())
        self.assertEqual(str(self.context()["counterparty_id"]), fx.customer)

    def test_tenant_scope_and_ordinary_update_privileges_cannot_bypass_guard(self):
        fx = self.fx
        customer, _ = self.buyer()
        other = fixtures.RedemptionTests()
        other.setUp()
        try:
            self.rebind(customer, tenant=other.tenant)
        except psycopg.Error:
            pass
        self.assertEqual(str(self.context()["counterparty_id"]), fx.customer)
        with fx.ledger.transaction(fx.tenant) as conn:
            self.assertFalse(conn.execute("SELECT has_any_column_privilege(current_user,"
                                          "'bonuspro.order_contexts','UPDATE') allowed").fetchone()["allowed"])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with fx.ledger.transaction(fx.tenant) as conn:
                conn.execute("UPDATE bonuspro.order_contexts SET counterparty_id=%s "
                             "WHERE tenant_id=%s AND order_id=%s", (customer, fx.tenant, fx.order))

    def test_rebinding_audit_is_immutable_and_tenant_isolated(self):
        fx = self.fx
        customer, _ = self.buyer()
        self.rebind(customer)
        other = fixtures.RedemptionTests()
        other.setUp()
        with fx.ledger.transaction(other.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                                          "WHERE tenant_id=%s AND order_id=%s",
                                          (fx.tenant, fx.order)).fetchone()["n"], 0)
        for statement in ("UPDATE bonuspro.order_customer_rebindings SET source_revision='forged' "
                          "WHERE tenant_id=%s AND order_id=%s",
                          "DELETE FROM bonuspro.order_customer_rebindings WHERE tenant_id=%s AND order_id=%s"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                with fx.ledger.transaction(fx.tenant) as conn:
                    conn.execute(statement, (fx.tenant, fx.order))

    def legacy_null_revision(self):
        fx=self.fx
        # Reproduce a row created before this additive migration. Use the table
        # owner explicitly so this fixture also works with a restricted owner.
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            assert conn.info.dbname=="bonuspro_ci"
            owner=conn.execute("SELECT pg_get_userbyid(relowner) FROM pg_class "
                               "WHERE oid='bonuspro.order_contexts'::regclass").fetchone()[0]
            conn.execute(psycopg.sql.SQL("SET LOCAL ROLE {}").format(psycopg.sql.Identifier(owner)))
            conn.execute("SELECT set_config('bonuspro.tenant_id',%s,true)",(fx.tenant,))
            conn.execute("UPDATE bonuspro.order_contexts SET binding_revision=NULL "
                         "WHERE tenant_id=%s AND order_id=%s",(fx.tenant,fx.order))

    def test_legacy_null_revision_can_be_initialized_by_fresh_saved_customer(self):
        self.legacy_null_revision()
        customer,_=self.buyer()
        self.rebind(customer)
        current=self.context()
        self.assertEqual(str(current["counterparty_id"]),customer)
        self.assertEqual(current["binding_revision"],"2026-10-19 10:01:00.000")

    def test_legacy_null_revision_with_history_stays_immutable_even_on_same_buyer(self):
        fx=self.fx
        self.legacy_null_revision()
        fx.prepare(points=1)
        before=self.context()
        with fx.ledger.transaction(fx.tenant) as conn:
            conn.execute("SELECT * FROM bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                (fx.order,fx.customer,"b"*64,"2026-10-19 10:02:00.000",fx.actor))
        self.assertEqual(self.context(),before)
        self.assertIsNone(self.context()["binding_revision"])
        customer,_=self.buyer()
        with self.assertRaises(psycopg.errors.CheckViolation):self.rebind(customer)

    def test_new_context_requires_an_initial_saved_revision(self):
        fx=self.fx
        with self.assertRaises(psycopg.errors.CheckViolation):
            with fx.ledger.transaction(fx.tenant) as conn:
                conn.execute("INSERT INTO bonuspro.order_contexts "
                    "(tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by) "
                    "VALUES (%s,%s,%s,%s,1,%s,%s)",
                    (fx.tenant,uuid4(),fx.customer,fx.wallet,"a"*64,fx.actor))

    def test_same_customer_observations_fence_future_rebind_without_audit_or_rule_changes(self):
        fx=self.fx
        original=self.context()
        newer=replace(fx.saved.snapshot,revision="2026-10-19 10:02:00.000")
        fx.checkout.preview(fx.tenant,newer,requested_points=0,actor_id=fx.actor,eligibility_version=1)
        current=self.context()
        self.assertEqual(current["binding_revision"],newer.revision)
        for field in ("counterparty_id","wallet_id","rule_version","initial_source_hash","enrolled_by","created_at"):
            self.assertEqual(current[field],original[field],field)
        customer,_=self.buyer()
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.rebind(customer)
        with self.assertRaises(Conflict):
            fx.checkout.preview(fx.tenant,replace(newer,revision="2026-10-19 10:01:00.000"),
                requested_points=0,actor_id=fx.actor,eligibility_version=1)
        with fx.ledger.transaction(fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.order_customer_rebindings "
                "WHERE tenant_id=%s AND order_id=%s",(fx.tenant,fx.order)).fetchone()["n"],0)

    def test_equal_revision_or_invalid_calendar_cannot_change_buyer(self):
        fx=self.fx
        customer,_=self.buyer()
        for revision in (fx.saved.snapshot.revision,"2026-02-30 10:01:00.000"):
            with self.subTest(revision=revision),self.assertRaises(psycopg.errors.CheckViolation):
                with fx.ledger.transaction(fx.tenant) as conn:
                    conn.execute("SELECT * FROM bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                        (fx.order,customer,"b"*64,revision,fx.actor))
        self.assertEqual(str(self.context()["counterparty_id"]),fx.customer)

    def test_stale_generic_reservation_cannot_use_the_old_wallet_after_rebind(self):
        fx=self.fx
        customer,wallet=self.buyer()
        self.rebind(customer)
        with self.assertRaises(Conflict):
            fx.ledger.reserve(fx.tenant,fx.wallet,order_id=fx.order,points=1,
                eligible_minor=1000000,rules=Rules(),quote_hash="a"*64,key=str(uuid4()),actor_id=fx.actor)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with fx.ledger.transaction(fx.tenant) as conn:
                conn.execute("INSERT INTO bonuspro.reservations "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,actor_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s)",
                    (fx.tenant,uuid4(),fx.wallet,str(uuid4()),"a"*64,fx.order,"b"*64,fx.actor))
        self.assertEqual(fx.ledger.snapshot(fx.tenant,fx.wallet)["reserved"],0)
        self.assertEqual(fx.ledger.snapshot(fx.tenant,wallet)["reserved"],0)

    def test_context_cannot_be_created_over_a_different_wallets_reservation(self):
        fx=self.fx
        customer,wallet=self.buyer()
        standalone_order=str(uuid4())
        fx.ledger.reserve(fx.tenant,fx.wallet,order_id=standalone_order,points=1,
            eligible_minor=1000000,rules=Rules(),quote_hash="a"*64,key=str(uuid4()),actor_id=fx.actor)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with fx.ledger.transaction(fx.tenant) as conn:
                conn.execute("INSERT INTO bonuspro.order_contexts "
                    "(tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by,binding_revision) "
                    "VALUES (%s,%s,%s,%s,1,%s,%s,%s)",
                    (fx.tenant,standalone_order,customer,wallet,"a"*64,fx.actor,"2026-10-19 10:01:00.000"))

    def test_prepare_and_rebind_cannot_reserve_old_wallet_after_binding_changes(self):
        fx = self.fx
        customer, wallet = self.buyer()
        new_snapshot = replace(fx.saved.snapshot, counterparty_id=customer, revision="2026-10-19 10:01:00.000")
        barrier = Barrier(2)
        def prepare():
            barrier.wait(timeout=10)
            try:
                return ("prepared", fx.prepare(points=1))
            except (Conflict, psycopg.errors.CheckViolation):
                return ("rejected", None)
        def rebind():
            barrier.wait(timeout=10)
            try:
                return ("rebound", fx.checkout.preview(fx.tenant, new_snapshot, requested_points=0,
                        actor_id=fx.actor, eligibility_version=1))
            except (Conflict, psycopg.errors.CheckViolation):
                return ("rejected", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(prepare)
            second = pool.submit(rebind)
            prepared, rebound = first.result(timeout=20), second.result(timeout=20)
        self.assertEqual(sum(result[0] != "rejected" for result in (prepared, rebound)), 1)
        context = self.context()
        with fx.ledger.transaction(fx.tenant) as conn:
            reservations = conn.execute("SELECT wallet_id,state FROM bonuspro.reservations "
                                        "WHERE tenant_id=%s AND order_id=%s",
                                        (fx.tenant, fx.order)).fetchall()
        if rebound[0] == "rebound":
            self.assertEqual(str(context["counterparty_id"]), customer)
            self.assertEqual(str(context["wallet_id"]), wallet)
            self.assertEqual(reservations, [])
            self.assertEqual(fx.ledger.snapshot(fx.tenant, fx.wallet)["reserved"], 0)
        else:
            self.assertEqual(str(context["counterparty_id"]), fx.customer)
            self.assertEqual(len(reservations), 1)
            self.assertEqual(str(reservations[0]["wallet_id"]), fx.wallet)
            self.assertEqual(reservations[0]["state"], "verifying")


if __name__ == "__main__":
    unittest.main()
