import unittest
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.vendor import UserContext
import test_imports as fixture

class ImportAPITests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ImportTests();self.fx.setUp();f=self.fx
        self.client=TestClient(create_app(Settings("https://testserver",f.install.config,f.ledger.dsn,f.install.lifecycle_dsn),
            store=f.install,remap_factory=lambda *a:f.client),base_url="https://testserver")
        self.addCleanup(self.client.close)
    def headers(self,role="admin"):
        return {"Authorization":"Bearer "+self.fx.install.create_session(UserContext(self.fx.tenant,self.fx.actor,role)),
                "Origin":"https://testserver"}
    def test_admin_origin_and_preview_commit_boundary(self):
        h=self.headers();p="/api/admin/imports/preview";body={"csv_text":self.fx.csv()}
        self.assertEqual(self.client.post(p,json=body).status_code,401)
        self.assertEqual(self.client.post(p,headers=self.headers("worker"),json=body).status_code,403)
        self.assertEqual(self.client.post(p,headers={**h,"Origin":"https://evil.test"},json=body).status_code,403)
        r=self.client.post(p,headers=h,json=body);self.assertEqual(r.status_code,200,r.text);b=r.json()
        self.assertEqual(self.fx.count("ledger_transactions"),0)
        path="/api/admin/imports/"+b["id"]+"/commit"
        payload={"digest":b["digest"],"reason":"Checked"}
        self.assertEqual(self.client.post(path,headers=h,json={**payload,"points":200}).status_code,422)
        r=self.client.post(path,headers=h,json=payload);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.fx.count("ledger_transactions"),1)
    def test_invalid_file_has_clear_validation_error(self):
        r=self.client.post("/api/admin/imports/preview",headers=self.headers(),json={"csv_text":"bad"})
        self.assertEqual(r.status_code,422,r.text)

if __name__=="__main__":unittest.main()
