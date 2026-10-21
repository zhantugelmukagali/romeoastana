"""Direct PostgreSQL bypass checks for immutable verified presale compensation.

Provider evidence is authorized by the service suite. These checks exercise
the independent storage boundary using already provider-confirmed fixtures.
"""
from dataclasses import replace
from uuid import uuid4
import unittest

import psycopg
from psycopg.types.json import Jsonb
import test_personal_applications as personal_fixture


class PresaleUndoSchemaTests(unittest.TestCase):
    def setUp(self):
        self.application=personal_fixture.PersonalApplicationTests()
        self.application.setUp()
        self.addCleanup(self.application.doCleanups)
        self.fx=self.application.fx
        self.ledger=self.fx.ledger

    def confirmed(self,points=300,personal=True):
        if personal:
            prepared,_=self.application.prepare(points=points)
            self.application.persist(prepared)
            self.assertTrue(self.application.verify()["verified"])
        else:
            prepared=self.fx.prepare(points=1000)
            self.fx.saved=self.fx.persisted(prepared)
            self.assertTrue(self.fx.redemptions.confirm_saved(self.fx.tenant,self.fx.saved)["verified"])
        with self.ledger.transaction(self.fx.tenant) as conn:
            table="personal_discount_applications" if personal else "redemption_attempts"
            row=conn.execute("SELECT * FROM bonuspro."+table+" WHERE id=%s",(prepared["id"],)).fetchone()
        return row,personal

    def request(self,application,*,changes=None):
        row,personal=application
        with self.ledger.transaction(self.fx.tenant) as conn:
            installed=conn.execute("SELECT generation FROM bonuspro.installations").fetchone()
            reserved=conn.execute("SELECT * FROM bonuspro.reservations WHERE id=%s",(row["id"],)).fetchone()
            values=dict(tenant_id=self.fx.tenant,id=str(uuid4()),operation_id=row["id"],
                order_id=self.fx.order,wallet_id=self.fx.wallet,
                reservation_id=reserved["id"] if reserved else None,
                personal_application_id=row["id"] if personal else None,
                points=row["points"] if personal else reserved["points"],
                actor_id=self.fx.actor,generation=installed["generation"],request_hash="a"*64,
                source_hash=self.fx.saved.source_hash,source_revision=self.fx.saved.snapshot.revision,
                marker_id=row["marker_id"],marker=row["marker"],reason="Explicit presale cancellation",
                plan=dict(before=row["plan"]["after"],after=row["plan"]["before"],
                          update_state=dict(positions=[],attributes=[dict(id=str(row["marker_id"]),value=None)])))
            values.update(changes or {})
            columns=list(values)
            conn.execute("INSERT INTO bonuspro.presale_undo_requests ("+",".join(columns)+") VALUES ("+
                ",".join(["%s"]*len(columns))+")",
                tuple(Jsonb(values[key]) if key=="plan" else values[key] for key in columns))
        return values

    def refund(self,conn,request,*,delta=None,actor=None):
        tx=str(uuid4());amount=request["points"] if delta is None else delta
        conn.execute("INSERT INTO bonuspro.ledger_transactions "
            "(tenant_id,id,wallet_id,operation_key,request_hash,kind,delta,actor_id,reason,presale_undo_id) "
            "VALUES (%s,%s,%s,%s,%s,'presale_refund',%s,%s,%s,%s)",
            (self.fx.tenant,tx,self.fx.wallet,"presale-refund/"+tx,"b"*64,amount,
             actor or self.fx.actor,"Verified original price restored",request["id"]))
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO bonuspro.ledger_postings "
                "(tenant_id,transaction_id,wallet_id,account,amount) VALUES (%s,%s,%s,%s,%s)",
                [(self.fx.tenant,tx,self.fx.wallet,"customer",amount),
                 (self.fx.tenant,tx,self.fx.wallet,"system",-amount)])
        return tx

    def receipt(self,conn,request,transaction=None,actor=None,generation=None):
        conn.execute("INSERT INTO bonuspro.presale_undo_completions "
            "(tenant_id,request_id,actor_id,generation,evidence_hash,proof_revision,transaction_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (self.fx.tenant,request["id"],actor or self.fx.actor,generation or request["generation"],"c"*64,
             "2026-10-19 10:03:00.000",transaction))

    def terminal_states(self,conn,request):
        if request["reservation_id"]:
            conn.execute("UPDATE bonuspro.reservations SET state='reversed' WHERE id=%s",
                         (request["reservation_id"],))
        if request["personal_application_id"]:
            conn.execute("UPDATE bonuspro.personal_discount_applications SET state='voided' WHERE id=%s",
                         (request["personal_application_id"],))

    def complete(self,request):
        with self.ledger.transaction(self.fx.tenant) as conn:
            tx=self.refund(conn,request) if request["points"] else None
            self.receipt(conn,request,tx)
            self.terminal_states(conn,request)
        return tx

    def count(self,table):
        return self.application.count(table)

    def assert_original_unchanged(self,application):
        row,personal=application
        with self.ledger.transaction(self.fx.tenant) as conn:
            table="personal_discount_applications" if personal else "redemption_attempts"
            current=conn.execute("SELECT * FROM bonuspro."+table+" WHERE id=%s",(row["id"],)).fetchone()
            self.assertEqual(current["plan"],row["plan"])
            if personal:self.assertEqual(current["proof_revision"],row["proof_revision"])
            debit=conn.execute("SELECT * FROM bonuspro.ledger_transactions WHERE reservation_id=%s",(row["id"],)).fetchone()
            if debit:self.assertEqual((debit["kind"],debit["delta"]),("redemption",-application[0].get("points",1000)))

    def assert_compensation_preserves_history(self,personal):
        application=self.confirmed(personal=personal)
        request=self.request(application)
        with self.ledger.transaction(self.fx.tenant) as conn:
            original=conn.execute("SELECT * FROM bonuspro.reservations WHERE id=%s",(request["reservation_id"],)).fetchone()
        self.assertLess(self.application.balance()["balance"],5000)
        tx=self.complete(request)
        self.assertEqual(self.application.balance(),dict(balance=5000,reserved=0,available=5000))
        self.assert_original_unchanged(application)
        with self.ledger.transaction(self.fx.tenant) as conn:
            reserved=conn.execute("SELECT * FROM bonuspro.reservations WHERE id=%s",(request["reservation_id"],)).fetchone()
            receipt=conn.execute("SELECT * FROM bonuspro.presale_undo_completions").fetchone()
            self.assertEqual(reserved["state"],"reversed")
            self.assertEqual(reserved["proof_revision"],original["proof_revision"])
            self.assertEqual(str(receipt["transaction_id"]),tx)
            self.assertIsNotNone(receipt["completed_at"])
        self.assertEqual(self.count("ledger_transactions"),3)

    def test_combined_compensation_retains_old_debit_proof_and_plan(self):
        self.assert_compensation_preserves_history(True)

    def test_bonus_only_compensation_retains_old_debit_proof_and_plan(self):
        self.assert_compensation_preserves_history(False)

    def test_personal_only_undo_has_no_reservation_or_wallet_posting(self):
        application=self.confirmed(points=0)
        request=self.request(application)
        self.assertIsNone(self.complete(request))
        self.assertEqual(self.count("reservations"),0)
        self.assertEqual(self.count("ledger_transactions"),1)
        self.assertEqual(self.application.balance()["balance"],5000)
        self.assert_original_unchanged(application)

    def test_confirmed_states_cannot_be_reversed_without_receipt(self):
        application=self.confirmed()
        request=self.request(application)
        for table,state,identity in (("reservations","reversed",request["reservation_id"]),
                                     ("personal_discount_applications","voided",request["personal_application_id"])):
            with self.subTest(table=table),self.assertRaises(psycopg.Error):
                with self.ledger.transaction(self.fx.tenant) as conn:
                    conn.execute("UPDATE bonuspro."+table+" SET state=%s WHERE id=%s",(state,identity))
        self.assertEqual(self.application.balance()["balance"],4700)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_refund_without_receipt_or_terminal_states_rolls_back(self):
        application=self.confirmed()
        request=self.request(application)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:self.refund(conn,request)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:
                tx=self.refund(conn,request);self.receipt(conn,request,tx)
        self.assertEqual(self.application.balance()["balance"],4700)
        self.assertEqual(self.count("ledger_transactions"),2)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_receipt_requires_exact_positive_amount_and_actual_completer(self):
        application=self.confirmed()
        request=self.request(application)
        for amount,refund_actor,receipt_actor in ((299,self.fx.actor,self.fx.actor),
                                                 (300,str(uuid4()),self.fx.actor)):
            with self.subTest(amount=amount,actor=refund_actor),self.assertRaises(psycopg.Error):
                with self.ledger.transaction(self.fx.tenant) as conn:
                    tx=self.refund(conn,request,delta=amount,actor=refund_actor)
                    self.receipt(conn,request,tx,actor=receipt_actor)
                    self.terminal_states(conn,request)
        self.assertEqual(self.application.balance()["balance"],4700)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_receipt_without_refund_and_personal_receipt_with_journal_are_rejected(self):
        application=self.confirmed()
        request=self.request(application)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:
                self.receipt(conn,request)
                self.terminal_states(conn,request)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_intent_cannot_change_original_target_or_generation_and_no_partial_audit_remains(self):
        application=self.confirmed()
        for changes in ({"generation":str(uuid4())},
                        {"wallet_id":str(uuid4())},
                        {"points":1},
                        {"plan":{"before":application[0]["plan"]["after"],"after":{},"update_state":{}}}):
            with self.subTest(changes=changes),self.assertRaises(psycopg.Error):
                self.request(application,changes=changes)
        self.assertEqual(self.count("presale_undo_requests"),0)
        self.assertEqual(self.application.balance()["balance"],4700)

    def test_same_operation_cannot_issue_two_requests_or_two_refunds(self):
        application=self.confirmed();request=self.request(application)
        with self.assertRaises(psycopg.Error):self.request(application)
        self.complete(request)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:self.refund(conn,request)
        self.assertEqual(self.count("presale_undo_requests"),1)
        self.assertEqual(self.count("presale_undo_completions"),1)
        self.assertEqual(self.application.balance()["balance"],5000)

    def test_request_receipt_immutable_and_tenant_isolated(self):
        application=self.confirmed();request=self.request(application);self.complete(request)
        for table in ("presale_undo_requests","presale_undo_completions"):
            for mutation in ("DELETE FROM bonuspro."+table,
                             "UPDATE bonuspro."+table+" SET actor_id=actor_id"):
                with self.subTest(mutation=mutation),self.assertRaises(psycopg.Error):
                    with self.ledger.transaction(self.fx.tenant) as conn:conn.execute(mutation)
            with self.ledger.transaction(str(uuid4())) as conn:
                self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro."+table).fetchone()["n"],0)

    def test_only_complete_undo_allows_buyer_rebind_and_keeps_refund_in_original_wallet(self):
        application=self.confirmed();request=self.request(application)
        customer,wallet=str(uuid4()),str(uuid4())
        self.ledger.create_wallet(self.fx.tenant,wallet)
        with self.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                         (self.fx.tenant,customer,wallet))
        def rebind():
            with self.ledger.transaction(self.fx.tenant) as conn:
                return conn.execute("SELECT (bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)).*",
                    (self.fx.order,customer,"d"*64,"2026-10-19 10:04:00.000",self.fx.actor)).fetchone()
        with self.assertRaises(psycopg.Error):rebind()
        self.complete(request)
        rebound=rebind()
        self.assertEqual(str(rebound["wallet_id"]),wallet)
        self.assertEqual(self.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],5000)
        self.assertEqual(self.ledger.snapshot(self.fx.tenant,wallet)["balance"],0)
        self.assertEqual(self.count("order_customer_rebindings"),1)
        self.assert_original_unchanged(application)

    def test_stale_completion_generation_blocks_receipt_and_compensation_atomically(self):
        import os
        application=self.confirmed();request=self.request(application)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET generation=%s WHERE tenant_id=%s",(str(uuid4()),self.fx.tenant))
        with self.assertRaises(psycopg.Error):self.complete(request)
        self.assertEqual(self.application.balance()["balance"],4700)
        self.assertEqual(self.count("presale_undo_completions"),0)
        self.assertEqual(self.count("ledger_transactions"),2)

    def test_forced_early_constraint_validation_keeps_lifecycle_settings_order_fences_until_commit(self):
        application=self.confirmed();request=self.request(application)
        keys=("installation/"+self.fx.tenant,self.fx.tenant+"/settings",
              self.fx.tenant+"/order/"+self.fx.order)
        with self.ledger.transaction(self.fx.tenant) as first:
            tx=self.refund(first,request)
            self.receipt(first,request,tx)
            self.terminal_states(first,request)
            first.execute("SET CONSTRAINTS ALL IMMEDIATE")
            with self.ledger.transaction(self.fx.tenant) as second:
                for key in keys:
                    locked=second.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                                          (key,)).fetchone()["acquired"]
                    self.assertFalse(locked,key)
        with self.ledger.transaction(self.fx.tenant) as second:
            for key in keys:
                self.assertTrue(second.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                                               (key,)).fetchone()["acquired"])
        self.assertEqual(self.application.balance()["balance"],5000)

    def test_explicit_edited_source_can_target_only_immutable_original_financial_snapshots(self):
        application=self.confirmed()
        request=self.request(application,changes={"source_hash":"e"*64,"source_revision":"2026-10-19 10:02:30.000"})
        self.assertEqual(request["plan"]["before"],application[0]["plan"]["after"])
        self.assertEqual(request["plan"]["after"],application[0]["plan"]["before"])
        self.assertEqual(self.count("presale_undo_requests"),1)
        self.assertEqual(self.application.balance()["balance"],4700)

    def test_personal_only_receipt_cannot_create_wallet_credit(self):
        application=self.confirmed(points=0);request=self.request(application)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:
                tx=self.refund(conn,request,delta=1)
                self.receipt(conn,request,tx)
                self.terminal_states(conn,request)
        self.assertEqual(self.count("presale_undo_completions"),0)
        self.assertEqual(self.application.balance()["balance"],5000)
        self.assertEqual(self.count("ledger_transactions"),1)

    def test_zero_point_sale_history_after_undo_still_pins_customer(self):
        application=self.confirmed();request=self.request(application);self.complete(request)
        customer,wallet=str(uuid4()),str(uuid4())
        self.ledger.create_wallet(self.fx.tenant,wallet)
        with self.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                         (self.fx.tenant,customer,wallet))
            conn.execute("INSERT INTO bonuspro.order_accruals "
                "(tenant_id,order_id,wallet_id,points,eligible_minor,financial_hash,evidence_hash,rule_version) "
                "VALUES (%s,%s,%s,0,0,%s,%s,1)",
                (self.fx.tenant,self.fx.order,self.fx.wallet,"e"*64,"f"*64))
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:
                conn.execute("SELECT bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                    (self.fx.order,customer,"d"*64,"2026-10-19 10:04:00.000",self.fx.actor))
        self.assertEqual(self.count("order_customer_rebindings"),0)

    def test_fresh_current_generation_receipt_resumes_original_request_without_rewriting_it(self):
        import os
        application=self.confirmed();request=self.request(application)
        current=str(uuid4())
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET generation=%s WHERE tenant_id=%s",(current,self.fx.tenant))
        with self.ledger.transaction(self.fx.tenant) as conn:
            tx=self.refund(conn,request)
            self.receipt(conn,request,tx,generation=current)
            self.terminal_states(conn,request)
        with self.ledger.transaction(self.fx.tenant) as conn:
            frozen=conn.execute("SELECT generation FROM bonuspro.presale_undo_requests").fetchone()
            completion=conn.execute("SELECT generation FROM bonuspro.presale_undo_completions").fetchone()
            self.assertEqual(str(frozen["generation"]),str(request["generation"]))
            self.assertEqual(str(completion["generation"]),current)
        self.assertEqual(self.application.balance()["balance"],5000)
        self.assertEqual(self.count("presale_undo_completions"),1)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.fx.tenant) as conn:self.refund(conn,request)
        self.assert_original_unchanged(application)
