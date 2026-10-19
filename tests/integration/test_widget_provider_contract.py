"""Real REMAP HTTP parser -> order normalizer -> PostgreSQL -> widget API.

The provider payload follows the official currency contract: no accountId.
No live provider writes or customer records are used.
"""
import json,unittest
from dataclasses import replace
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
import test_redemptions as fixtures
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.remap import RemapClient,BASE
from bonuspro.adapters.moysklad.vendor import UserContext

def ref(kind,identity):
    return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+identity}}

class WidgetProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RedemptionTests();self.fx.setUp()
        self.currency=str(uuid4());self.calls=[];self.linked=False
        self.currency_account=None
        self.omit_vat_included=False;self.order_overrides={};self.demand_applicable=True
        self.order_account=self.fx.tenant
        config=Settings("https://testserver",self.fx.install.config,
                        self.fx.ledger.dsn,self.fx.install.lifecycle_dsn)
        def factory(token,tenant):
            return RemapClient(token,tenant,transport=httpx.MockTransport(self.provider))
        self.client=TestClient(create_app(config,store=self.fx.install,remap_factory=factory),
                               base_url="https://testserver")
        self.addCleanup(self.client.close)
        session=self.fx.install.create_session(UserContext(self.fx.tenant,self.fx.actor,"admin"))
        self.headers={"Authorization":"Bearer "+session,"Origin":"https://testserver"}
        self.path="/api/widget/orders/"+self.fx.order

    def provider(self,request):
        self.calls.append(request)
        if request.headers.get("accept")!="application/json;charset=utf-8":
            return httpx.Response(400,json={"errors":[{"code":1062}]})
        saved=self.fx.saved;s=saved.snapshot
        if request.url.path.endswith("/currency/"+self.currency):
            body=dict(id=self.currency,meta=ref("currency",self.currency)["meta"],
                isoCode="KZT",code="398",default=True,rate=1,multiplicity=1,
                indirect=False,archived=False,system=True)
            if self.currency_account is not None:body["accountId"]=self.currency_account
        elif request.url.path.endswith("/positions"):
            rows=[dict(id=x.id,accountId=self.fx.tenant,assortment=ref(x.kind,x.assortment),
                quantity=float(x.quantity),price=float(x.price),discount=x.discount_bps/100,
                vat=float(x.vat),vatEnabled=x.vat_enabled) for x in saved.lines]
            body={"meta":{"size":len(rows)},"rows":rows}
        elif request.url.path.endswith("/customerorder/"+self.fx.order):
            body=dict(id=s.order_id,accountId=self.order_account,
                meta=ref("customerorder",s.order_id)["meta"],agent=ref("counterparty",s.counterparty_id),
                organization=ref("organization",saved.organization_id),
                rate={"currency":ref("currency",self.currency)},sum=s.total_minor,
                payedSum=s.paid_minor,shippedSum=s.shipped_minor,updated=s.revision,
                applicable=not s.cancelled,vatEnabled=saved.vat_enabled,vatIncluded=saved.vat_included,
                attributes=[{"id":self.fx.marker,"value":saved.marker}] if saved.marker else [],name=saved.name)
            if self.omit_vat_included:body.pop("vatIncluded")
            body.update(self.order_overrides)
            if self.linked:body["demands"]=[ref("demand",self.fx.order)]
        elif request.url.path.endswith("/demand/"+self.fx.order):
            body=dict(id=self.fx.order,accountId=self.fx.tenant,meta=ref("demand",self.fx.order)["meta"],
                      applicable=self.demand_applicable,sum=s.total_minor)
        else:
            return httpx.Response(404,json={"errors":[{"code":1021}]})
        return httpx.Response(200,json=body)

    def test_real_transport_preview_prepare_verify_and_fulfilled_repeat(self):
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        self.assertEqual(quote.json()["eligible_minor"],1000000)
        self.assertEqual(quote.json()["total_minor"],1200000)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,json=dict(
            points=1000,request_id=str(uuid4()),source_hash=quote.json()["saved_source_hash"]))
        self.assertEqual(prepared.status_code,200,prepared.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],1000)
        before=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertFalse(before.json()["verified"])
        self.fx.saved=self.fx.persisted(prepared.json())
        confirmed=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        self.assertTrue(confirmed.json()["verified"])
        transaction=confirmed.json()["transaction_id"]
        self.fx.saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,
            paid_minor=1100000,shipped_minor=1100000))
        self.linked=True
        repeated=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(repeated.status_code,200,repeated.text)
        self.assertTrue(repeated.json()["verified"])
        self.assertEqual(repeated.json()["transaction_id"],transaction)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=4000,reserved=0,available=4000))
        self.assertTrue(all(request.method=="GET" for request in self.calls))

    def test_foreign_currency_or_order_is_rejected_without_reserving(self):
        for field in ("currency_account","order_account"):
            original=getattr(self,field)
            setattr(self,field,str(uuid4()))
            response=self.client.get(self.path,headers=self.headers)
            self.assertEqual(response.status_code,422,response.text)
            setattr(self,field,original)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)

    def request_body(self,quote,points=1000):
        return dict(points=points,request_id=str(uuid4()),source_hash=quote["saved_source_hash"])

    def test_disabled_vat_omission_real_transport_prepares_and_confirms_saved_discount(self):
        self.omit_vat_included=True
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,json=self.request_body(quote.json()))
        self.assertEqual(prepared.status_code,200,prepared.text)
        self.fx.saved=self.fx.persisted(prepared.json())
        result=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=4000,reserved=0,available=4000))

    def test_explicit_false_disabled_vat_remains_compatible_with_immutable_plan(self):
        self.fx.saved=replace(self.fx.saved,vat_included=False)
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,json=self.request_body(quote.json()))
        self.assertEqual(prepared.status_code,200,prepared.text)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            row=conn.execute("SELECT plan FROM bonuspro.redemption_attempts WHERE id=%s",(prepared.json()["id"],)).fetchone()
            self.assertFalse(row["plan"]["before"]["vat_included"])
            self.assertFalse(row["plan"]["after"]["vat_included"])
        self.fx.saved=self.fx.persisted(prepared.json())
        result=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()["verified"])

    def test_paid_shipped_and_unposted_orders_open_read_only_without_authorizing_spend(self):
        original=self.fx.saved
        for patch,reason in ((dict(paid_minor=1),"order_fulfilled"),
                             (dict(shipped_minor=1),"order_fulfilled"),
                             (dict(cancelled=True),"order_not_posted")):
            with self.subTest(patch=patch):
                self.fx.saved=replace(original,snapshot=replace(original.snapshot,**patch))
                quote=self.client.get(self.path,headers=self.headers)
                self.assertEqual(quote.status_code,200,quote.text)
                self.assertEqual(quote.json()["quote"]["balance"],5000)
                self.assertEqual(quote.json()["quote"]["max_spend_points"],0)
                self.assertEqual(quote.json()["quote"]["blocked_reason"],reason)
                rejected=self.client.post(self.path+"/prepare",headers=self.headers,
                    json=self.request_body(quote.json(),1))
                self.assertEqual(rejected.status_code,409,rejected.text)
                self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)

    def test_draft_linked_document_is_a_blocked_view_not_an_initial_load_error(self):
        self.linked=True;self.demand_applicable=False
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        self.assertEqual(quote.json()["quote"]["blocked_reason"],"linked_documents_present")
        self.assertEqual(quote.json()["quote"]["max_spend_points"],0)
        rejected=self.client.post(self.path+"/prepare",headers=self.headers,
                                 json=self.request_body(quote.json(),1))
        self.assertEqual(rejected.status_code,422,rejected.text)
        self.assertEqual(rejected.json()["reason"],"linked_documents_present")
        self.assertEqual(rejected.json()["stage"],"prepare")
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)

    def test_invalid_tax_modes_stay_rejected_with_safe_specific_reason(self):
        cases=((dict(vatEnabled=True,vatIncluded=None),"vat_mode_missing"),
               (dict(vatEnabled=False,vatIncluded=None),"vat_mode_missing"),
               (dict(vatEnabled=False,vatIncluded="private-document-secret"),"vat_mode_missing"),
               (dict(vatEnabled=True,vatIncluded=False),"vat_mode_unsupported"))
        for fields,reason in cases:
            with self.subTest(fields=fields):
                self.order_overrides=fields
                result=self.client.get(self.path,headers=self.headers)
                self.assertEqual(result.status_code,422,result.text)
                self.assertEqual(result.json()["reason"],reason)
                self.assertEqual(result.json()["stage"],"load")
                self.assertRegex(result.json()["trace_id"],"^[a-f0-9]{12}$")
                self.assertNotIn("private-document-secret",result.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)

    def test_real_json_fractional_quantity_and_existing_discount_round_trip(self):
        from decimal import Decimal
        line=replace(self.fx.saved.lines[0],price=Decimal(10000),quantity=Decimal("1.25"),discount_bps=1000)
        self.fx.saved=replace(self.fx.saved,lines=(line,),snapshot=replace(self.fx.saved.snapshot,
                             total_minor=11250,eligible_minor=11250))
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        self.assertEqual(quote.json()["eligible_minor"],11250)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,json=self.request_body(quote.json(),1))
        self.assertEqual(prepared.status_code,200,prepared.text)
        self.assertEqual(prepared.json()["update_state"]["positions"],[{"id":line.id,"discount":10.8}])
        self.fx.saved=replace(self.fx.saved,lines=(replace(line,discount_bps=1080),),
            marker="bonuspro:v1:"+prepared.json()["id"],snapshot=replace(self.fx.saved.snapshot,
                revision="saved-fractional",total_minor=11150,eligible_minor=11150))
        verified=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(verified.status_code,200,verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],4999)

    def test_unrepresentable_amount_has_no_reservation_and_identifies_prepare_failure(self):
        from decimal import Decimal
        saved=self.fx.saved
        self.fx.saved=replace(saved,lines=(replace(saved.lines[0],price=Decimal(3000000)),saved.lines[1]),
            snapshot=replace(saved.snapshot,total_minor=3200000,eligible_minor=3000000))
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        rejected=self.client.post(self.path+"/prepare",headers=self.headers,json=self.request_body(quote.json(),1))
        self.assertEqual(rejected.status_code,422,rejected.text)
        self.assertEqual(rejected.json()["reason"],"discount_not_representable")
        self.assertEqual(rejected.json()["stage"],"prepare")
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=5000,reserved=0,available=5000))
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.redemption_attempts").fetchone()["n"],0)

    def test_missing_customer_empty_positions_and_totals_are_distinct_load_failures(self):
        original=self.fx.saved
        for case,reason in (("customer","customer_missing"),("positions","positions_missing"),("sum","totals_mismatch")):
            self.order_overrides={}
            self.fx.saved=original
            if case=="customer":self.order_overrides={"agent":None}
            elif case=="positions":
                self.fx.saved=replace(original,lines=(),snapshot=replace(original.snapshot,total_minor=0,eligible_minor=0))
            else:self.fx.saved=replace(original,snapshot=replace(original.snapshot,total_minor=1200001))
            response=self.client.get(self.path,headers=self.headers)
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(response.json()["reason"],reason)
            self.assertEqual(response.json()["stage"],"load")

    def test_lost_prepare_recovery_uses_fresh_saved_proof_and_original_reservation(self):
        quote=self.client.get(self.path,headers=self.headers)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,
                                 json=self.request_body(quote.json()))
        self.assertEqual(prepared.status_code,200,prepared.text)
        attempt=prepared.json()
        # Simulate losing the prepare response, then explicitly saving the
        # unchanged host order before asking to continue its original attempt.
        self.fx.saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,
                                                           revision="saved-for-recovery"))
        checked=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(checked.status_code,200,checked.text)
        self.assertFalse(checked.json()["verified"])
        source=checked.json()["resume_source_hash"]
        body=dict(attempt_id=attempt["id"],source_hash=source)
        stale=self.client.post(self.path+"/resume",headers=self.headers,
                              json={**body,"source_hash":quote.json()["saved_source_hash"]})
        self.assertEqual(stale.status_code,409,stale.text)
        recovered=self.client.post(self.path+"/resume",headers=self.headers,json=body)
        self.assertEqual(recovered.status_code,200,recovered.text)
        self.assertEqual(recovered.json()["id"],attempt["id"])
        self.assertEqual(recovered.json()["update_state"],attempt["update_state"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=5000,reserved=1000,available=4000))
        self.fx.saved=self.fx.persisted(attempt)
        done=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(done.status_code,200,done.text)
        self.assertTrue(done.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=4000,reserved=0,available=4000))
        self.assertEqual(self.client.post(self.path+"/resume",headers=self.headers,json=body).status_code,409)

    def test_resume_refuses_concurrent_saved_edit_and_linked_draft(self):
        quote=self.client.get(self.path,headers=self.headers)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,
                                 json=self.request_body(quote.json()))
        self.assertEqual(prepared.status_code,200,prepared.text)
        checked=self.client.post(self.path+"/verify",headers=self.headers)
        body=dict(attempt_id=prepared.json()["id"],source_hash=checked.json()["resume_source_hash"])
        original=self.fx.saved
        self.fx.saved=replace(original,snapshot=replace(original.snapshot,revision="edited-after-proof"))
        rejected=self.client.post(self.path+"/resume",headers=self.headers,json=body)
        self.assertEqual(rejected.status_code,409,rejected.text)
        self.fx.saved=original;self.linked=True;self.demand_applicable=False
        rejected=self.client.post(self.path+"/resume",headers=self.headers,json=body)
        self.assertEqual(rejected.status_code,422,rejected.text)
        self.assertEqual(rejected.json()["reason"],"linked_documents_present")
        self.assertEqual(rejected.json()["stage"],"resume")
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),
                         dict(balance=5000,reserved=1000,available=4000))

    def test_resume_requires_operator_origin_and_exact_request_contract(self):
        quote=self.client.get(self.path,headers=self.headers)
        prepared=self.client.post(self.path+"/prepare",headers=self.headers,
                                 json=self.request_body(quote.json()))
        checked=self.client.post(self.path+"/verify",headers=self.headers)
        body=dict(attempt_id=prepared.json()["id"],source_hash=checked.json()["resume_source_hash"])
        self.assertEqual(self.client.post(self.path+"/resume",headers={"Origin":"https://testserver"},json=body).status_code,401)
        self.assertEqual(self.client.post(self.path+"/resume",headers={**self.headers,"Origin":"https://evil.test"},
                                         json=body).status_code,403)
        for patch in ({"points":1},{"wallet_id":str(uuid4())},{"source_hash":"forged"},{"attempt_id":str(uuid4())}):
            rejected=self.client.post(self.path+"/resume",headers=self.headers,json={**body,**patch})
            self.assertEqual(rejected.status_code,409 if "attempt_id" in patch else 422,rejected.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],1000)

    def test_unconfigured_shop_reports_setup_without_provider_reads_or_implicit_wallet(self):
        import time
        tenant,actor=str(uuid4()),str(uuid4())
        self.fx.install.change(tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),
                               "Install","synthetic")
        session=self.fx.install.create_session(UserContext(tenant,actor,"admin"))
        headers={"Authorization":"Bearer "+session,"Origin":"https://testserver"}
        before=len(self.calls)
        response=self.client.get("/api/widget/orders/"+str(uuid4()),headers=headers)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json(),{"error":"program_not_configured"})
        self.assertEqual(len(self.calls),before)
        with self.fx.ledger.transaction(tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"],0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.reservations").fetchone()["n"],0)
