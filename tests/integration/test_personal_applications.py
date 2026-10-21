"""Real provider JSON and PostgreSQL personal-only/combined discount lifecycle."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import psycopg
import test_widget_provider_contract as provider_fixture
from bonuspro.adapters.moysklad.fulfillment import Fulfillment
from bonuspro.adapters.moysklad.order import SavedOrder
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.postgres.personal_discounts import PersonalDiscounts
from bonuspro.adapters.postgres.voids import Voids


class PersonalApplicationTests(unittest.TestCase):
    def setUp(self):
        self.widget=provider_fixture.WidgetProviderContractTests()
        self.widget.setUp()
        self.addCleanup(self.widget.doCleanups)
        self.fx=self.widget.fx
        self.fx.saved=replace(self.fx.saved,lines=tuple(replace(line,price=line.price.normalize(),
            quantity=line.quantity.normalize(),vat=line.vat.normalize()) for line in self.fx.saved.lines))
        self.settings=PersonalDiscounts(self.fx.ledger)
        self.policy=self.settings.save_policy(self.fx.tenant,enabled=True,external_policy="skip",allow_bonus_stack=True,
            expected_version=0,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Approved personal policy")
        self.assignment=self.settings.save_assignment(self.fx.tenant,self.fx.customer,rate_bps=1000,
            expected_version=0,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Assigned customer rate")
        self.original=self.fx.saved

    def get(self):
        response=self.widget.client.get(self.widget.path,headers=self.widget.headers)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def prepare(self,points=0,request=None,quote=None):
        quoted=quote or self.get()
        personal=quoted["personal_discount"]
        body=dict(points=points,apply_personal=True,expected_policy_version=personal["policy_version"],
                  expected_assignment_version=personal["assignment_version"],request_id=request or str(uuid4()),
                  source_hash=quoted["saved_source_hash"])
        response=self.widget.client.post(self.widget.path+"/prepare",headers=self.widget.headers,json=body)
        self.assertEqual(response.status_code,200,response.text)
        return response.json(),body

    def persist(self,prepared):
        updates={line["id"]:line for line in prepared["update_state"]["positions"]}
        lines=[]
        for line in self.fx.saved.lines:
            update=updates[line.id]
            discount=int(Decimal(str(update["discount"]))*100) if "discount" in update else line.discount_bps
            lines.append(replace(line,price=Decimal(str(update.get("price",line.price))).normalize(),
                                 discount_bps=discount,quantity=line.quantity.normalize(),vat=line.vat.normalize()))
        self.fx.saved=replace(self.fx.saved,lines=tuple(lines),
            marker=prepared["update_state"]["attributes"][0]["value"],
            snapshot=replace(self.fx.saved.snapshot,revision="2026-10-19 10:02:00.000",
                total_minor=sum(line.amount for line in lines),
                eligible_minor=sum(line.amount for line in lines if line.kind!="service")))
        return self.fx.saved

    def verify(self):
        response=self.widget.client.post(self.widget.path+"/verify",headers=self.widget.headers)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def balance(self):return self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)

    def count(self,table):
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            return conn.execute("SELECT count(*) AS n FROM bonuspro."+table).fetchone()["n"]

    def process(self,quantity=None):
        automation=Automation(self.fx.ledger)
        automation.configure(self.fx.tenant,self.fx.actor)
        saved=self.fx.saved
        returned=() if quantity is None else (replace(saved.lines[0],id=str(uuid4()),quantity=Decimal(str(quantity))),)
        proof=Fulfillment(saved,datetime.now(timezone.utc)+timedelta(seconds=1),
            saved.snapshot.total_minor,saved.snapshot.total_minor,"return_review" if returned else "eligible",
            "a"*64,time.monotonic()+120,returned=returned,full_payment=True,full_shipment=True)
        automation.jobs.enqueue(self.fx.tenant,order_id=self.fx.order,key=str(uuid4()))
        job=automation.jobs.claim(self.fx.tenant,lease_seconds=300)
        automation.complete(self.fx.tenant,job,proof)

    def test_preview_uses_after_personal_base_without_changing_binding_or_wallet(self):
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            original=conn.execute("SELECT * FROM bonuspro.order_contexts WHERE order_id=%s",(self.fx.order,)).fetchone()
        quoted=self.get();personal=quoted["personal_discount"]
        self.assertEqual((quoted["total_minor"],quoted["eligible_minor"]),(1200000,1000000))
        self.assertEqual((personal["discount_minor"],personal["total_minor_after"],personal["eligible_minor_after"]),
                         (100000,1100000,900000))
        self.assertEqual((personal["quote"]["potential_earn_points"],personal["quote"]["max_spend_points"]),(450,2700))
        self.assertEqual(self.balance(),dict(balance=5000,reserved=0,available=5000))
        self.assertEqual(self.count("personal_discount_applications"),0)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            current=conn.execute("SELECT * FROM bonuspro.order_contexts WHERE order_id=%s",(self.fx.order,)).fetchone()
        self.assertEqual(current,original)

    def test_personal_only_save_confirms_without_reservation_or_points_posting(self):
        prepared,_=self.prepare()
        self.assertEqual((prepared["points"],prepared["apply_personal"],prepared["personal_discount_minor"]),(0,True,100000))
        self.assertEqual(prepared["update_state"]["positions"],
                         [{"id":self.fx.saved.lines[0].id,"discount":10.0},{"id":self.fx.saved.lines[1].id}])
        self.assertEqual(self.count("reservations"),0)
        self.assertFalse(self.verify()["verified"])
        self.persist(prepared)
        checked=self.verify()
        self.assertTrue(checked["verified"]);self.assertIsNone(checked["transaction_id"])
        self.assertEqual(self.verify()["state"],"committed")
        self.assertEqual(self.balance(),dict(balance=5000,reserved=0,available=5000))
        self.assertEqual(self.count("ledger_transactions"),1)
        self.assertEqual(self.count("redemption_attempts"),0)
        self.fx.saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,
            paid_minor=self.fx.saved.snapshot.total_minor,shipped_minor=self.fx.saved.snapshot.total_minor))
        self.widget.linked=True
        self.assertTrue(self.verify()["verified"])
        self.assertTrue(all(request.method=="GET" for request in self.widget.calls))

    def test_combined_single_patch_pins_three_financial_snapshots_and_debits_once(self):
        prepared,_=self.prepare(points=1000)
        self.assertEqual(prepared["update_state"]["positions"],
                         [{"id":self.fx.saved.lines[0].id,"discount":20.0},{"id":self.fx.saved.lines[1].id}])
        self.assertEqual(self.balance()["reserved"],1000)
        self.persist(prepared)
        first=self.verify();second=self.verify()
        self.assertTrue(first["verified"]);self.assertEqual(first["transaction_id"],second["transaction_id"])
        self.assertEqual(self.balance(),dict(balance=4000,reserved=0,available=4000))
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            app=conn.execute("SELECT * FROM bonuspro.personal_discount_applications").fetchone()
            attempt=conn.execute("SELECT * FROM bonuspro.redemption_attempts").fetchone()
        self.assertEqual(str(app["id"]),str(attempt["id"]))
        self.assertEqual((app["plan"]["before"]["total_minor"],app["plan"]["personal_basis"]["total_minor"],
                          app["plan"]["after"]["total_minor"]),(1200000,1100000,1000000))
        self.assertEqual(app["plan"]["bonus_before"],app["plan"]["personal_basis"])
        self.assertEqual(app["plan"],attempt["plan"])

    def test_settings_changes_after_prepare_do_not_reprice_saved_application(self):
        prepared,_=self.prepare(points=1000)
        self.settings.save_policy(self.fx.tenant,enabled=False,external_policy="best",allow_bonus_stack=False,
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Future settings")
        self.settings.save_assignment(self.fx.tenant,self.fx.customer,rate_bps=3000,
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Future assignment")
        self.persist(prepared)
        self.assertTrue(self.verify()["verified"])
        self.process()
        self.assertEqual(self.balance()["balance"],4450)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            app=conn.execute("SELECT policy_version,assignment_version,rate_bps FROM bonuspro.personal_discount_applications").fetchone()
        self.assertEqual(app,dict(policy_version=1,assignment_version=1,rate_bps=1000))

    def test_stale_policy_or_assignment_quote_leaves_no_application_or_reservation(self):
        quoted=self.get()
        self.settings.save_assignment(self.fx.tenant,self.fx.customer,rate_bps=2000,
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Changed before prepare")
        personal=quoted["personal_discount"]
        rejected=self.widget.client.post(self.widget.path+"/prepare",headers=self.widget.headers,json=dict(
            points=1000,apply_personal=True,expected_policy_version=personal["policy_version"],
            expected_assignment_version=personal["assignment_version"],request_id=str(uuid4()),
            source_hash=quoted["saved_source_hash"]))
        self.assertEqual(rejected.status_code,409,rejected.text)
        self.assertEqual((self.count("personal_discount_applications"),self.count("reservations")),(0,0))

    def test_stacking_disabled_still_allows_personal_only_and_blocks_combined(self):
        self.settings.save_policy(self.fx.tenant,enabled=True,external_policy="skip",allow_bonus_stack=False,
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Do not combine")
        quoted=self.get();personal=quoted["personal_discount"]
        self.assertEqual(personal["quote"]["max_spend_points"],0)
        rejected=self.widget.client.post(self.widget.path+"/prepare",headers=self.widget.headers,json=dict(
            points=1,apply_personal=True,expected_policy_version=personal["policy_version"],
            expected_assignment_version=personal["assignment_version"],request_id=str(uuid4()),
            source_hash=quoted["saved_source_hash"]))
        self.assertEqual(rejected.status_code,409,rejected.text)
        prepared,_=self.prepare(quote=quoted)
        self.assertEqual(prepared["points"],0)

    def test_saved_tamper_blocks_confirmation_and_preserves_only_bonus_hold(self):
        prepared,_=self.prepare(points=1000);exact=self.persist(prepared)
        changed=replace(exact.lines[0],price=(exact.lines[0].price+100).normalize())
        self.fx.saved=replace(exact,lines=(changed,exact.lines[1]),snapshot=replace(exact.snapshot,
            eligible_minor=changed.amount,total_minor=changed.amount+exact.lines[1].amount))
        self.assertFalse(self.verify()["verified"])
        self.assertEqual(self.balance(),dict(balance=5000,reserved=1000,available=4000))
        self.fx.saved=exact;self.assertTrue(self.verify()["verified"])

    def test_personal_only_recovery_replays_pinned_plan_without_reserving_points(self):
        prepared,_=self.prepare()
        self.fx.saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,revision="2026-10-19 10:01:00.000"))
        checked=self.verify();self.assertFalse(checked["verified"])
        response=self.widget.client.post(self.widget.path+"/resume",headers=self.widget.headers,json=dict(
            attempt_id=prepared["id"],source_hash=checked["resume_source_hash"]))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["update_state"],prepared["update_state"])
        self.assertEqual(self.count("reservations"),0)
        self.persist(prepared);self.assertTrue(self.verify()["verified"])

    def test_prepare_retry_is_no_dispatch_and_cross_type_duplicate_is_blocked(self):
        prepared,body=self.prepare()
        retry=self.widget.client.post(self.widget.path+"/prepare",headers=self.widget.headers,json=body)
        self.assertEqual(retry.status_code,200,retry.text)
        self.assertIsNone(retry.json()["update_state"])
        self.assertEqual(retry.json()["id"],prepared["id"])
        with self.assertRaises(Conflict):self.fx.prepare()
        self.assertEqual((self.count("personal_discount_applications"),self.count("reservations")),(1,0))

    def test_personal_only_worker_recovers_saved_marker_and_returns_after_personal_base(self):
        prepared,_=self.prepare();self.persist(prepared)
        self.process()
        self.assertEqual(self.balance()["balance"],5450)
        self.assertEqual(self.fx.redemptions.existing(self.fx.tenant,self.fx.order)["state"],"committed")
        self.process("0.5");self.assertEqual(self.balance()["balance"],5225)
        self.process(1);self.assertEqual(self.balance()["balance"],5000)
        self.assertEqual(self.count("reservations"),0)

    def test_combined_worker_and_returns_refund_only_bonus_not_personal_discount(self):
        prepared,_=self.prepare(points=1000);self.persist(prepared)
        self.process()
        self.assertEqual(self.balance()["balance"],4450)
        self.process("0.5");self.process("0.5")
        self.assertEqual(self.balance()["balance"],4725)
        self.process(1)
        self.assertEqual(self.balance()["balance"],5000)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            totals=conn.execute("SELECT sum(earn_delta) AS earn,sum(refund_delta) AS refund FROM bonuspro.settlement_events").fetchone()
        self.assertEqual((int(totals["earn"]),int(totals["refund"])),(-450,1000))

    def age(self,identity):
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            assert conn.info.dbname=="bonuspro_ci"
            conn.execute("SET LOCAL session_replication_role=replica")
            conn.execute("UPDATE bonuspro.personal_discount_applications SET created_at=clock_timestamp()-interval '16 minutes' "
                         "WHERE tenant_id=%s AND id=%s",(self.fx.tenant,identity))
            conn.execute("UPDATE bonuspro.reservations SET created_at=clock_timestamp()-interval '16 minutes' "
                         "WHERE tenant_id=%s AND id=%s",(self.fx.tenant,identity))

    def cancelled(self,saved):
        return Fulfillment(replace(saved,marker=None,snapshot=replace(saved.snapshot,cancelled=True)),
            datetime.now(timezone.utc),0,0,"cancelled","b"*64,time.monotonic()+120,cancel_safe=True)

    def test_personal_only_void_requires_original_restoration_and_immutable_audit(self):
        prepared,_=self.prepare();discounted=self.persist(prepared);self.age(prepared["id"])
        service=Voids(self.fx.ledger)
        with self.assertRaises(Conflict):
            service.release(self.fx.tenant,prepared["id"],self.cancelled(discounted),actor=self.fx.actor,reason="Restore first")
        result=service.release(self.fx.tenant,prepared["id"],self.cancelled(self.original),
                               actor=self.fx.actor,reason="Cancelled and original restored")
        self.assertEqual(result,dict(state="released"))
        self.assertEqual(self.count("personal_discount_voids"),1)
        self.assertEqual(self.count("reservations"),0)
        self.assertEqual(self.balance()["balance"],5000)
        with self.assertRaises(psycopg.Error):
            with self.fx.ledger.transaction(self.fx.tenant) as conn:
                conn.execute("DELETE FROM bonuspro.personal_discount_voids")

    def test_combined_void_releases_both_records_atomically_and_keeps_history(self):
        prepared,_=self.prepare(points=1000);self.age(prepared["id"])
        Voids(self.fx.ledger).release(self.fx.tenant,prepared["id"],self.cancelled(self.original),
                                     actor=self.fx.actor,reason="Cancelled and original restored")
        self.assertEqual(self.balance(),dict(balance=5000,reserved=0,available=5000))
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.personal_discount_applications").fetchone()["state"],"voided")
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.reservations").fetchone()["state"],"released")
        self.assertEqual((self.count("personal_discount_voids"),self.count("redemption_voids")),(1,1))
        changed=replace(self.original.snapshot,counterparty_id=str(uuid4()),revision="2026-10-19 10:05:00.000")
        with self.assertRaises(Conflict):
            self.fx.checkout.preview(self.fx.tenant,changed,requested_points=0,actor_id=self.fx.actor,eligibility_version=1)

    def test_late_voided_personal_marker_blocks_then_restored_original_can_be_reviewed(self):
        prepared,_=self.prepare();discounted=self.persist(prepared);self.age(prepared["id"])
        reason="Cancelled and original restored"
        Voids(self.fx.ledger).release(self.fx.tenant,prepared["id"],self.cancelled(self.original),
                                     actor=self.fx.actor,reason=reason)
        self.fx.saved=discounted
        self.process()
        automation=Automation(self.fx.ledger)
        issues=automation.dashboard(self.fx.tenant)["issues"]
        self.assertEqual(len(issues),1)
        self.assertEqual(issues[0]["reason"],"unrecognized_bonus_marker")
        self.assertEqual(self.balance()["balance"],5000)
        automation.resolve_issue(self.fx.tenant,str(issues[0]["id"]),self.cancelled(self.original),
            actor=self.fx.actor,reason="Original prices and customer restored after late host update")
        self.assertEqual(automation.dashboard(self.fx.tenant)["issues"],[])
        self.assertEqual(self.balance(),dict(balance=5000,reserved=0,available=5000))

    def test_personal_application_and_plan_are_tenant_isolated_and_immutable(self):
        prepared,_=self.prepare()
        with self.fx.ledger.transaction(str(uuid4())) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.personal_discount_applications").fetchone()["n"],0)
        with self.assertRaises(psycopg.Error):
            with self.fx.ledger.transaction(self.fx.tenant) as conn:
                conn.execute("UPDATE bonuspro.personal_discount_applications SET plan='{}' WHERE id=%s",(prepared["id"],))

    def test_newer_same_buyer_observation_blocks_old_personal_and_bonus_prepare(self):
        newer=replace(self.fx.saved.snapshot,revision="2026-10-19 10:05:00.000")
        self.fx.checkout.preview(self.fx.tenant,newer,requested_points=0,actor_id=self.fx.actor,eligibility_version=1)
        with self.assertRaises(Conflict):self.fx.prepare(points=1)
        with self.assertRaises(Conflict):
            self.fx.redemptions.prepare(self.fx.tenant,self.fx.saved,points=0,request_id=str(uuid4()),
                actor_id=self.fx.actor,expected_source=self.fx.saved.source_hash,apply_personal=True,
                expected_policy_version=1,expected_assignment_version=1)
        self.assertEqual((self.count("personal_discount_applications"),self.count("reservations")),(0,0))

    def test_paused_old_bonus_read_cannot_overtake_newer_same_buyer_preview(self):
        planned=Event();proceed=Event();original_plan=SavedOrder.plan
        def paused(order,points):
            result=original_plan(order,points);planned.set()
            if not proceed.wait(10):raise RuntimeError("newer observation did not finish")
            return result
        with patch.object(SavedOrder,"plan",paused):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(self.fx.prepare,points=1)
                self.assertTrue(planned.wait(10))
                try:
                    self.fx.checkout.preview(self.fx.tenant,replace(self.fx.saved.snapshot,revision="2026-10-19 10:05:00.000"),
                        requested_points=0,actor_id=self.fx.actor,eligibility_version=1)
                finally:proceed.set()
                with self.assertRaises(Conflict):pending.result(timeout=15)
        self.assertEqual(self.balance()["reserved"],0)


if __name__=="__main__":unittest.main()
