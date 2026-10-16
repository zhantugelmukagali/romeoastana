import unittest
import test_automation as fixture

class OperationsTests(unittest.TestCase):
    def test_dashboard_reports_unverified_discount_without_customer_data(self):
        f=fixture.AutomationTests();f.setUp();f.fx.prepare()
        result=f.a.dashboard(f.tenant)
        self.assertEqual(result["unverified"][0]["points"],1000)
        self.assertEqual(str(result["unverified"][0]["order_id"]),f.fx.order)
        self.assertEqual(result["attention"]["pending_returns"],0)
        self.assertNotIn("counterparty_id",result["unverified"][0])
        self.assertIsNone(result["attention"]["oldest_job"])

if __name__=="__main__":unittest.main()
