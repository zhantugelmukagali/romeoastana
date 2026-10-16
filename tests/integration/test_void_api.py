import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.vendor import UserContext
import test_voids as fixture

class VoidAPITests(unittest.TestCase):
    def test_admin_server_proof_and_no_browser_release_evidence(self):
        fx=fixture.VoidTests();fx.setUp();fx.age();s=fx.sale
        with TestClient(create_app(Settings("https://testserver",s.install.config,s.ledger.dsn,s.install.lifecycle_dsn),
            store=s.install,remap_factory=lambda *a:object()),base_url="https://testserver") as client:
            def headers(role):
                return {"Origin":"https://testserver","Authorization":"Bearer "+s.install.create_session(UserContext(s.tenant,s.actor,role))}
            path="/api/admin/redemptions/"+fx.p["id"]+"/release";body={"reason":"Cancelled and checked"}
            self.assertEqual(client.post(path,headers=headers("worker"),json=body).status_code,403)
            self.assertEqual(client.post(path,headers=headers("admin"),json={**body,"cancel_safe":True}).status_code,422)
            with patch("bonuspro.api.app.FulfillmentReader.read",return_value=fx.proof):
                r=client.post(path,headers=headers("admin"),json=body)
                self.assertEqual(r.status_code,200,r.text)
                self.assertEqual(r.json(),{"state":"released"})
            self.assertEqual(fx.fx.balance()["reserved"],0)

if __name__=="__main__":unittest.main()
