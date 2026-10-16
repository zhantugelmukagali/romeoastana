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
                quantity=int(x.quantity),price=int(x.price),discount=x.discount_bps/100,
                vat=int(x.vat),vatEnabled=x.vat_enabled) for x in saved.lines]
            body={"meta":{"size":len(rows)},"rows":rows}
        elif request.url.path.endswith("/customerorder/"+self.fx.order):
            body=dict(id=s.order_id,accountId=self.order_account,
                meta=ref("customerorder",s.order_id)["meta"],agent=ref("counterparty",s.counterparty_id),
                organization=ref("organization",saved.organization_id),
                rate={"currency":ref("currency",self.currency)},sum=s.total_minor,
                payedSum=s.paid_minor,shippedSum=s.shipped_minor,updated=s.revision,
                applicable=not s.cancelled,vatEnabled=saved.vat_enabled,vatIncluded=saved.vat_included,
                attributes=[{"id":self.fx.marker,"value":saved.marker}] if saved.marker else [],name=saved.name)
            if self.linked:body["demands"]=[ref("demand",self.fx.order)]
        elif request.url.path.endswith("/demand/"+self.fx.order):
            body=dict(id=self.fx.order,accountId=self.fx.tenant,meta=ref("demand",self.fx.order)["meta"],
                      applicable=True,sum=s.total_minor)
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
