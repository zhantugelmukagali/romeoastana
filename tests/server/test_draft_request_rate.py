"""Exercise the real worker REMAP client against the documented draft rate limit."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from uuid import uuid4

import httpx

from bonuspro.worker import Worker


class DraftRequestRateTests(unittest.TestCase):
    def test_worker_document_reads_fit_eleven_requests_per_three_seconds(self):
        clock = [100.0]
        received = []

        def sleep(seconds):
            clock[0] += seconds

        def provider(request):
            recent = [stamp for stamp in received if clock[0] - stamp < 3]
            if len(recent) >= 11:
                return httpx.Response(429, headers={"X-Lognex-Retry-After": "3000"})
            received.append(clock[0])
            return httpx.Response(200, json={"meta": {"size": 0}, "rows": []})

        tenant, order = str(uuid4()), str(uuid4())
        installation = SimpleNamespace(remap_token=lambda account: "synthetic-draft-token")
        # Construction is local; no ledger operation is needed to create a provider reader.
        worker = Worker(object(), installation, tenant)
        with patch("bonuspro.adapters.moysklad.remap.time.monotonic",
                   side_effect=lambda: clock[0]), patch(
                       "bonuspro.adapters.moysklad.remap.time.sleep", side_effect=sleep):
            client = worker.client()
            client.transport = httpx.MockTransport(provider)
            for _ in range(20):
                self.assertEqual(client.positions("customerorder", order), [])
        self.assertEqual(len(received), 20)
        self.assertGreater(received[-1] - received[0], 3)


if __name__ == "__main__":
    unittest.main()
