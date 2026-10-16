"""Prevent accidental shipment of the former browser-only demo."""
import unittest
from pathlib import Path

class ReleaseSurfaceTests(unittest.TestCase):
    def test_shipped_interface_has_no_demo_transport_or_fake_financial_data(self):
        root=Path(__file__).resolve().parents[2]/"web"
        forbidden=("VITE_BROWSER_DEMO","browserSeed","browserDemo","synthetic-session","synthetic-remap",
                   "DEMO_MODE","demo.db","mockServiceWorker","тестовый баланс","демо-режим")
        for path in root.rglob("*"):
            if path.suffix in (".html",".js"):
                source=path.read_text().lower()
                for token in forbidden:self.assertNotIn(token.lower(),source,str(path))
