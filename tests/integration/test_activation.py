import os,unittest
from uuid import uuid4
from unittest.mock import Mock
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.vendor import UserContext,ProviderUnavailable
from bonuspro.adapters.postgres.activation import ActivationService
from bonuspro.adapters.postgres.checkout import Checkout
from bonuspro.adapters.postgres.redemptions import Redemptions
from bonuspro.adapters.postgres.ledger import Conflict
import test_imports as fixture

class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ImportTests();self.fx.setUp();f=self.fx
        self.service=ActivationService(f.install);self.provider=Mock();self.key=str(uuid4())
        self.rules=Checkout(f.ledger)
        self.rules.save_rules(f.tenant,earn_bps=500,spend_bps=3000,delivery_policy="exclude",
            expected_version=0,request_id=str(uuid4()),actor_id=f.actor,reason="Initial")
        Redemptions(f.ledger).bind(f.tenant,str(uuid4()),f.actor)
    def activate(self):return self.service.activate(self.fx.tenant,self.key,self.fx.actor,"Approved cutover",self.provider)
    def test_activation_remote_confirmation_and_pinned_cutoff(self):
        r=self.activate();self.assertEqual(r["state"],"Activated")
        self.assertEqual(self.activate(),r);self.provider.activate.assert_called_once_with(self.fx.tenant)
        self.assertEqual(self.fx.install.state(self.fx.tenant)["state"],"Activated")
        with self.assertRaises(Conflict):self.fx.preview()
    def test_lost_remote_response_retries_same_intent_without_local_activation(self):
        self.provider.activate.side_effect=ProviderUnavailable("unknown")
        with self.assertRaises(ProviderUnavailable):self.activate()
        self.assertEqual(self.fx.install.state(self.fx.tenant)["state"],"SettingsRequired")
        self.assertEqual(self.fx.count("activation_requests"),1)
        self.provider.activate.side_effect=None;self.activate()
        self.assertEqual(self.fx.count("activation_completions"),1)
    def test_suspend_during_remote_call_cannot_be_overwritten(self):
        import time
        def suspend(_):self.fx.install.change(self.fx.tenant,
            dict(iat=int(time.time()),exp=int(time.time())+60,jti=str(uuid4())),"Suspend")
        self.provider.activate.side_effect=suspend
        with self.assertRaises(Conflict):self.activate()
        self.assertEqual(self.fx.install.state(self.fx.tenant)["state"],"Suspended")
        self.assertEqual(self.fx.count("activation_completions"),0)
    def test_changed_rules_require_new_review(self):
        def change(_):self.rules.save_rules(self.fx.tenant,earn_bps=1000,spend_bps=3000,delivery_policy="exclude",
            expected_version=1,request_id=str(uuid4()),actor_id=self.fx.actor,reason="Changed")
        self.provider.activate.side_effect=change
        with self.assertRaises(Conflict):self.activate()
        self.assertEqual(self.fx.install.state(self.fx.tenant)["state"],"SettingsRequired")
    def test_api_requires_admin_and_explicit_cutover_acknowledgements(self):
        f=self.fx
        with TestClient(create_app(Settings("https://testserver",f.install.config,f.ledger.dsn,f.install.lifecycle_dsn),
            store=f.install,vendor=self.provider),base_url="https://testserver") as client:
            token=f.install.create_session(UserContext(f.tenant,f.actor,"admin"))
            h={"Origin":"https://testserver","Authorization":"Bearer "+token}
            body=dict(request_id=self.key,reason="Reviewed",balances_reviewed=True,previous_program_disabled=True)
            self.assertEqual(client.post("/api/admin/activate",headers=h,json={**body,"balances_reviewed":False}).status_code,422)
            self.assertEqual(client.post("/api/admin/activate",headers=h,json={**body,"state":"Activated"}).status_code,422)
            r=client.post("/api/admin/activate",headers=h,json=body);self.assertEqual(r.status_code,200,r.text)

if __name__=="__main__":unittest.main()
