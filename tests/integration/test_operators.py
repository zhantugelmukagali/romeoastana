import unittest
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.vendor import UserContext
from bonuspro.adapters.postgres.operators import Operators,OperatorDenied
from bonuspro.adapters.postgres.ledger import Conflict,NotFound
import test_redemptions as fixture

class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.RedemptionTests();self.fx.setUp();f=self.fx
        self.op=Operators(f.ledger);self.worker=UserContext(f.tenant,str(uuid4()),"worker","florist@example.test")
        self.token=f.install.create_session(self.worker)
    def update(self,enabled=True,version=0,key=None):
        f=self.fx
        return self.op.update(f.tenant,self.worker.user_id,enabled=enabled,expected_version=version,
            request_id=key or str(uuid4()),actor=f.actor,reason="Approved employee access")
    def test_default_deny_explicit_grant_and_revocation(self):
        with self.assertRaises(OperatorDenied):self.op.check(self.worker)
        self.assertEqual(self.op.listing(self.fx.tenant)["items"][0]["login"],"florist@example.test")
        self.update();self.op.check(self.worker);self.update(False,1)
        with self.assertRaises(OperatorDenied):self.op.check(self.worker)
    def test_other_tenant_and_unknown_employee_cannot_be_granted(self):
        with self.assertRaises(NotFound):self.op.update(str(uuid4()),self.worker.user_id,enabled=True,
            expected_version=0,request_id=str(uuid4()),actor=self.fx.actor,reason="Test")
        self.assertEqual(self.op.listing(str(uuid4()))["items"],[])
    def test_concurrent_changes_use_version_and_exact_retry(self):
        key=str(uuid4());self.update(key=key);self.update(key=key)
        with self.assertRaises(Conflict):self.update(False,key=key)
        def change(i):
            try:return self.update(bool(i),1)
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)
    def test_revoked_during_document_read_cannot_prepare(self):
        f=self.fx;self.update();self.op.check(self.worker);self.update(False,1)
        with self.assertRaises(OperatorDenied):
            f.redemptions.prepare(f.tenant,f.saved,points=1000,request_id=str(uuid4()),actor_id=self.worker.user_id,
                expected_source=f.saved.source_hash,authorize=lambda conn:self.op.check_in(conn,self.worker))
        self.assertEqual(f.ledger.snapshot(f.tenant,f.wallet)["reserved"],0)
    def test_authorized_saved_discount_then_revocation_blocks_debit(self):
        f=self.fx;self.update()
        p=f.redemptions.prepare(f.tenant,f.saved,points=1000,request_id=str(uuid4()),actor_id=self.worker.user_id,
            expected_source=f.saved.source_hash,authorize=lambda conn:self.op.check_in(conn,self.worker))
        self.update(False,1)
        with self.assertRaises(OperatorDenied):
            f.redemptions.confirm_saved(f.tenant,f.persisted(p),authorize=lambda conn:self.op.check_in(conn,self.worker))
        self.assertEqual(f.ledger.snapshot(f.tenant,f.wallet)["balance"],5000)
    def test_widget_access_does_not_grant_admin_api(self):
        f=self.fx;self.update()
        with TestClient(create_app(Settings("https://testserver",f.install.config,f.ledger.dsn,f.install.lifecycle_dsn),store=f.install),
                        base_url="https://testserver") as client:
            h={"Authorization":"Bearer "+self.token,"Origin":"https://testserver"}
            for endpoint in ("operators","rules","imports","settlements","automation"):
                self.assertEqual(client.get("/api/admin/"+endpoint,headers=h).status_code,403)
            self.assertEqual(client.put("/api/admin/operators/"+self.worker.user_id,headers=h,
                json=dict(enabled=True,expected_version=1,request_id=str(uuid4()),reason="Escalate")).status_code,403)

if __name__=="__main__":unittest.main()
