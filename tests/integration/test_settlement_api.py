import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.vendor import UserContext
import test_settlements as fixture

class SettlementAPITests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.SettlementTests();self.fx.setUp()
        self.fx.process();self.fx.spend_all();self.proof=self.fx.proof(1);self.fx.process(self.proof)
        self.case=self.fx.cases()[0];sale=self.fx.sale
        config=Settings("https://testserver",sale.install.config,sale.ledger.dsn,sale.install.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=sale.install,remap_factory=lambda *a:object()),base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.path="/api/admin/settlements/"+self.case["id"]+"/resolve"
    def headers(self,role="admin"):
        sale=self.fx.sale
        token=sale.install.create_session(UserContext(sale.tenant,sale.actor,role))
        return {"Authorization":"Bearer "+token,"Origin":"https://testserver"}
    def test_authenticated_tenant_scoped_case_and_no_client_amounts(self):
        self.assertEqual(self.client.get("/api/admin/settlements").status_code,401)
        self.assertEqual(self.client.get("/api/admin/settlements",headers=self.headers("worker")).status_code,403)
        h=self.headers()
        response=self.client.get("/api/admin/settlements",headers=h)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["items"][0]["earn_delta"],-500)
        self.assertEqual(self.client.post(self.path,headers={**h,"Origin":"https://evil.test"},json={"decision":"waive","reason":"Reviewed"}).status_code,403)
        self.assertEqual(self.client.post(self.path,headers=h,json={"decision":"waive","reason":"Reviewed","points":1}).status_code,422)
    def test_saved_proof_required_and_successful_retry_is_idempotent(self):
        h=self.headers();body={"decision":"waive","reason":"Reviewed with customer"}
        with patch("bonuspro.api.app.FulfillmentReader.read",return_value=self.proof) as reader:
            first=self.client.post(self.path,headers=h,json=body)
            self.assertEqual(first.status_code,200,first.text)
            second=self.client.post(self.path,headers=h,json=body)
            self.assertEqual(second.json(),first.json());self.assertEqual(reader.call_count,1)
        self.assertEqual(self.fx.funds()["balance"],0)
    def test_changed_provider_proof_cannot_resolve_stale_case(self):
        with patch("bonuspro.api.app.FulfillmentReader.read",return_value=self.fx.proof("0.5")):
            response=self.client.post(self.path,headers=self.headers(),json={"decision":"waive","reason":"Reviewed"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.fx.cases()[0]["state"],"pending")

if __name__=="__main__":unittest.main()
