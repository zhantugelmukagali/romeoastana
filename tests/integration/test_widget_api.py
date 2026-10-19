import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
from fastapi.testclient import TestClient
import test_redemptions as redemption_fixture
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.remap import OrderEvidence, BASE
from bonuspro.adapters.moysklad.vendor import UserContext

def ref(kind,identity):return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+identity}}

class WidgetAPITests(unittest.TestCase):
    def setUp(self):
        self.fx=redemption_fixture.RedemptionTests();self.fx.setUp()
        self.reads=[];self.currency=str(uuid4())
        outer=self
        class Provider:
            def __init__(self,token,tenant):
                assert token=="synthetic"
                self.tenant=tenant
            def marker_attribute(self,create=False):return outer.fx.marker
            def entity(self,kind,identity):return {"isoCode":"KZT"}
            def order_evidence(self,identity):
                outer.reads.append(identity)
                saved=outer.fx.saved;s=saved.snapshot
                if identity!=s.order_id:
                    from bonuspro.adapters.moysklad.remap import EntityMissing
                    raise EntityMissing("not found")
                attrs=[{"id":outer.fx.marker,"value":saved.marker}] if saved.marker else []
                order=dict(id=s.order_id,agent=ref("counterparty",s.counterparty_id),
                    organization=ref("organization",saved.organization_id),rate={"currency":ref("currency",outer.currency)},
                    vatEnabled=saved.vat_enabled,vatIncluded=saved.vat_included,sum=s.total_minor,
                    payedSum=s.paid_minor,shippedSum=s.shipped_minor,updated=s.revision,applicable=not s.cancelled,
                    attributes=attrs,name=saved.name)
                lines=tuple(dict(id=x.id,assortment=ref(x.kind,x.assortment),price=x.price,
                    quantity=x.quantity,discount=Decimal(x.discount_bps)/100,vat=x.vat,vatEnabled=x.vat_enabled)
                    for x in saved.lines)
                return OrderEvidence(order,lines,(),())
        config=Settings("https://testserver",self.fx.install.config,self.fx.ledger.dsn,self.fx.install.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=self.fx.install,remap_factory=Provider),base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.path="/api/widget/orders/"+self.fx.order
        self.headers=self.auth()
    def auth(self,role="admin"):
        token=self.fx.install.create_session(UserContext(self.fx.tenant,self.fx.actor,role))
        return {"Authorization":"Bearer "+token,"Origin":"https://testserver"}
    def body(self):
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200,quote.text)
        return dict(points=1000,source_hash=quote.json()["saved_source_hash"],request_id=str(uuid4()))
    def test_full_http_flow_rereads_provider_and_confirms_only_saved_discount(self):
        response=self.client.post(self.path+"/prepare",headers=self.headers,json=self.body())
        self.assertEqual(response.status_code,200,response.text)
        prepared=response.json()
        before=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(before.status_code,200,before.text);self.assertFalse(before.json()["verified"])
        self.fx.saved=self.fx.persisted(prepared)
        after=self.client.post(self.path+"/verify",headers=self.headers)
        self.assertEqual(after.status_code,200,after.text);self.assertTrue(after.json()["verified"])
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["balance"],4000)
        self.assertGreaterEqual(len(self.reads),4)
    def test_browser_cannot_supply_financial_proof_or_tenant(self):
        body=self.body()
        for patch in ({"tenant_id":str(uuid4())},{"discount_minor":100000},{"wallet_id":str(uuid4())},
                      {"points":True},{"points":"1000"},{"points":1.1},{"points":0},{"source_hash":"forged"}):
            result=self.client.post(self.path+"/prepare",headers=self.headers,json={**body,**patch})
            self.assertEqual(result.status_code,422,(patch,result.text))
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)
    def test_role_origin_and_other_order_guards(self):
        body=self.body()
        self.assertEqual(self.client.get(self.path).status_code,401)
        for role in ("cashier","worker","individual"):
            headers=self.auth(role)
            self.assertEqual(self.client.get(self.path,headers=headers).status_code,403)
            self.assertEqual(self.client.post(self.path+"/prepare",headers=headers,json=body).status_code,403)
        self.assertEqual(self.client.post(self.path+"/prepare",headers={**self.headers,"Origin":"https://evil.test"},json=body).status_code,403)
        self.assertEqual(self.client.get("/api/widget/orders/"+str(uuid4()),headers=self.headers).status_code,404)
    def test_preview_does_not_debit_or_activate_and_widget_has_security_headers(self):
        quote=self.client.get(self.path,headers=self.headers)
        self.assertEqual(quote.status_code,200)
        self.assertNotIn("synthetic",quote.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)
        page=self.client.get("/widget")
        self.assertEqual(page.status_code,200)
        self.assertIn("frame-ancestors https://online.moysklad.ru",page.headers["content-security-policy"])
        self.assertEqual(page.headers["cache-control"],"no-store")
    def test_prepare_refuses_changed_saved_document(self):
        body=self.body()
        self.fx.saved=replace(self.fx.saved,snapshot=replace(self.fx.saved.snapshot,revision="changed"))
        self.assertEqual(self.client.post(self.path+"/prepare",headers=self.headers,json=body).status_code,409)
    def test_setup_admin_only_and_credential_revocation(self):
        self.assertEqual(self.client.post("/api/admin/widget/setup",headers=self.auth("worker")).status_code,403)
        self.assertEqual(self.client.post("/api/admin/widget/setup",headers=self.headers).status_code,200)
        self.fx.install.logout(self.headers["Authorization"].split(" ",1)[1])
        self.assertEqual(self.client.post(self.path+"/verify",headers=self.headers).status_code,401)

    def test_document_failure_logs_and_response_only_allowlisted_codes(self):
        from unittest.mock import patch
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        failure=InvalidDocument("private-token-and-customer-document",reason="private-reason")
        with self.assertLogs("bonuspro.widget",level="WARNING") as logs:
            with patch("bonuspro.api.app.read_order",side_effect=failure):
                response=self.client.get(self.path,headers=self.headers)
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()["reason"],"saved_order_invalid")
        self.assertEqual(response.json()["stage"],"load")
        self.assertRegex(response.json()["trace_id"],"^[a-f0-9]{12}$")
        public=response.text+" ".join(logs.output)
        self.assertNotIn("private-token",public)
        self.assertNotIn("private-reason",public)
        self.assertNotIn(self.fx.customer,public)
        self.assertIn("trace=BP-"+response.json()["trace_id"],public)

    def test_invalid_widget_request_has_redacted_trace_and_no_financial_effect(self):
        body=self.body()
        response=self.client.post(self.path+"/prepare",headers=self.headers,
            json={**body,"points":"private-token-and-document"})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()["reason"],"invalid_request")
        self.assertEqual(response.json()["stage"],"prepare")
        self.assertNotIn("private-token",response.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)["reserved"],0)

if __name__=="__main__":unittest.main()
