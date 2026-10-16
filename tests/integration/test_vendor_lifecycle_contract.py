"""Documented Vendor lifecycle payloads, signatures, and financial invariants."""
import unittest,time
from uuid import uuid4
import jwt
import test_installations as installation_fixture
import test_activation as activation_fixture
from bonuspro.adapters.moysklad.vendor import UserContext
from bonuspro.adapters.postgres.checkout import Checkout
from bonuspro.security import AuthenticationFailed

class VendorLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.fx=installation_fixture.InstallationTests();self.fx.setUp();self.fx.install()
        self.client=self.fx.client();self.addCleanup(self.client.close)
        self.path=f"/api/moysklad/vendor/1.0/apps/{self.fx.config.app_id}/{self.fx.account}"

    def headers(self,claims=None):
        token=jwt.encode(claims or self.fx.claims(1),self.fx.config.vendor_secret,algorithm="HS256")
        return {"Authorization":"Bearer "+token}

    def row(self):
        with self.fx.repo.lifecycle() as conn:
            return dict(conn.execute("SELECT * FROM bonuspro.installations WHERE tenant_id=%s",
                                     (self.fx.account,)).fetchone())

    def test_billing_notices_without_access_keep_token_state_sessions_and_generation(self):
        session=self.fx.repo.create_session(self.fx.identity);before=self.row()
        for cause in ("TariffChanged","Autoprolongation"):
            response=self.client.put(self.path,headers=self.headers(),json=dict(
                appUid=self.fx.config.app_uid,cause=cause,subscription={"trial":False,"tariffName":"Free"}))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{"status":"SettingsRequired"})
            self.assertEqual(self.row(),before)
            self.assertEqual(self.fx.repo.authenticate(session),self.fx.identity)
            self.assertEqual(self.fx.repo.remap_token(self.fx.account),"synthetic-remap-token")

    def test_billing_cannot_supply_new_credentials_or_resurrect_an_uninstalled_account(self):
        body=dict(appUid=self.fx.config.app_uid,cause="TariffChanged",
            access=[dict(resource="https://api.moysklad.ru/api/remap/1.2",access_token="replacement-secret")])
        response=self.client.put(self.path,headers=self.headers(),json=body)
        self.assertEqual(response.status_code,422)
        self.assertNotIn("replacement-secret",response.text)
        self.fx.repo.change(self.fx.account,self.fx.claims(2),"Uninstall")
        body.pop("access")
        self.assertEqual(self.client.put(self.path,headers=self.headers(),json=body).status_code,409)
        self.assertEqual(self.row()["state"],"Uninstalled")

    def event_body(self):
        return dict(appUid=self.fx.config.app_uid,cause="PermissionsChanged",access=[
            dict(resource="https://api.moysklad.ru/api/remap/1.2/",scope=["custom"],
                 permissions={"useOwnAttributeMetadata":True,"useOwnWebhooks":True})])

    def test_permissions_event_is_signed_replay_protected_and_never_replaces_credentials(self):
        body=self.event_body();before=self.row()
        self.assertEqual(self.client.put(self.path+"/event",json=body).status_code,401)
        for prefix in ("/api/moysklad/vendor/1.0","/api/vendor/1.0"):
            path=f"{prefix}/apps/{self.fx.config.app_id}/{self.fx.account}/event"
            headers=self.headers()
            result=self.client.put(path,json=body,headers=headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.content,b"")
            self.assertEqual(self.client.put(path,json=body,headers=headers).status_code,401)
            self.assertEqual(self.row(),before)

    def test_permissions_notice_cannot_create_or_activate_an_installation(self):
        other=str(uuid4())
        path=self.path.rsplit("/",1)[0]+"/"+other+"/event"
        self.assertEqual(self.client.put(path,json=self.event_body(),headers=self.headers()).status_code,200)
        with self.fx.repo.lifecycle() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bonuspro.installations WHERE tenant_id=%s",(other,)).fetchone())
        self.fx.repo.change(self.fx.account,self.fx.claims(2),"Uninstall")
        before=self.row()
        self.assertEqual(self.client.put(self.path+"/event",json=self.event_body(),headers=self.headers()).status_code,200)
        self.assertEqual(self.row(),before)

    def test_unknown_causes_app_identity_and_wrong_resources_are_rejected(self):
        body=self.event_body()
        for patch,status in (({"cause":"Install"},422),({"appUid":"wrong.vendor"},404),
                ({"access":[dict(resource="https://evil.test")]},422)):
            response=self.client.put(self.path+"/event",headers=self.headers(),json={**body,**patch})
            self.assertEqual(response.status_code,status,response.text)
        missing=dict(appUid=self.fx.config.app_uid,cause="Install")
        self.assertEqual(self.client.put(self.path,headers=self.headers(),json=missing).status_code,422)

class ResumeContractTests(unittest.TestCase):
    def setUp(self):
        self.fx=activation_fixture.ActivationTests();self.fx.setUp()
        self.fx.activate();self.f=self.fx.fx
        self.stamp=int(time.time())-5

    def claims(self,delta=0):
        return dict(iat=self.stamp+delta,exp=int(time.time())+120,jti=str(uuid4()))

    def test_resume_restores_approved_program_without_moving_cutoff_or_reusing_old_sessions(self):
        before=self.f.install.state(self.f.tenant)
        session=self.f.install.create_session(UserContext(self.f.tenant,self.f.actor,"admin"))
        with self.f.ledger.transaction(self.f.tenant) as conn:
            cutoff=conn.execute("SELECT starts_at FROM bonuspro.automation_config").fetchone()["starts_at"]
        self.f.install.change(self.f.tenant,self.claims(),"Suspend")
        state=self.f.install.change(self.f.tenant,self.claims(1),"Resume","new-synthetic-token")
        self.assertEqual(state,"Activated")
        self.assertEqual(self.f.install.remap_token(self.f.tenant),"new-synthetic-token")
        with self.assertRaises(AuthenticationFailed):self.f.install.authenticate(session)
        with self.f.ledger.transaction(self.f.tenant) as conn:
            self.assertEqual(conn.execute("SELECT starts_at FROM bonuspro.automation_config").fetchone()["starts_at"],cutoff)
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.ledger_transactions").fetchone()["n"],0)

    def test_resume_with_changed_rules_requires_review_and_does_not_reset_configuration(self):
        rules=Checkout(self.f.ledger)
        rules.save_rules(self.f.tenant,earn_bps=600,spend_bps=3000,delivery_policy="exclude",
            expected_version=1,request_id=str(uuid4()),actor_id=self.f.actor,reason="Changed settings")
        self.f.install.change(self.f.tenant,self.claims(),"Suspend")
        self.assertEqual(self.f.install.change(self.f.tenant,self.claims(1),"Resume","new-token"),"SettingsRequired")
        self.assertEqual(rules.rules(self.f.tenant)["version"],2)

    def test_reinstall_stays_in_setup_mode_with_preserved_cutover_consent(self):
        self.f.install.change(self.f.tenant,self.claims(),"Uninstall")
        self.assertEqual(self.f.install.change(self.f.tenant,self.claims(1),"Install","new-token"),"SettingsRequired")
