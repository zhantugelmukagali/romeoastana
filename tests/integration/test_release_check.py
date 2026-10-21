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
                                  "ledger_postings","order_monitors","jobs","personal_discount_policy_versions",
                                  "customer_personal_discount_versions","personal_discount_applications","personal_discount_voids")}
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


class EmptyAccountProbeTests(unittest.TestCase):
    def test_activated_empty_account_reports_missing_control_order_without_changing_setup_or_money(self):
        import test_activation as activation_fixture
        from bonuspro.adapters.postgres.automation import Automation
        from bonuspro.adapters.postgres.readiness import Readiness
        f=activation_fixture.ActivationTests();f.setUp();self.addCleanup(f.doCleanups)
        f.activate();fx=f.fx
        Automation(fx.ledger).cooldown(fx.tenant,0,None)
        self.assertTrue(Readiness(fx.ledger).report(fx.tenant)["ready"])
        with fx.ledger.transaction(fx.tenant) as conn:
            marker=str(conn.execute("SELECT marker_id FROM bonuspro.widget_bindings").fetchone()["marker_id"])
        tables=("wallets","order_contexts","reservations","ledger_transactions","ledger_postings",
                "order_monitors","jobs","personal_discount_policy_versions","customer_personal_discount_versions",
                "personal_discount_applications","personal_discount_voids","automation_config","sync_state",
                "activation_requests","activation_completions")
        def snapshot():
            with fx.ledger.transaction(fx.tenant) as conn:
                return {table:conn.execute("SELECT to_jsonb(t) AS row FROM bonuspro."+table+" t").fetchall()
                        for table in tables}
        before=snapshot();requests=[]
        self.assertEqual(before["ledger_transactions"],[])
        self.assertEqual(before["wallets"],[])
        def provider(request):
            requests.append(request)
            if request.url.path.endswith("/customerorder/metadata/attributes"):
                return httpx.Response(200,json={"meta":{},"rows":[{
                    "id":marker,"name":"БонусПро: операция списания","type":"string","required":False,
                    "accountId":fx.tenant,"meta":{"type":"attributemetadata",
                        "href":BASE+"/entity/customerorder/metadata/attributes/"+marker}}]})
            if request.url.path.endswith("/entity/customerorder"):
                return httpx.Response(200,json={"meta":{"size":0},"rows":[]})
            raise AssertionError("Empty account must not request any document graph")
        def factory(token,tenant,**kwargs):
            return RemapClient(token,tenant,transport=httpx.MockTransport(provider),**kwargs)
        report=check_account(fx.ledger,fx.install,fx.tenant,client_factory=factory)
        self.assertTrue(report["read_only"])
        self.assertTrue(report["operational_ready"])
        self.assertFalse(report["business_acceptance_verified"])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assertEqual(report["sampled_orders"],0)
        self.assertEqual(report["outcomes"],{})
        self.assertEqual(report["failures"],[{"stage":"discovery","code":"no_orders_to_check"}])
        self.assertEqual(snapshot(),before)
        self.assertEqual(len(requests),2)
        self.assertTrue(all(request.method=="GET" for request in requests))
        for value in (fx.tenant,marker,fx.actor,"synthetic"):
            self.assertNotIn(value,json.dumps(report))

if __name__=="__main__":unittest.main()
