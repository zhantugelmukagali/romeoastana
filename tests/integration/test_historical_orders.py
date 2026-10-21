"""Worker-to-PostgreSQL fallback for unowned pre-program unsupported documents."""
import copy
import io
import json
import os
import time
import unittest
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from uuid import uuid4
import httpx
import psycopg
import test_automation as fixtures
from bonuspro.worker import Worker
from bonuspro.domain.finance import Rules
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader,MSK
from bonuspro.adapters.moysklad.remap import BASE,RemapClient,InvalidDocument
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.postgres.readiness import Readiness
from bonuspro.adapters.postgres.returns import Returns


def ref(kind,identity):
    return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+identity}}


class HistoricalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.base=fixtures.AutomationTests();self.base.setUp()
        self.fx=self.base.fx;self.auto=self.base.a;self.tenant=self.fx.tenant
        # The fixture preview also schedules its existing order.
        # Check it through the real non-earning path before focusing this worker
        # on a different historical order; do not fake a monitor outcome.
        self.base.run_proof(self.base.proof(outcome="waiting_shipment"))
        self.assertEqual(self.fx.ledger.snapshot(self.tenant,self.fx.wallet)["balance"],5000)
        self.order,self.currency,self.line=(str(uuid4()) for _ in range(3))
        self.cutoff=self.auto.state(self.tenant)["starts_at"]
        self.requests=[];self.entity_reads=0;self.on_entity=None;self.related={};self.invalid_positions=False
        self.doc=dict(id=self.order,accountId=self.tenant,meta=ref("customerorder",self.order)["meta"],
            applicable=True,sum=10000,payedSum=0,shippedSum=0,
            created=self.stamp(self.cutoff-timedelta(days=1)),updated=self.stamp(self.cutoff+timedelta(seconds=1)),
            agent=ref("counterparty",self.fx.customer),organization=ref("organization",self.fx.saved.organization_id),
            rate={"currency":ref("currency",self.currency),"value":1},vatEnabled=False,vatIncluded=True,
            attributes=[],name="private historical document")
        self.rows=[dict(id=self.line,accountId=self.tenant,assortment=ref("product",str(uuid4())),
                        quantity=1,price=10000,discount=0,vat=0,vatEnabled=False)]
        self.hint()
    def stamp(self,value):
        return value.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    def hint(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.auto.hint_in(conn,self.tenant,self.order)
    def provider(self,request):
        self.assertEqual(request.method,"GET");self.requests.append(request)
        path=request.url.path
        if path in self.related:return httpx.Response(200,json=copy.deepcopy(self.related[path]))
        if path=="/api/remap/1.2/entity/customerorder/"+self.order:
            self.entity_reads+=1
            if self.on_entity:self.on_entity(self.entity_reads)
            return httpx.Response(200,json=copy.deepcopy(self.doc))
        if path.endswith("/customerorder/"+self.order+"/positions"):
            return httpx.Response(200,json={"meta":{"size":1},"rows":None if self.invalid_positions else copy.deepcopy(self.rows)})
        if path.endswith("/currency/"+self.currency):
            return httpx.Response(200,json=dict(id=self.currency,meta=ref("currency",self.currency)["meta"],isoCode="RUB",code="643"))
        if path.startswith("/api/remap/1.2/entity/") and len(path[len("/api/remap/1.2/entity/"):].split("/"))==1:
            return httpx.Response(200,json={"meta":{"size":0},"rows":[]})
        raise AssertionError("unexpected provider path")
    def client(self,token=None,tenant=None,**options):
        options["minimum_interval"]=0
        return RemapClient(token or "synthetic",tenant or self.tenant,
                           transport=httpx.MockTransport(self.provider),**options)
    def worker(self):
        return Worker(self.fx.ledger,self.fx.install,self.tenant,client_factory=self.client)
    def cycle(self):
        return self.worker().cycle(max_jobs=1)
    def monitor(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT * FROM bonuspro.order_monitors WHERE order_id=%s",(self.order,)).fetchone()
    def jobs(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return conn.execute("SELECT state,last_error FROM bonuspro.jobs WHERE entity_id=%s ORDER BY created_at,id",
                                (self.order,)).fetchall()
    def finances(self):
        with self.fx.ledger.transaction(self.tenant) as conn:
            return {table:conn.execute("SELECT to_jsonb(t) r FROM bonuspro."+table+" t ORDER BY to_jsonb(t)::text").fetchall()
                    for table in ("wallets","customer_wallets","order_contexts","reservations","redemption_attempts",
                        "personal_discount_applications","ledger_transactions","ledger_postings","order_accruals",
                        "settlement_events","settlement_cases","reconciliation_issues","presale_undo_requests",
                        "presale_undo_completions","return_cases")}
    def candidate_proof(self):
        candidate=self.auto.historical_candidate(self.tenant,self.order)
        self.assertIsNotNone(candidate)
        reader=FulfillmentReader(self.client())
        with self.assertRaises(InvalidDocument):reader.read(self.order,marker_id=str(candidate["marker_id"]))
        return reader.historical(self.order,before=candidate["starts_at"],
            generation=str(candidate["generation"]),marker_id=str(candidate["marker_id"]))
    def claim(self):
        self.auto.schedule(self.tenant)
        job=self.auto.jobs.claim(self.tenant,lease_seconds=300)
        self.assertEqual(str(job["entity_id"]),self.order)
        return job

    def test_old_unowned_rub_is_historical_without_enrolment_money_or_provider_error(self):
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.jobs(),[dict(state="done",last_error=None)])
        self.assertEqual(self.finances(),before)
        report=Readiness(self.fx.ledger).report(self.tenant)
        self.assertEqual(report["attention"]["provider_errors"],0)
        self.assertTrue(report["ready"])

    def test_current_rub_order_still_requires_review_without_financial_changes(self):
        self.doc["created"]=self.stamp(self.cutoff+timedelta(seconds=1))
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.jobs(),[dict(state="dead",last_error="invalid_document")])
        self.assertEqual(self.finances(),before)
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),1)
        self.assertEqual(self.monitor()["outcome"],"provider_error")  # wake is NOT classification
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.finances(),before)

    def test_old_preview_context_and_released_history_are_never_ignored(self):
        snapshot=replace(self.fx.saved.snapshot,order_id=self.order,revision=self.doc["updated"],
                         total_minor=10000,eligible_minor=10000)
        self.fx.checkout.preview(self.tenant,snapshot,requested_points=0,actor_id=self.fx.actor,eligibility_version=1)
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertIsNone(self.auto.historical_candidate(self.tenant,self.order))
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),0)
        self.assertEqual(self.finances(),before)
        other=str(uuid4());self.order=other
        self.doc.update(id=other,meta=ref("customerorder",other)["meta"]);self.hint()
        reservation=self.fx.ledger.reserve(self.tenant,self.fx.wallet,order_id=other,points=10,
            eligible_minor=10000,rules=Rules(500,3000),quote_hash="e"*64,key=str(uuid4()),actor_id=self.fx.actor)
        self.fx.ledger.release_unapplied(self.tenant,reservation)
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),0)
        self.assertEqual(self.finances(),before)

    def test_existing_parse_error_recovers_via_bounded_deployment_hint_then_fresh_worker_proof(self):
        before=self.finances()
        # Reproduce the old version's real parse-error path, without forcing
        # monitor/job state or editing any financial record in SQL.
        with patch("bonuspro.worker.FulfillmentReader.historical",return_value=None):
            self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.jobs(),[dict(state="dead",last_error="invalid_document")])
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant,limit=20),1)
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.finances(),before)
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.jobs(),[dict(state="dead",last_error="invalid_document"),dict(state="done",last_error=None)])
        self.assertEqual(self.finances(),before)
        self.assertEqual(Readiness(self.fx.ledger).report(self.tenant)["attention"]["provider_errors"],0)
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),0)

    def test_marker_and_noncanonical_marker_reference_cannot_be_hidden_by_historical_fallback(self):
        for attributes in ([dict(id=self.fx.marker.upper(),value="bonuspro:v1:synthetic")],
            [dict(meta={"href":BASE+"/entity/customerorder/metadata/attributes/"+self.fx.marker+"?hidden=1"},value=None)]):
            with self.subTest(attributes=attributes):
                self.doc["attributes"]=attributes
                self.hint()
                before=self.finances()
                self.assertEqual(self.cycle(),"processed")
                self.assertEqual(self.monitor()["outcome"],"provider_error")
                self.assertEqual(self.finances(),before)

    def test_context_created_during_metadata_read_prevents_acknowledgement(self):
        before_balance=self.fx.ledger.snapshot(self.tenant,self.fx.wallet)
        def race(reads):
            if reads==6:
                snapshot=replace(self.fx.saved.snapshot,order_id=self.order,revision=self.doc["updated"],
                                 total_minor=10000,eligible_minor=10000)
                self.fx.checkout.preview(self.tenant,snapshot,requested_points=0,actor_id=self.fx.actor,eligibility_version=1)
        self.on_entity=race
        self.assertEqual(self.cycle(),"processed")
        self.assertNotEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.jobs(),[dict(state="queued",last_error="conflict")])
        self.assertIsNone(self.auto.historical_candidate(self.tenant,self.order))
        self.assertEqual(self.fx.ledger.snapshot(self.tenant,self.fx.wallet),before_balance)

    def test_generation_changed_during_metadata_read_prevents_acknowledgement(self):
        before=self.finances()
        def race(reads):
            if reads==6:
                with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
                    conn.execute("UPDATE bonuspro.installations SET generation=%s WHERE tenant_id=%s",
                                 (str(uuid4()),self.tenant))
        self.on_entity=race
        self.assertEqual(self.cycle(),"processed")
        self.assertNotEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.jobs(),[dict(state="queued",last_error="conflict")])
        self.assertEqual(self.finances(),before)

    def test_expired_proof_cutoff_binding_or_revision_never_completes_a_job(self):
        proof=self.candidate_proof();job=self.claim()
        before=self.finances()
        variants=(replace(proof,deadline=0),replace(proof,generation=str(uuid4())),
            replace(proof,cutoff=proof.cutoff+timedelta(seconds=1)),replace(proof,marker_id=str(uuid4())),
            replace(proof,revision="2019-01-01 00:00:00.000"))
        for invalid in variants:
            with self.subTest(invalid=invalid.revision),self.assertRaises(Conflict):
                self.auto.complete_historical(self.tenant,job,invalid)
            self.assertNotEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.finances(),before)
        self.assertEqual(self.jobs(),[dict(state="running",last_error=None)])

    def test_expired_or_replaced_lease_cannot_acknowledge_historical_metadata(self):
        proof=self.candidate_proof();job=self.claim();before=self.finances()
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job["id"],))
        with self.assertRaises(Conflict):self.auto.complete_historical(self.tenant,job,proof)
        self.assertNotEqual(self.monitor()["outcome"],"historical")
        self.assertEqual(self.finances(),before)

    def test_deployment_retry_limit_and_inactive_installation_are_safe(self):
        for limit in (0,21,True):
            with self.assertRaises(ValueError):self.auto.wake_historical_candidates(self.tenant,limit=limit)
        with patch("bonuspro.worker.FulfillmentReader.historical",return_value=None):
            self.assertEqual(self.cycle(),"processed")
        before=self.finances()
        self.fx.install.change(self.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),0)
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.finances(),before)

    def test_known_legacy_return_without_context_is_history_even_after_reversal(self):
        returned=str(uuid4())
        credit=self.fx.ledger.credit(self.tenant,self.fx.wallet,points=10,key=str(uuid4()),
                                    actor_id=self.fx.actor,reason="historical fixture")
        case=Returns(self.fx.ledger).request(self.tenant,credit,return_id=returned,points=10,
                                            actor_id=self.fx.actor)
        self.assertEqual(case["state"],"reversed")
        with self.fx.ledger.transaction(self.tenant) as conn:
            self.worker().signals.index_in(conn,self.tenant,self.order,[("salesreturn",returned)])
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.order_contexts WHERE order_id=%s",
                                           (self.order,)).fetchone())
        self.assertIsNone(self.auto.historical_candidate(self.tenant,self.order))
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.auto.wake_historical_candidates(self.tenant),0)
        self.assertEqual(self.finances(),before)

    def test_incomplete_rawgraph_cannot_use_historical_metadata_fallback(self):
        self.invalid_positions=True
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.entity_reads,1)
        self.assertEqual(self.finances(),before)

    def test_any_demand_or_return_reference_keeps_old_unsupported_order_under_review(self):
        demand=str(uuid4())
        demand_doc={**copy.deepcopy(self.doc),"id":demand,"meta":ref("demand",demand)["meta"],
                    "applicable":False,"customerOrder":ref("customerorder",self.order),"returns":[]}
        self.related[BASE.removeprefix("https://api.moysklad.ru")+"/entity/demand/"+demand]=demand_doc
        self.related[BASE.removeprefix("https://api.moysklad.ru")+"/entity/demand/"+demand+"/positions"]={
            "meta":{"size":1},"rows":copy.deepcopy(self.rows)}
        self.doc["demands"]=[ref("demand",demand)]
        before=self.finances()
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.entity_reads,4)  # existing two rawgraphs; no extra graph/metadata probe
        self.assertEqual(self.finances(),before)
        self.doc.pop("demands");self.doc["returns"]=[ref("salesreturn",str(uuid4()))]
        self.hint();self.entity_reads=0
        self.assertEqual(self.cycle(),"processed")
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertEqual(self.entity_reads,4)
        self.assertEqual(self.finances(),before)

    def test_validation_log_keeps_finite_reason_and_phase_without_private_details(self):
        self.doc["created"]=self.stamp(self.cutoff+timedelta(seconds=1))
        before=self.finances()
        output=io.StringIO()
        with patch("sys.stdout",output):
            self.assertEqual(self.cycle(),"processed")
        logs=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(logs,[dict(event="order_validation_failed",phase="fulfillment_read",
                                    code="currency_unsupported")])
        self.hint();private="private-token-customer-document"
        output=io.StringIO()
        error=InvalidDocument(private,reason=private)
        with patch("sys.stdout",output),patch("bonuspro.adapters.moysklad.fulfillment.normalize_order",side_effect=error):
            self.assertEqual(self.cycle(),"processed")
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
            [dict(event="order_validation_failed",phase="fulfillment_read",code="saved_order_invalid")])
        for value in (private,self.tenant,self.order,self.fx.customer,self.doc["name"]):
            self.assertNotIn(value,output.getvalue())
        self.assertEqual(self.monitor()["outcome"],"provider_error")
        self.assertTrue(all(job["last_error"]=="invalid_document" for job in self.jobs()))
        self.assertEqual(self.finances(),before)
