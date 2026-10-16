import unittest
from uuid import uuid4
import test_portal_api as fixture
from bonuspro.adapters.postgres.redemptions import Redemptions

class ReadinessAPITests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PortalAPITests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_configuration_is_read_from_database_without_financial_mutation(self):
        path="/api/admin/readiness";f=self.f
        before=f.fx.ledger.snapshot(f.fx.tenant,f.fx.wallet)
        r=f.client.get(path,headers=f.headers);self.assertEqual(r.status_code,200,r.text)
        report=r.json();self.assertFalse(report["ready"])
        self.assertFalse(any(x["complete"] for x in report["checks"]))
        saved=f.client.put("/api/admin/rules",headers=f.headers,json=dict(earn_bps=500,spend_bps=3000,
            expected_version=0,request_id=str(uuid4()),reason="Approved operating rules"))
        self.assertEqual(saved.status_code,200,saved.text)
        Redemptions(f.fx.ledger).bind(f.fx.tenant,str(uuid4()),f.fx.user)
        report=f.client.get(path,headers=f.headers).json()
        self.assertEqual([x["complete"] for x in report["checks"]],[True,True,False,False])
        self.assertIsNone(report["last_processing_at"])
        self.assertEqual(f.fx.ledger.snapshot(f.fx.tenant,f.fx.wallet),before)
        self.assertNotIn("synthetic-remap",str(report))
    def test_anonymous_staff_and_mutations_are_rejected(self):
        path="/api/admin/readiness";f=self.f
        self.assertEqual(f.client.get(path).status_code,401)
        self.assertEqual(f.client.get(path,headers=f.fx.headers("worker")).status_code,403)
        self.assertEqual(f.client.post(path,headers=f.headers,json={"ready":True}).status_code,405)
