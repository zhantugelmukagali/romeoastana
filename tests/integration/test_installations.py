import os
import time
import unittest
from uuid import uuid4
from hashlib import sha256

import jwt
import psycopg
from fastapi.testclient import TestClient

from bonuspro.security import Secrets, AuthenticationFailed
from bonuspro.settings import Settings
from bonuspro.api.app import create_app
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.moysklad.vendor import UserContext


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.account,self.user=str(uuid4()),str(uuid4())
        self.config=Secrets(str(uuid4()),"test.vendor","synthetic-secret-"+"x"*32,b"k"*32)
        self.repo=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],self.config)
        self.stamp=int(time.time())-20
        self.identity=UserContext(self.account,self.user,"admin")

    def claims(self, delta=0):
        return {"iat":self.stamp+delta,"exp":int(time.time())+120,"jti":str(uuid4())}

    def install(self):
        return self.repo.change(self.account,self.claims(),"Install","synthetic-remap-token")

    def test_install_encrypts_and_does_not_report_ready_prematurely(self):
        self.assertEqual(self.install(),"SettingsRequired")
        with self.repo.lifecycle() as conn:
            row=conn.execute("SELECT token_ciphertext FROM bonuspro.installations WHERE tenant_id=%s",(self.account,)).fetchone()
        self.assertNotIn("synthetic-remap-token",row["token_ciphertext"])
        self.assertEqual(self.config.decrypt(self.account,row["token_ciphertext"]),"synthetic-remap-token")

    def test_replay_rejected_but_retry_with_new_jti_is_idempotent(self):
        claims=self.claims()
        self.repo.change(self.account,claims,"Install","synthetic-remap-token")
        session=self.repo.create_session(self.identity)
        with self.assertRaises(AuthenticationFailed):
            self.repo.change(self.account,claims,"Install","synthetic-remap-token")
        self.repo.change(self.account,self.claims(1),"Install","synthetic-remap-token")
        self.assertEqual(self.repo.authenticate(session),self.identity)

    def test_suspend_revokes_sessions_and_resume_uses_new_generation(self):
        self.install()
        session=self.repo.create_session(self.identity)
        self.repo.change(self.account,self.claims(1),"Suspend")
        with self.assertRaises(AuthenticationFailed):
            self.repo.authenticate(session)
        with self.assertRaises(AuthenticationFailed):
            self.repo.create_session(self.identity)
        self.repo.change(self.account,self.claims(2),"Resume","new-synthetic-token")
        with self.assertRaises(AuthenticationFailed):
            self.repo.authenticate(session)
        self.assertEqual(self.repo.authenticate(self.repo.create_session(self.identity)),self.identity)

    def test_uninstall_missing_installation_is_idempotent(self):
        self.assertIsNone(self.repo.change(self.account,self.claims(),"Uninstall"))
        self.assertIsNone(self.repo.change(self.account,self.claims(1),"Uninstall"))

    def test_old_install_cannot_undo_new_suspend(self):
        self.install()
        self.repo.change(self.account,self.claims(2),"Suspend")
        with self.assertRaises(Conflict):
            self.repo.change(self.account,self.claims(1),"Install","old-token")

    def test_forged_session_tenant_and_expired_session_are_rejected(self):
        self.install()
        session=self.repo.create_session(self.identity)
        with self.assertRaises(AuthenticationFailed):
            self.repo.authenticate(str(uuid4())+"."+session.split(".",1)[1])
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.sessions SET expires_at=now()-interval '1 second' WHERE tenant_id=%s",
                         (self.account,))
        with self.assertRaises(AuthenticationFailed):
            self.repo.authenticate(session)

    def test_logout_revokes_token(self):
        self.install()
        session=self.repo.create_session(self.identity)
        self.repo.logout(session)
        with self.assertRaises(AuthenticationFailed):
            self.repo.authenticate(session)

    def test_lifecycle_login_cannot_read_financial_journal(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.repo.lifecycle() as conn:
                conn.execute("SELECT * FROM bonuspro.ledger_transactions")

    def test_application_login_cannot_provision_installation(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.repo.ledger.transaction(self.account) as conn:
                conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)",(self.account,))

    def client(self):
        identity=self.identity
        class Provider:
            def exchange(self,token):
                return identity
        config=Settings("https://testserver",self.config,os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"])
        return TestClient(create_app(config,store=self.repo,vendor=Provider()),base_url="https://testserver")

    def test_http_lifecycle_signature_replay_and_validation_redaction(self):
        with self.client() as client:
            path=f"/api/moysklad/vendor/1.0/apps/{self.config.app_id}/{self.account}"
            body={"appUid":self.config.app_uid,"cause":"Install",
                  "access":[{"resource":"https://api.moysklad.ru/api/remap/1.2","access_token":"synthetic-remap-token"}]}
            self.assertEqual(client.put(path,json=body).status_code,401)
            token=jwt.encode(self.claims(),self.config.vendor_secret,algorithm="HS256")
            headers={"Authorization":"Bearer "+token}
            response=client.put(path,json=body,headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json(),{"status":"SettingsRequired"})
            self.assertEqual(client.put(path,json=body,headers=headers).status_code,401)
            response=client.post("/api/session",json={"token":{"secret":"do-not-echo-this"}})
            self.assertEqual(response.status_code,422)
            self.assertNotIn("do-not-echo-this",response.text)

    def test_http_session_origin_and_permissions(self):
        self.install()
        with self.client() as client:
            self.assertEqual(client.post("/api/session",json={"token":"t"}).status_code,403)
            response=client.post("/api/session",json={"token":"t"},headers={"Origin":"https://testserver"})
            self.assertEqual(response.status_code,200)
            token=response.json()["token"]
            auth={"Authorization":"Bearer "+token}
            self.assertEqual(client.get("/api/me",headers=auth).json()["account_id"],self.account)
            self.assertEqual(client.get("/api/installation",headers=auth).status_code,200)
            other=self.repo.create_session(UserContext(self.account,self.user,"worker"))
            self.assertEqual(client.get("/api/installation",headers={"Authorization":"Bearer "+other}).status_code,403)
            self.assertEqual(client.get("/api/me").status_code,401)
            self.assertEqual(client.get("/health/ready").status_code,200)
            self.assertEqual(client.get("/health/live",headers={"Host":"evil.example"}).status_code,400)

    def test_http_body_limit(self):
        with self.client() as client:
            self.assertEqual(client.post("/api/session",content=b"x"*65537).status_code,413)


if __name__=="__main__":
    unittest.main()
