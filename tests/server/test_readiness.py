import unittest
from datetime import datetime,timezone,timedelta
from bonuspro.adapters.postgres.readiness import evaluate

class ReadinessTests(unittest.TestCase):
    def row(self,**changes):
        now=datetime.now(timezone.utc)
        return dict(state="Activated",has_rules=True,has_widget=True,has_cutoff=True,
                    heartbeat_at=now-timedelta(seconds=30),scan_until=now-timedelta(seconds=30),
                    last_error=None,checked_at=now,**changes)
    def test_ready_requires_every_real_dependency(self):
        row=self.row();self.assertTrue(evaluate(row)["ready"])
        for field in ("has_rules","has_widget","has_cutoff"):
            changed={**row,field:False};self.assertFalse(evaluate(changed)["ready"])
        self.assertFalse(evaluate({**row,"state":"Suspended"})["ready"])
    def test_absent_stale_future_and_failed_cycles_never_claim_processing_ready(self):
        row=self.row()
        for heartbeat in (None,row["checked_at"]-timedelta(minutes=10),row["checked_at"]+timedelta(seconds=1)):
            self.assertFalse(evaluate({**row,"heartbeat_at":heartbeat})["ready"])
        result=evaluate({**row,"last_error":"private provider error"})
        self.assertFalse(result["ready"]);self.assertNotIn("private",str(result))

    def test_discovery_lag_and_missing_cursor_cannot_claim_ready(self):
        row=self.row()
        for cursor in (None,row["checked_at"]-timedelta(seconds=601),row["checked_at"]+timedelta(seconds=1)):
            report=evaluate({**row,"scan_until":cursor})
            self.assertFalse(report["ready"])
            self.assertEqual(report["processing_status"],"discovery_delayed")
        self.assertTrue(evaluate({**row,"scan_until":row["checked_at"]-timedelta(minutes=10)})["ready"])

    def test_current_failures_cannot_be_hidden_by_a_fresh_worker_heartbeat(self):
        row=self.row()
        for field,status in (("provider_errors","provider_documents"),
                             ("documents_to_review","documents_review"),
                             ("unresolved_issues","documents_review"),
                             ("stale_redemptions","unverified_redemptions"),
                             ("stale_personal_discounts","unverified_redemptions"),
                             ("delayed_jobs","queue_delayed")):
            report=evaluate({**row,field:1})
            self.assertFalse(report["ready"])
            self.assertEqual(report["processing_status"],status)
            self.assertEqual(report["attention"][field],1)
        self.assertNotIn("last_error",evaluate({**row,"last_error":"private provider body"}))

    def test_large_attention_counts_are_bounded(self):
        row=self.row()
        result=evaluate({**row,"provider_errors":999999999})
        self.assertEqual(result["attention"]["provider_errors"],1001)
        self.assertTrue(result["attention_counts_limited"])


    def test_failed_order_checklist_gives_a_specific_next_action_without_guessing_currency(self):
        for count,shown in ((1,"1"),(1001,"более 1000")):
            with self.subTest(count=count):
                report=evaluate({**self.row(),"provider_errors":count})
                self.assertFalse(report["ready"])
                check=next(item for item in report["checks"] if item["id"]=="processing")
                self.assertEqual(check["label"],"Не удалось проверить заказы: "+shown+
                    ". Откройте заказ и проверьте виджет.")
                self.assertEqual(check["target"],"processing-settings")
                self.assertFalse(check["complete"])
                self.assertNotIn("RUB",check["label"])
