import os
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from bonuspro.security import Secrets
from bonuspro.settings import Settings
from bonuspro.api.app import create_app
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.ledger import Ledger, VerifiedDiscount
from bonuspro.adapters.postgres.returns import Returns
from bonuspro.adapters.moysklad.vendor import UserContext
from bonuspro.domain.finance import Rules


class ReturnAPITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.user,self.wallet=(str(uuid4()) for _ in range(3))
        secrets=Secrets(str(uuid4()),"synthetic.test","synthetic-"+"x"*40,b"k"*32)
        self.repo=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],secrets)
        self.repo.change(self.tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),
                         "Install","synthetic-remap")
        self.ledger=Ledger(os.environ["TEST_APP_DSN"])
        self.ledger.create_wallet(self.tenant,self.wallet)
        credit=self.ledger.credit(self.tenant,self.wallet,points=1000,key="credit",
                                  actor_id=self.user,reason="Synthetic accrual")
        order=str(uuid4())
        rid=self.ledger.reserve(self.tenant,self.wallet,order_id=order,points=700,eligible_minor=1000000,
                                rules=Rules(),quote_hash="d"*64,key="reserve",actor_id=self.user)
        self.ledger.begin_verification(self.tenant,rid)
        self.ledger.confirm_discount(self.tenant,rid,proof=VerifiedDiscount(order,"d"*64,70000,"revision"))
        self.case=Returns(self.ledger).request(self.tenant,credit,return_id=str(uuid4()),points=1000,actor_id=self.user)
        config=Settings("https://testserver",secrets,os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"])
        self.client=TestClient(create_app(config,store=self.repo),base_url="https://testserver")
        self.addCleanup(self.client.close)

    def headers(self,role="admin"):
        token=self.repo.create_session(UserContext(self.tenant,self.user,role))
        return {"Authorization":"Bearer "+token,"Origin":"https://testserver"}

    def test_staff_and_anonymous_cannot_read_or_resolve(self):
        url="/api/admin/returns/"+self.case["id"]+"/resolve"
        self.assertEqual(self.client.get("/api/admin/returns").status_code,401)
        for role in ("worker","cashier","individual"):
            headers=self.headers(role)
            self.assertEqual(self.client.get("/api/admin/returns",headers=headers).status_code,403)
            self.assertEqual(self.client.post(url,headers=headers,json={"decision":"waive","reason":"Test"}).status_code,403)

    def test_admin_list_and_resolution_are_audited_and_idempotent(self):
        headers=self.headers()
        response=self.client.get("/api/admin/returns",headers=headers)
        self.assertEqual(response.status_code,200)
        row=response.json()["items"][0]
        self.assertEqual((row["points"],row["balance"],row["available"]),(1000,300,300))
        url="/api/admin/returns/"+self.case["id"]+"/resolve"
        body={"decision":"waive","reason":"Approved customer exception"}
        first=self.client.post(url,headers=headers,json=body)
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json(),self.client.post(url,headers=headers,json=body).json())
        self.assertEqual(self.client.get("/api/admin/returns",headers=headers).json()["items"],[])
        history=self.client.get("/api/admin/returns?state=waived",headers=headers).json()["items"][0]
        self.assertEqual(history["resolved_by"],self.user)
        self.assertEqual(history["resolution_reason"],body["reason"])

    def test_browser_cannot_override_amount_role_or_actor(self):
        headers=self.headers()
        url="/api/admin/returns/"+self.case["id"]+"/resolve"
        for extra in ({"points":1},{"role":"admin"},{"actor_id":str(uuid4())}):
            response=self.client.post(url,headers=headers,json={"decision":"waive","reason":"Test",**extra})
            self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.post(url,headers=headers,json={"decision":"waive","reason":"  "}).status_code,422)

    def test_origin_and_insufficient_balance_fail_closed(self):
        headers=self.headers()
        url="/api/admin/returns/"+self.case["id"]+"/resolve"
        body={"decision":"reverse","reason":"Test"}
        headers["Origin"]="https://other.example"
        self.assertEqual(self.client.post(url,headers=headers,json=body).status_code,403)
        headers["Origin"]="https://testserver"
        self.assertEqual(self.client.post(url,headers=headers,json=body).status_code,409)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],300)

    def test_static_admin_page_and_security_headers(self):
        page=self.client.get("/admin")
        self.assertEqual(page.status_code,200)
        self.assertIn("БонусПро",page.text)
        self.assertIn("frame-ancestors https://online.moysklad.ru",page.headers["content-security-policy"])
        self.assertEqual(page.headers["cache-control"],"no-store")
        self.assertEqual(self.client.get("/assets/app.js").status_code,200)
