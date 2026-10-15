import importlib.util,unittest
from pathlib import Path
from uuid import uuid4
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
from start_processing import select_tenant,check_image,worker_override,ProcessingFailed

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
