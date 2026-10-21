"""Real PostgreSQL receipts and API proof for committed presale cancellation."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import test_redemptions as redemption_fixture
import test_widget_api as widget_fixture
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from fastapi.testclient import TestClient
from bonuspro.adapters.moysklad.fulfillment import Fulfillment
from bonuspro.adapters.moysklad.remap import OrderEvidence
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import MAX_POINTS
from bonuspro.adapters.postgres.presale_undo import PresaleUndo,PresaleUndoConflict


class PresaleUndoTests(unittest.TestCase):
    def setUp(self):
        self.fx=redemption_fixture.RedemptionTests()
        self.fx.setUp()
        self.undo=PresaleUndo(self.fx.ledger)
        self.fx.saved=replace(self.fx.saved,recreatable_ids=frozenset(line.id for line in self.fx.saved.lines))
        self.original=self.fx.saved
        self.applied=self.fx.prepare()
        self.discounted=replace(self.fx.persisted(self.applied),
            snapshot=replace(self.fx.persisted(self.applied).snapshot,revision="2026-10-14 10:01:00.000"))
        self.fx.redemptions.confirm_saved(self.fx.tenant,self.discounted)

    def proof(self,saved=None,**patches):
        saved=saved or self.discounted
        return replace(Fulfillment(saved,datetime.now(timezone.utc)+timedelta(seconds=1),0,0,
            "waiting_shipment","a"*64,time.monotonic()+120,presale_safe=True),**patches)

    def restored(self,**patches):
        return replace(self.original,snapshot=replace(self.original.snapshot,revision="2026-10-14 10:02:00.000"),**patches)

    def prepare(self,proof=None,**changes):
        args=dict(attempt_id=self.applied["id"],request_id=str(uuid4()),actor=self.fx.actor,
                  expected_source=(proof or self.proof()).saved.source_hash)
        args.update(changes)
        return self.undo.prepare(self.fx.tenant,self.fx.order,proof or self.proof(),**args)

    def verify(self,identity,proof=None,**changes):
        return self.undo.verify(self.fx.tenant,self.fx.order,proof or self.proof(self.restored()),
            undo_id=identity,actor=self.fx.actor,**changes)

    def balance(self):
        return self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"]

    def count(self,table):
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            return conn.execute("SELECT count(*) n FROM bonuspro."+table).fetchone()["n"]

    def test_prepare_and_browser_ack_never_refund(self):
        result=self.prepare()
        self.assertEqual(self.balance(),4000)
        self.assertEqual(result["update_state"]["attributes"],[{"id":self.fx.marker,"value":None}])
        self.assertEqual({r["id"] for r in result["update_state"]["positions"]},
                         {r.id for r in self.original.lines})
        pending=self.verify(result["undo"]["id"],self.proof())
        self.assertFalse(pending["undo"]["verified"])
        self.assertTrue(pending["undo"]["recoverable"])
        self.assertEqual(self.balance(),4000)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_saved_original_refunds_once_and_preserves_original_immutable_journal(self):
        result=self.prepare();identity=result["undo"]["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies=list(pool.map(lambda _:self.verify(identity),range(4)))
        self.assertTrue(all(r["undo"]["verified"] for r in replies))
        self.assertEqual(self.balance(),5000)
        self.assertEqual(self.count("presale_undo_completions"),1)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            rows=conn.execute("SELECT kind,delta,presale_undo_id,reservation_id FROM bonuspro.ledger_transactions "
                              "WHERE kind IN ('redemption','presale_refund') ORDER BY delta").fetchall()
            self.assertEqual([(r["kind"],r["delta"]) for r in rows],[("redemption",-1000),("presale_refund",1000)])
            self.assertEqual(str(rows[0]["reservation_id"]),self.applied["id"])
            self.assertEqual(str(rows[1]["presale_undo_id"]),identity)
            reservation=conn.execute("SELECT state,proof_revision FROM bonuspro.reservations").fetchone()
            self.assertEqual(reservation["state"],"reversed")
            self.assertEqual(reservation["proof_revision"],self.discounted.snapshot.revision)

    def test_lost_prepare_reply_and_double_click_do_not_redispatch_inverse(self):
        request=str(uuid4())
        first=self.prepare(request_id=request)
        replay=self.prepare(request_id=request)
        self.assertTrue(replay["replayed"]);self.assertNotIn("update_state",replay)
        other=self.prepare()
        self.assertEqual(first["undo"]["id"],other["undo"]["id"])
        self.assertNotIn("update_state",other)
        self.assertEqual(self.count("presale_undo_requests"),1)
        proof=self.proof()
        resumed=self.undo.resume(self.fx.tenant,self.fx.order,proof,
            undo_id=first["undo"]["id"],expected_source=proof.saved.source_hash)
        self.assertEqual(resumed["update_state"],first["update_state"])
        self.assertEqual(self.balance(),4000)

    def test_changed_saved_hash_and_reused_request_with_changed_body_are_rejected(self):
        with self.assertRaises(PresaleUndoConflict):
            self.prepare(expected_source="b"*64)
        self.assertEqual(self.count("presale_undo_requests"),0)
        request=str(uuid4());self.prepare(request_id=request)
        with self.assertRaises(PresaleUndoConflict):
            self.prepare(request_id=request,restore_original=True)
        self.assertEqual(self.balance(),4000)

    def test_changed_quantity_requires_explicit_restore_then_full_saved_original(self):
        lines=(replace(self.discounted.lines[0],quantity=Decimal(2)),self.discounted.lines[1])
        changed=replace(self.discounted,lines=lines,snapshot=replace(self.discounted.snapshot,
            revision="2026-10-14 10:01:30.000",total_minor=2000000,eligible_minor=1800000))
        proof=self.proof(changed)
        with self.assertRaises(PresaleUndoConflict) as rejected:
            self.prepare(proof)
        self.assertEqual(rejected.exception.reason,"undo_restore_original")
        self.assertEqual(self.count("presale_undo_requests"),0)
        result=self.prepare(proof,restore_original=True)
        self.assertEqual(next(x["quantity"] for x in result["update_state"]["positions"]
                             if x["id"]==self.original.lines[0].id),1)
        self.assertEqual(self.balance(),4000)
        pending=self.verify(result["undo"]["id"],proof)
        self.assertTrue(pending["undo"]["restore_required"])
        self.assertEqual(self.balance(),4000)
        self.assertTrue(self.verify(result["undo"]["id"])["undo"]["verified"])
        self.assertEqual(self.balance(),5000)

    def test_deleted_rows_are_recreated_but_only_full_original_signature_refunds(self):
        deleted=replace(self.discounted,lines=(self.discounted.lines[1],),
            snapshot=replace(self.discounted.snapshot,total_minor=200000,eligible_minor=0,
                             revision="2026-10-14 10:01:30.000"))
        result=self.prepare(self.proof(deleted),restore_original=True)
        self.assertEqual(sum("id" not in x for x in result["update_state"]["positions"]),1)
        restored=self.restored(lines=tuple(replace(line,id=str(uuid4())) for line in self.original.lines))
        wrong=replace(restored,lines=(replace(restored.lines[0],price=Decimal(900000)),
                                      replace(restored.lines[1],price=Decimal(300000))))
        self.assertFalse(self.verify(result["undo"]["id"],self.proof(wrong))["undo"]["verified"])
        self.assertEqual(self.balance(),4000)
        self.assertTrue(self.verify(result["undo"]["id"],self.proof(restored))["undo"]["verified"])
        self.assertEqual(self.balance(),5000)

    def test_manually_restored_original_with_new_ids_only_clears_marker_and_refunds_after_save(self):
        # No automatic reconstruction is authorized for this legacy shape.
        # Staff has independently restored the original rows in MoySklad.
        manual=replace(self.restored(),marker=self.discounted.marker,
            lines=tuple(replace(line,id=str(uuid4())) for line in self.original.lines))
        result=self.prepare(self.proof(manual),restore_original=True)
        self.assertEqual(result["update_state"]["positions"],[{"id":line.id} for line in manual.lines])
        self.assertEqual(self.balance(),4000)
        still_marked=self.verify(result["undo"]["id"],self.proof(manual))
        self.assertFalse(still_marked["undo"]["verified"])
        clean=replace(manual,marker=None,snapshot=replace(manual.snapshot,revision="2026-10-14 10:03:00.000"))
        self.assertTrue(self.verify(result["undo"]["id"],self.proof(clean))["undo"]["verified"])
        self.assertEqual(self.balance(),5000)

    def test_only_original_buyer_can_be_restored_and_refunded(self):
        changed=replace(self.discounted,snapshot=replace(self.discounted.snapshot,counterparty_id=str(uuid4())))
        hints=self.undo.hints(self.fx.tenant,changed,dict(id=self.applied["id"],state="committed",verified=False))
        self.assertFalse(hints["undo_available"])
        self.assertEqual(hints["original_buyer_id"],self.fx.customer)
        with self.assertRaises(PresaleUndoConflict):
            self.prepare(self.proof(changed),restore_original=True)
        result=self.prepare()
        changed=replace(self.restored(),snapshot=replace(self.restored().snapshot,counterparty_id=str(uuid4())))
        self.assertFalse(self.verify(result["undo"]["id"],self.proof(changed))["undo"]["recoverable"])
        self.assertEqual(self.balance(),4000)

    def test_payment_shipment_links_returns_cancel_and_expired_proof_block_prepare_and_completion(self):
        proofs=[
            self.proof(presale_safe=False),self.proof(paid_minor=1),self.proof(shipped_minor=1),
            self.proof(returned=(self.discounted.lines[0],)),self.proof(deadline=0),
            self.proof(replace(self.discounted,snapshot=replace(self.discounted.snapshot,cancelled=True))),
        ]
        for proof in proofs:
            with self.subTest(proof=proof.outcome):
                with self.assertRaises(PresaleUndoConflict):self.prepare(proof)
        self.assertEqual(self.count("presale_undo_requests"),0)
        result=self.prepare()
        for proof in (self.proof(self.restored(),presale_safe=False),self.proof(self.restored(),paid_minor=1),
                      self.proof(self.restored(),shipped_minor=1),self.proof(self.restored(),deadline=0)):
            with self.assertRaises(PresaleUndoConflict):self.verify(result["undo"]["id"],proof)
        self.assertEqual(self.balance(),4000)

    def test_reinstall_recovers_with_new_activation_auth_proof_and_current_generation_receipt(self):
        result=self.prepare()
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            old_generation=conn.execute("SELECT generation FROM bonuspro.installations").fetchone()["generation"]
        now=int(time.time())
        self.fx.install.change(self.fx.tenant,dict(iat=now-5,exp=now+60,jti=str(uuid4())),"Uninstall")
        with self.assertRaises(PresaleUndoConflict):
            self.verify(result["undo"]["id"])
        self.fx.install.change(self.fx.tenant,dict(iat=now-4,exp=now+60,jti=str(uuid4())),"Install","newsynthetic")
        with self.assertRaises(PresaleUndoConflict):
            self.verify(result["undo"]["id"])
        self.fx.activate()
        self.assertTrue(self.verify(result["undo"]["id"])["undo"]["verified"])
        self.assertEqual(self.balance(),5000)
        self.assertTrue(self.verify(result["undo"]["id"])["undo"]["verified"])
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            row=conn.execute("SELECT c.generation AS receipt,i.generation AS active,q.generation AS original "
                "FROM bonuspro.presale_undo_completions c JOIN bonuspro.presale_undo_requests q "
                "ON q.tenant_id=c.tenant_id AND q.id=c.request_id "
                "JOIN bonuspro.installations i ON i.tenant_id=c.tenant_id").fetchone()
            self.assertEqual(row["receipt"],row["active"])
            self.assertEqual(row["original"],old_generation)
            self.assertNotEqual(row["receipt"],row["original"])

    def test_older_or_same_revision_different_source_cannot_authorize_refund_or_resume(self):
        result=self.prepare()
        for revision in ("2026-10-14 09:59:00.000",self.discounted.snapshot.revision):
            with self.subTest(revision=revision):
                saved=replace(self.restored(),snapshot=replace(self.restored().snapshot,revision=revision))
                with self.assertRaises(PresaleUndoConflict):
                    self.verify(result["undo"]["id"],self.proof(saved))
                with self.assertRaises(PresaleUndoConflict):
                    self.undo.resume(self.fx.tenant,self.fx.order,self.proof(saved),
                        undo_id=result["undo"]["id"],expected_source=saved.source_hash,restore_original=True)
        self.assertEqual(self.balance(),4000)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_pending_buyer_change_explains_original_buyer_then_recovers_after_save(self):
        result=self.prepare()
        changed=replace(self.discounted,snapshot=replace(self.discounted.snapshot,
            counterparty_id=str(uuid4()),revision="2026-10-14 10:01:30.000"))
        reply=self.verify(result["undo"]["id"],self.proof(changed))
        self.assertFalse(reply["undo"]["recoverable"])
        self.assertEqual(reply["undo"]["original_buyer_id"],self.fx.customer)
        restored_buyer=replace(self.discounted,snapshot=replace(self.discounted.snapshot,
            revision="2026-10-14 10:01:40.000"))
        reply=self.verify(result["undo"]["id"],self.proof(restored_buyer))
        self.assertTrue(reply["undo"]["recoverable"])
        resumed=self.undo.resume(self.fx.tenant,self.fx.order,self.proof(restored_buyer),
            undo_id=result["undo"]["id"],expected_source=reply["undo"]["resume_source_hash"])
        self.assertIn("update_state",resumed)
        self.assertTrue(self.verify(result["undo"]["id"])["undo"]["verified"])

    def test_other_identity_changes_do_not_claim_that_buyer_changed(self):
        for changed in (replace(self.discounted,organization_id=str(uuid4())),
                        replace(self.discounted,vat_included=False),
                        replace(self.discounted,snapshot=replace(self.discounted.snapshot,currency="RUB"))):
            hints=self.undo.hints(self.fx.tenant,changed,dict(id=self.applied["id"],state="committed",verified=False))
            self.assertFalse(hints["undo_available"])
            self.assertNotIn("original_buyer_id",hints)

    def test_changed_current_marker_binding_never_hides_the_original_marker(self):
        result=self.prepare()
        # Simulate an externally repaired/new-generation binding. Runtime
        # sessions cannot rewrite bindings; this is a defensive fault fixture.
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("ALTER TABLE bonuspro.widget_bindings DISABLE TRIGGER USER")
            conn.execute("UPDATE bonuspro.widget_bindings SET marker_id=%s WHERE tenant_id=%s",
                         (str(uuid4()),self.fx.tenant))
            conn.execute("ALTER TABLE bonuspro.widget_bindings ENABLE TRIGGER USER")
            conn.execute("UPDATE bonuspro.installations SET generation=%s WHERE tenant_id=%s",
                         (str(uuid4()),self.fx.tenant))
        clean=self.restored()
        with self.assertRaises(PresaleUndoConflict) as rejected:
            self.verify(result["undo"]["id"],self.proof(clean))
        self.assertEqual(rejected.exception.reason,"undo_generation_changed")
        with self.assertRaises(PresaleUndoConflict):
            self.undo.resume(self.fx.tenant,self.fx.order,self.proof(clean),
                undo_id=result["undo"]["id"],expected_source=clean.source_hash,restore_original=True)
        self.assertEqual(self.balance(),4000)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_already_manually_restored_same_source_can_finish_without_an_extra_mutation(self):
        clean=self.restored()
        result=self.prepare(self.proof(clean),restore_original=True)
        reply=self.verify(result["undo"]["id"],self.proof(clean))
        self.assertTrue(reply["undo"]["verified"])
        self.assertEqual(self.balance(),5000)

    def test_sale_history_discovered_after_prepare_prevents_refund(self):
        result=self.prepare()
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            Automation.issue_in(conn,self.fx.tenant,self.fx.order,self.fx.wallet,"changed_credited_order","b"*64)
        with self.assertRaises(PresaleUndoConflict) as rejected:
            self.verify(result["undo"]["id"])
        self.assertEqual(rejected.exception.reason,"undo_sale_locked")
        self.assertEqual(self.balance(),4000)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_refund_capacity_is_checked_again_after_prepare(self):
        result=self.prepare()
        self.fx.ledger.credit(self.fx.tenant,self.fx.wallet,points=MAX_POINTS-4000,
            key=str(uuid4()),actor_id=self.fx.actor,reason="Balance boundary")
        with self.assertRaises(PresaleUndoConflict) as rejected:
            self.verify(result["undo"]["id"])
        self.assertEqual(rejected.exception.reason,"undo_balance_limit")
        self.assertEqual(self.balance(),MAX_POINTS)
        self.assertEqual(self.count("presale_undo_completions"),0)

    def test_authorization_is_rechecked_inside_financial_transaction(self):
        def revoked(conn):
            raise PermissionError("revoked")
        with self.assertRaises(PermissionError):
            self.prepare(authorize=revoked)
        self.assertEqual(self.count("presale_undo_requests"),0)
        result=self.prepare()
        with self.assertRaises(PermissionError):
            self.verify(result["undo"]["id"],authorize=revoked)
        self.assertEqual(self.balance(),4000)

    def test_completed_reverse_allows_new_application_and_buyer_rebind_without_moving_wallets(self):
        result=self.prepare();self.verify(result["undo"]["id"])
        restored=self.restored()
        again=self.fx.redemptions.prepare(self.fx.tenant,restored,points=500,
            request_id=str(uuid4()),actor_id=self.fx.actor,expected_source=restored.source_hash)
        self.assertNotEqual(again["id"],self.applied["id"])
        self.assertEqual(self.balance(),5000)
        # A second, independent fixture proves rebind after terminal refund,
        # before a new active generation again pins the original buyer.
        fx=redemption_fixture.RedemptionTests();fx.setUp()
        service=PresaleUndo(fx.ledger);prepared=fx.prepare();saved=fx.persisted(prepared)
        saved=replace(saved,snapshot=replace(saved.snapshot,revision="2026-10-14 10:01:00.000"))
        fx.redemptions.confirm_saved(fx.tenant,saved)
        proof=replace(self.proof(saved),saved=saved)
        undo=service.prepare(fx.tenant,fx.order,proof,attempt_id=prepared["id"],request_id=str(uuid4()),
                             actor=fx.actor,expected_source=saved.source_hash)
        clean=replace(fx.saved,snapshot=replace(fx.saved.snapshot,revision="2026-10-14 10:02:00.000"))
        service.verify(fx.tenant,fx.order,self.proof(clean),undo_id=undo["undo"]["id"],actor=fx.actor)
        customer=str(uuid4())
        target=replace(clean.snapshot,counterparty_id=customer,revision="2026-10-14 10:03:00.000")
        quote=fx.checkout.preview(fx.tenant,target,requested_points=0,actor_id=fx.actor,eligibility_version=1)
        self.assertEqual(quote["counterparty_id"],customer)
        self.assertNotEqual(quote["wallet_id"],fx.wallet)
        self.assertEqual(fx.ledger.snapshot(fx.tenant,fx.wallet)["balance"],5000)
        self.assertEqual(fx.ledger.snapshot(fx.tenant,quote["wallet_id"])["balance"],0)

    def test_worker_read_before_undo_cannot_create_false_issue_after_completion(self):
        automation=Automation(self.fx.ledger);automation.configure(self.fx.tenant,self.fx.actor)
        old_proof=self.proof()
        result=self.prepare();self.verify(result["undo"]["id"])
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            automation.apply_in(conn,self.fx.tenant,{"entity_id":self.fx.order},old_proof)
        self.assertEqual(self.count("reconciliation_issues"),0)
        self.assertEqual(self.count("order_accruals"),0)
        self.assertEqual(self.balance(),5000)
        # An older UNMARKED sale proof also cannot post after the reversal.
        stale=replace(self.original,snapshot=replace(self.original.snapshot,
            revision="2026-10-14 10:00:30.000",paid_minor=1200000,shipped_minor=1200000))
        from bonuspro.adapters.postgres.ledger import Conflict
        with self.assertRaises(Conflict):
            with self.fx.ledger.transaction(self.fx.tenant) as conn:
                automation.apply_in(conn,self.fx.tenant,{"entity_id":self.fx.order},
                    self.proof(stale,paid_minor=1200000,shipped_minor=1200000,outcome="eligible",
                               full_payment=True,full_shipment=True,presale_safe=False))
        self.assertEqual(self.count("order_accruals"),0)
        self.assertEqual(self.count("reconciliation_issues"),0)
        newer=replace(self.discounted,snapshot=replace(self.discounted.snapshot,revision="2026-10-14 10:03:00.000"))
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            automation.apply_in(conn,self.fx.tenant,{"entity_id":self.fx.order},self.proof(newer))
        self.assertEqual(self.count("reconciliation_issues"),1)
        self.assertEqual(self.balance(),5000)


class PresaleUndoPersonalTests(unittest.TestCase):
    def run_personal(self,points):
        import test_personal_applications as personal_fixture
        test=personal_fixture.PersonalApplicationTests()
        test.setUp();self.addCleanup(test.doCleanups)
        prepared,_=test.prepare(points=points)
        saved=test.persist(prepared);self.assertTrue(test.verify()["verified"])
        with test.fx.ledger.transaction(test.fx.tenant) as conn:
            frozen=conn.execute("SELECT plan FROM bonuspro.personal_discount_applications").fetchone()["plan"]
            manifest=frozen["reconstruction"]
            self.assertEqual(manifest["version"],1)
            original={line.id:line.record() for line in test.original.lines}
            self.assertEqual({row["id"] for row in manifest["rows"]},set(original))
            for row in manifest["rows"]:
                self.assertEqual(row["fields"],{k:v for k,v in original[row["id"]].items() if k!="id"})
                self.assertEqual(row["recreatable"],original[row["id"]]["kind"]=="service")
        service=PresaleUndo(test.fx.ledger)
        def proof(value):
            return Fulfillment(value,datetime.now(timezone.utc),0,0,"waiting_shipment","c"*64,
                               time.monotonic()+120,presale_safe=True)
        result=service.prepare(test.fx.tenant,test.fx.order,proof(saved),attempt_id=prepared["id"],
            request_id=str(uuid4()),actor=test.fx.actor,expected_source=saved.source_hash)
        clean=replace(test.original,snapshot=replace(test.original.snapshot,revision="2026-10-19 10:03:00.000"))
        completion=service.verify(test.fx.tenant,test.fx.order,proof(clean),undo_id=result["undo"]["id"],actor=test.fx.actor)
        self.assertTrue(completion["undo"]["verified"])
        self.assertEqual(test.balance()["balance"],5000)
        with test.fx.ledger.transaction(test.fx.tenant) as conn:
            row=conn.execute("SELECT state,proof_revision FROM bonuspro.personal_discount_applications").fetchone()
            self.assertEqual(row["state"],"voided");self.assertIsNotNone(row["proof_revision"])
            count=conn.execute("SELECT count(*) n FROM bonuspro.ledger_transactions WHERE kind='presale_refund'").fetchone()["n"]
            self.assertEqual(count,1 if points else 0)
        replay=service.verify(test.fx.tenant,test.fx.order,proof(clean),undo_id=result["undo"]["id"],actor=test.fx.actor)
        self.assertTrue(replay["undo"]["verified"]);self.assertEqual(test.balance()["balance"],5000)

    def test_personal_only_undo_never_creates_a_points_posting(self):
        self.run_personal(0)

    def test_combined_undo_reverses_both_parts_with_one_refund(self):
        self.run_personal(1000)


class PresaleUndoAPITests(unittest.TestCase):
    def setUp(self):
        self.widget=widget_fixture.WidgetAPITests();self.widget.setUp()
        self.addCleanup(self.widget.doCleanups)
        self.fx=self.widget.fx
        outer=self
        class Provider:
            def __init__(self,token,tenant):self.tenant=tenant
            def entity(self,*args):return {"isoCode":"KZT"}
            def order_evidence(self,identity):
                saved=outer.fx.saved;s=saved.snapshot
                ref=widget_fixture.ref
                doc=dict(id=s.order_id,agent=ref("counterparty",s.counterparty_id),
                    organization=ref("organization",saved.organization_id),rate={"currency":ref("currency",outer.widget.currency)},
                    vatEnabled=saved.vat_enabled,vatIncluded=saved.vat_included,sum=s.total_minor,
                    payedSum=s.paid_minor,shippedSum=s.shipped_minor,updated=s.revision,
                    created="2026-10-14 09:59:00.000",applicable=not s.cancelled,
                    attributes=[{"id":outer.fx.marker,"value":saved.marker}] if saved.marker else [],name=saved.name)
                lines=tuple(dict(id=x.id,assortment=ref(x.kind,x.assortment),price=x.price,quantity=x.quantity,
                    discount=Decimal(x.discount_bps)/100,vat=x.vat,vatEnabled=x.vat_enabled) for x in saved.lines)
                return OrderEvidence(doc,lines,(),())
        config=Settings("https://testserver",self.fx.install.config,self.fx.ledger.dsn,self.fx.install.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=self.fx.install,remap_factory=Provider),base_url="https://testserver")
        self.addCleanup(self.client.close);self.path=self.widget.path;self.headers=self.widget.headers
        self.fx.saved=replace(self.fx.saved,lines=tuple(replace(line,price=line.price.normalize(),
            quantity=line.quantity.normalize(),vat=line.vat.normalize()) for line in self.fx.saved.lines))
        self.original=self.fx.saved;self.applied=self.fx.prepare()
        self.fx.saved=replace(self.fx.persisted(self.applied),
            snapshot=replace(self.fx.persisted(self.applied).snapshot,revision="2026-10-14 10:01:00.000"))
        self.fx.redemptions.confirm_saved(self.fx.tenant,self.fx.saved)

    def test_real_api_uses_provider_reads_and_pending_wrapper_until_saved_inverse(self):
        verified=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(verified.status_code,200,verified.text)
        self.assertTrue(verified.json()["undo_available"])
        body=dict(request_id=str(uuid4()),attempt_id=self.applied["id"],source_hash=verified.json()["undo_source_hash"])
        prepared=self.client.post(self.path+"/undo/prepare",headers=self.headers,json=body)
        self.assertEqual(prepared.status_code,200,prepared.text)
        identity=prepared.json()["undo"]["id"]
        pending=self.client.get(self.path,headers=self.headers)
        self.assertEqual(pending.json()["undo"]["id"],identity)
        before=self.client.post(self.path+"/undo/verify",headers=self.headers,json={"undo_id":identity})
        self.assertTrue(before.json()["undo"]["recoverable"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],4000)
        self.fx.saved=replace(self.original,snapshot=replace(self.original.snapshot,revision="2026-10-14 10:02:00.000"))
        after=self.client.post(self.path+"/undo/verify",headers=self.headers,json={"undo_id":identity})
        self.assertEqual(after.status_code,200,after.text);self.assertTrue(after.json()["undo"]["verified"])
        fresh=self.client.get(self.path,headers=self.headers)
        self.assertIn("quote",fresh.json())
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],5000)

    def test_browser_proof_fields_and_non_boolean_restore_are_rejected_without_writes(self):
        body=dict(request_id=str(uuid4()),attempt_id=self.applied["id"],source_hash=self.fx.saved.source_hash)
        for extra in ({"verified":True},{"restore_original":"true"},{"proof":{"paid_minor":0}}):
            response=self.client.post(self.path+"/undo/prepare",headers=self.headers,json={**body,**extra})
            self.assertEqual(response.status_code,422,response.text)
        response=self.client.post(self.path+"/undo/prepare",headers={**self.headers,"Origin":"https://foreign.test"},json=body)
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],4000)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.presale_undo_requests").fetchone()["n"],0)
