import unittest
from uuid import uuid4
from bonuspro.adapters.postgres.operators import Operators
import test_widget_api as fixture

class OperatorWidgetAPITests(unittest.TestCase):
    def test_worker_can_complete_authorized_sale_but_revocation_is_immediate(self):
        f=fixture.WidgetAPITests();f.setUp();self.addCleanup(f.doCleanups)
        h=f.auth("worker");op=Operators(f.fx.ledger)
        op.update(f.fx.tenant,f.fx.actor,enabled=True,expected_version=0,request_id=str(uuid4()),actor=f.fx.actor,reason="Approved")
        quote=f.client.get(f.path,headers=h);self.assertEqual(quote.status_code,200,quote.text)
        p=f.client.post(f.path+"/prepare",headers=h,json=dict(points=1000,request_id=str(uuid4()),source_hash=quote.json()["saved_source_hash"]))
        self.assertEqual(p.status_code,200,p.text);f.fx.saved=f.fx.persisted(p.json())
        r=f.client.post(f.path+"/verify",headers=h);self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()["verified"])
        self.assertEqual(f.fx.ledger.snapshot(f.fx.tenant,f.fx.wallet)["balance"],4000)
        op.update(f.fx.tenant,f.fx.actor,enabled=False,expected_version=1,request_id=str(uuid4()),actor=f.fx.actor,reason="Revoked")
        self.assertEqual(f.client.get(f.path,headers=h).status_code,403)

if __name__=="__main__":unittest.main()
