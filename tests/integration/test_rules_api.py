import os
import time
from uuid import uuid4
import unittest
from fastapi.testclient import TestClient
from bonuspro.security import Secrets
from bonuspro.settings import Settings
from bonuspro.api.app import create_app
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.moysklad.vendor import UserContext


class RulesAPITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.user=(str(uuid4()) for _ in range(2))
        secret=Secrets(str(uuid4()),"synthetic.test","synthetic-"+"x"*40,b"k"*32)
        self.repo=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],secret)
        self.repo.change(self.tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),
                         "Install","synthetic-remap")
        config=Settings("https://testserver",secret,os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"])
        self.client=TestClient(create_app(config,store=self.repo),base_url="https://testserver")
        self.addCleanup(self.client.close)

    def headers(self,role="admin"):
        return {"Authorization":"Bearer "+self.repo.create_session(UserContext(self.tenant,self.user,role)),
                "Origin":"https://testserver"}

    def body(self):
        return dict(earn_bps=500,spend_bps=3000,delivery_policy="exclude",expected_version=0,
                    request_id=str(uuid4()),reason="Approved initial rules")

    def test_admin_save_and_retry_do_not_activate_installation(self):
        headers=self.headers();body=self.body()
        self.assertEqual(self.client.get("/api/admin/rules",headers=headers).json(),{"current":None})
        first=self.client.put("/api/admin/rules",headers=headers,json=body)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),self.client.put("/api/admin/rules",headers=headers,json=body).json())
        self.assertEqual(first.json()["current"]["actor_id"],self.user)
        self.assertEqual(self.client.get("/api/installation",headers=headers).json()["state"],"SettingsRequired")
        self.assertEqual(len(self.client.get("/api/admin/rules/history",headers=headers).json()["items"]),1)

    def test_stale_version_does_not_overwrite(self):
        headers=self.headers()
        self.client.put("/api/admin/rules",headers=headers,json=self.body())
        response=self.client.put("/api/admin/rules",headers=headers,json={**self.body(),"earn_bps":1000})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.get("/api/admin/rules",headers=headers).json()["current"]["earn_bps"],500)

    def test_anonymous_and_non_admin_access_denied(self):
        self.assertEqual(self.client.get("/api/admin/rules").status_code,401)
        for role in ("cashier","worker","individual"):
            headers=self.headers(role)
            for path in ("/api/admin/rules","/api/admin/rules/history"):
                self.assertEqual(self.client.get(path,headers=headers).status_code,403)
            self.assertEqual(self.client.put("/api/admin/rules",headers=headers,json=self.body()).status_code,403)

    def test_browser_cannot_set_actor_tenant_or_unsupported_rules(self):
        headers=self.headers()
        for patch in (dict(actor_id=str(uuid4())),dict(tenant_id=str(uuid4())),dict(earn_bps=True),
                      dict(earn_bps=1.5),dict(spend_bps="3000"),dict(delivery_policy="include"),
                      dict(reason="  "),dict(expires=True),dict(minor_per_point=1)):
            response=self.client.put("/api/admin/rules",headers=headers,json={**self.body(),**patch})
            self.assertEqual(response.status_code,422,patch)
        self.assertIsNone(self.client.get("/api/admin/rules",headers=headers).json()["current"])

    def test_wrong_origin_and_revoked_session_cannot_save(self):
        headers=self.headers()
        self.assertEqual(self.client.put("/api/admin/rules",headers={**headers,"Origin":"https://evil.test"},json=self.body()).status_code,403)
        self.repo.change(self.tenant,dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        self.assertEqual(self.client.put("/api/admin/rules",headers=headers,json=self.body()).status_code,401)

    def test_history_cursor_validation_and_settings_page(self):
        headers=self.headers()
        for cursor in ("-1","0","2147483648","garbage"):
            self.assertEqual(self.client.get("/api/admin/rules/history?before="+cursor,headers=headers).status_code,422)
        page=self.client.get("/settings")
        self.assertEqual(page.status_code,200)
        self.assertIn("Правила программы",page.text)
        self.assertIn("no-store",page.headers["cache-control"])

if __name__=="__main__":unittest.main()
