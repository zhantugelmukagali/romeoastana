"""Prevent accidental shipment of the former browser-only demo."""
import re,unittest
from pathlib import Path

class ReleaseSurfaceTests(unittest.TestCase):
    def test_shipped_interface_has_no_demo_transport_or_fake_financial_data(self):
        root=Path(__file__).resolve().parents[2]/"web"
        forbidden=re.compile(r"VITE_BROWSER_DEMO|browserSeed|browserDemo|synthetic-session|synthetic-remap|DEMO_MODE|demo\\.db|mockServiceWorker",re.I)
        for path in root.rglob("*"):
            if path.suffix in (".html",".js"):
                self.assertIsNone(forbidden.search(path.read_text()),str(path))
