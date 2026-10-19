import importlib.util,unittest
from pathlib import Path
from uuid import uuid4
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
from start_processing import select_tenant,check_image,worker_override,ProcessingFailed,processing_confirmed,wait_for_report

class ProcessingStartTests(unittest.TestCase):
    def test_only_enabled_selected_accounts_are_started(self):
        a,b=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=a,state="Activated"),dict(tenant_id=b,state="Suspended")]
        self.assertEqual(select_tenant(rows),a)
        with self.assertRaises(ProcessingFailed):select_tenant(rows,b)
        with self.assertRaises(ProcessingFailed):select_tenant([])
        rows.append(dict(tenant_id=b,state="SettingsRequired"))
        with self.assertRaises(ProcessingFailed):select_tenant(rows)
        self.assertEqual(select_tenant(rows,b),b)
    def test_worker_cannot_run_a_different_or_mutable_image(self):
        config={"services":{"worker":{"image":"bonuspro:abcdef012345"}}}
        check_image(config,"bonuspro:abcdef012345")
        for image in ("bonuspro:latest","bonuspro:111111111111"):
            with self.assertRaises(ProcessingFailed):check_image(config,image)
        with self.assertRaises(ProcessingFailed):check_image({},"bonuspro:abcdef012345")
    def test_override_does_not_contain_keys_or_activate_an_account(self):
        tenant=str(uuid4());text=worker_override(tenant)
        self.assertIn(tenant,text);self.assertIn("--health",text)
        for forbidden in ("SECRET","ENCRYPTION","PASSWORD","activate","migrate"):
            self.assertNotIn(forbidden,text)

    def test_only_a_new_successful_cycle_confirms_processing_startup(self):
        baseline="2026-10-19T10:00:00+00:00"
        report=dict(ready=True,last_processing_at="2026-10-19T10:00:01+00:00")
        self.assertTrue(processing_confirmed(report,baseline))
        for heartbeat in (None,"2026-10-19T09:59:59+00:00",baseline,"2026-10-19T10:00:01"):
            self.assertFalse(processing_confirmed({**report,"last_processing_at":heartbeat},baseline))
        self.assertFalse(processing_confirmed({**report,"ready":False},baseline))

    def test_startup_wait_is_bounded_and_does_not_activate_an_inactive_program(self):
        baseline="2026-10-19T10:00:00+00:00"
        before=dict(ready=True,last_processing_at=baseline,checks=[dict(id="activation",complete=True)])
        after={**before,"last_processing_at":"2026-10-19T10:00:01+00:00"}
        calls=[];reports=[before,after]
        def fetch():
            calls.append(True)
            return reports.pop(0)
        self.assertEqual(wait_for_report(fetch,baseline,clock=lambda:0,pause=lambda _:None),after)
        self.assertEqual(len(calls),2)
        moments=[0,61]
        self.assertEqual(wait_for_report(lambda:before,baseline,clock=lambda:moments.pop(0),pause=lambda _:self.fail("expired startup must not wait")),before)
        inactive={**before,"ready":False,"checks":[dict(id="activation",complete=False)]}
        self.assertEqual(wait_for_report(lambda:inactive,baseline,pause=lambda _:self.fail("startup must not activate a program")),inactive)
