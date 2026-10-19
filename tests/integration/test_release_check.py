"""Installed-account probe uses real REMAP parser and never enrols or posts money."""
import json,unittest
from uuid import uuid4
import httpx
from bonuspro.releasecheck import check_account
from bonuspro.adapters.moysklad.remap import BASE,RemapClient
import test_widget_provider_contract as fixtures

class ReleaseProbeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WidgetProviderContractTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.requests=[];self.foreign=False
        self.f.order_overrides={"created":"2026-10-19 12:00:00.000"}
    def provider(self,request):
        self.requests.append(request)
        fx=self.f.fx
        path=request.url.path
        if path.endswith("/customerorder/metadata/attributes"):
            identity=fx.marker
            return httpx.Response(200,json={"meta":{},"rows":[{
                "id":identity,"name":"БонусПро: операция списания","type":"string","required":False,
                "accountId":fx.tenant,"meta":{"type":"attributemetadata",
                    "href":BASE+"/entity/customerorder/metadata/attributes/"+identity}}]})
        if path.endswith("/entity/customerorder"):
            return httpx.Response(200,json={"meta":{"size":1},"rows":[{
                "id":fx.order,"accountId":str(uuid4()) if self.foreign else fx.tenant,
                "meta":{"type":"customerorder","href":BASE+"/entity/customerorder/"+fx.order}}]})
        return self.f.provider(request)
    def probe(self):
        fx=self.f.fx
        def factory(token,tenant,**kwargs):
            return RemapClient(token,tenant,transport=httpx.MockTransport(self.provider),**kwargs)
        return check_account(fx.ledger,fx.install,fx.tenant,sample_limit=1,client_factory=factory)
    def snapshot(self):
        fx=self.f.fx
        with fx.ledger.transaction(fx.tenant) as conn:
            counts={table:conn.execute("SELECT count(*) n FROM bonuspro."+table).fetchone()["n"]
                    for table in ("wallets","order_contexts","reservations","ledger_transactions",
                                  "ledger_postings","order_monitors","jobs")}
        return dict(counts=counts,funds=fx.ledger.snapshot(fx.tenant,fx.wallet))
    def test_real_account_graph_probe_is_read_only_and_not_a_financial_acceptance_claim(self):
        before=self.snapshot()
        report=self.probe()
        self.assertEqual(report["failures"],[],report)
        self.assertEqual(report["sampled_orders"],1,report)
        self.assertEqual(report["outcomes"],{"waiting_shipment":1})
        self.assertFalse(report["business_acceptance_verified"])
        # Fixture has no automation cutoff/heartbeat, so a good document cannot fake readiness.
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(all(r.method=="GET" for r in self.requests))
        serialized=json.dumps(report)
        for value in (self.f.fx.tenant,self.f.fx.order,self.f.fx.customer,"synthetic"):
            self.assertNotIn(value,serialized)
    def test_invalid_live_tax_mode_is_detected_without_reserving_or_echoing_document(self):
        before=self.snapshot()
        self.f.order_overrides.update(vatEnabled=True,vatIncluded=False)
        report=self.probe()
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"vat_mode_unsupported"}],report)
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(report["ready_for_live_acceptance"])
    def test_foreign_discovery_row_is_not_followed(self):
        self.foreign=True
        report=self.probe()
        self.assertEqual(report["failures"],[{"stage":"discovery","code":"saved_order_invalid"}],report)
        self.assertFalse(any(r.url.path.endswith("/customerorder/"+self.f.fx.order) for r in self.requests))
        self.assertEqual(report["sampled_orders"],0)

if __name__=="__main__":unittest.main()
