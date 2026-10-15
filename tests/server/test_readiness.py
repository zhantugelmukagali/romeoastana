import unittest
from datetime import datetime,timezone,timedelta
from bonuspro.adapters.postgres.readiness import evaluate

class ReadinessTests(unittest.TestCase):
    def row(self,**changes):
        now=datetime.now(timezone.utc)
        return dict(state="Activated",has_rules=True,has_widget=True,has_cutoff=True,
                    heartbeat_at=now-timedelta(seconds=30),last_error=None,checked_at=now,**changes)
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
