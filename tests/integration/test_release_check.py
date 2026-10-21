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


class HistoricalAccountProbeTests(unittest.TestCase):
    """Old unrelated demos cannot become a control sale or hide live failures."""
    TABLES=("installations","widget_bindings","wallets","customer_wallets","order_contexts",
            "order_customer_rebindings","reservations","redemption_attempts","ledger_transactions",
            "ledger_postings","order_monitors","jobs","order_accruals","settlement_events",
            "settlement_cases","reconciliation_issues","personal_discount_policy_versions",
            "customer_personal_discount_versions","personal_discount_applications",
            "personal_discount_voids","presale_undo_requests","presale_undo_completions",
            "automation_config","sync_state","activation_requests","activation_completions")

    def setUp(self):
        from datetime import timedelta,timezone
        import test_activation as activation_fixture
        from bonuspro.adapters.postgres.automation import Automation
        from bonuspro.adapters.postgres.readiness import Readiness
        self.activation=activation_fixture.ActivationTests()
        self.activation.setUp()
        self.addCleanup(self.activation.doCleanups)
        self.activation.activate()
        self.fx=self.activation.fx
        self.automation=Automation(self.fx.ledger)
        self.automation.cooldown(self.fx.tenant,0,None)
        self.assertTrue(Readiness(self.fx.ledger).report(self.fx.tenant)["ready"])
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.marker=str(conn.execute("SELECT marker_id FROM bonuspro.widget_bindings").fetchone()["marker_id"])
            self.cutoff=conn.execute("SELECT starts_at FROM bonuspro.automation_config").fetchone()["starts_at"]
        self.msk=timezone(timedelta(hours=3))
        self.orders=[]
        self.requests=[]

    def provider_timestamp(self,value):
        return value.astimezone(self.msk).strftime("%Y-%m-%d %H:%M:%S.%f")

    def add_order(self,*,historical,currency="RUB",marker=False,demand=False):
        from datetime import timedelta
        order,customer,organization,money,product,position=(str(uuid4()) for _ in range(6))
        created=self.cutoff+timedelta(days=-1) if historical else self.cutoff+timedelta(seconds=1)
        # Discovery is sorted by updated: an old demo can be edited after launch.
        updated=self.cutoff+timedelta(minutes=1) if historical else created+timedelta(seconds=1)
        def ref(kind,identity):
            return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+identity}}
        doc=dict(id=order,accountId=self.fx.tenant,meta=ref("customerorder",order)["meta"],
            name="Private control order",agent=ref("counterparty",customer),
            organization=ref("organization",organization),rate={"currency":ref("currency",money),"value":1},
            sum=100000,payedSum=0,shippedSum=0,applicable=True,vatEnabled=False,vatIncluded=True,
            created=self.provider_timestamp(created),updated=self.provider_timestamp(updated),
            attributes=[{"id":self.marker,"value":"bonuspro:v1:"+str(uuid4())}] if marker else [])
        row=dict(id=position,accountId=self.fx.tenant,assortment=ref("product",product),
                 quantity=1,price=100000,discount=0,vat=0,vatEnabled=False)
        money_doc=dict(id=money,meta=ref("currency",money)["meta"],isoCode=currency,
            code="643" if currency=="RUB" else "398",default=True,rate=1,multiplicity=1,
            indirect=False,archived=False,system=True)
        saved=dict(id=order,customer=customer,currency_id=money,doc=doc,positions=[row],money=money_doc)
        if demand:
            identity=str(uuid4())
            saved["demand"]={**doc,"id":identity,"meta":ref("demand",identity)["meta"],
                "customerOrder":ref("customerorder",order),"returns":[]}
            doc["demands"]=[ref("demand",identity)]
        self.orders.append(saved)
        return saved

    def provider(self,request):
        self.requests.append(request)
        path=request.url.path
        if path.endswith("/customerorder/metadata/attributes"):
            return httpx.Response(200,json={"meta":{},"rows":[{
                "id":self.marker,"name":"БонусПро: операция списания","type":"string","required":False,
                "accountId":self.fx.tenant,"meta":{"type":"attributemetadata",
                    "href":BASE+"/entity/customerorder/metadata/attributes/"+self.marker}}]})
        if path.endswith("/entity/customerorder"):
            limit=int(request.url.params.get("limit",3))
            return httpx.Response(200,json={"meta":{"size":len(self.orders)},"rows":[
                {key:order["doc"][key] for key in ("id","accountId","meta")}
                for order in self.orders[:limit]]})
        for order in self.orders:
            if path.endswith("/customerorder/"+order["id"]):
                return httpx.Response(200,json=order["doc"])
            if path.endswith("/customerorder/"+order["id"]+"/positions"):
                return httpx.Response(200,json={"meta":{"size":1},"rows":order["positions"]})
            if order.get("demand"):
                if path.endswith("/demand/"+order["demand"]["id"]):
                    return httpx.Response(200,json=order["demand"])
                if path.endswith("/demand/"+order["demand"]["id"]+"/positions"):
                    return httpx.Response(200,json={"meta":{"size":1},"rows":order["positions"]})
            if path.endswith("/currency/"+order["currency_id"]):
                return httpx.Response(200,json=order["money"])
        raise AssertionError("Unexpected provider graph read: "+path)

    def probe(self):
        def factory(token,tenant,**kwargs):
            return RemapClient(token,tenant,transport=httpx.MockTransport(self.provider),**kwargs)
        return check_account(self.fx.ledger,self.fx.install,self.fx.tenant,
                             sample_limit=min(5,max(1,len(self.orders))),client_factory=factory)

    def snapshot(self):
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            return {table:conn.execute("SELECT to_jsonb(t) AS row FROM bonuspro."+table+" t").fetchall()
                    for table in self.TABLES}

    def assert_read_only_report(self,report,before):
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(all(request.method=="GET" for request in self.requests))
        self.assertTrue(report["read_only"])
        self.assertFalse(report["business_acceptance_verified"])
        serialized=json.dumps(report)
        private=[self.fx.tenant,self.fx.actor,self.marker,"synthetic","Private control order"]
        for order in self.orders:private.extend((order["id"],order["customer"],order["currency_id"]))
        for value in private:self.assertNotIn(value,serialized)

    def test_old_rub_demo_is_counted_as_historical_without_becoming_a_control_order(self):
        old=self.add_order(historical=True)
        before=self.snapshot()
        self.assertEqual(before["wallets"],[])
        self.assertEqual(before["order_contexts"],[])
        self.assertEqual(before["ledger_transactions"],[])
        report=self.probe()
        self.assertTrue(report["operational_ready"],report)
        self.assertFalse(report["ready_for_live_acceptance"],report)
        self.assertEqual(report["sampled_orders"],1)
        self.assertEqual(report["historical_orders"],1)
        self.assertEqual(report["outcomes"],{"historical":1})
        self.assertEqual(report["failures"],[{"stage":"discovery","code":"no_current_orders_to_check"}])
        # Reuse the ordinary twice-stable graph; fallback adds metadata reads,
        # never another full graph or an incomplete metadata-only exemption.
        self.assertEqual(sum(request.url.path.endswith("/customerorder/"+old["id"])
                             for request in self.requests),6)
        self.assertEqual(sum(request.url.path.endswith("/customerorder/"+old["id"]+"/positions")
                             for request in self.requests),2)
        self.assertEqual(sum(request.url.path.endswith("/currency/"+old["currency_id"])
                             for request in self.requests),1)
        self.assert_read_only_report(report,before)

    def test_current_rub_order_still_requires_supported_currency_and_never_enrols(self):
        order=self.add_order(historical=False)
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["sampled_orders"],1)
        self.assertEqual(report["outcomes"],{})
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assertTrue(any(request.url.path.endswith("/currency/"+order["currency_id"])
                            for request in self.requests))
        self.assert_read_only_report(report,before)

    def test_old_rub_order_with_existing_context_cannot_be_skipped(self):
        from bonuspro.adapters.postgres.checkout import Checkout,OrderSnapshot
        order=self.add_order(historical=True)
        Checkout(self.fx.ledger).preview(self.fx.tenant,
            OrderSnapshot(self.fx.tenant,order["id"],order["customer"],order["doc"]["updated"],
                          "KZT",100000,100000),
            requested_points=0,actor_id=self.fx.actor,eligibility_version=1)
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assert_read_only_report(report,before)

    def test_old_rub_order_with_audit_only_history_and_no_context_cannot_be_skipped(self):
        order=self.add_order(historical=True);wallet=str(uuid4())
        self.fx.ledger.create_wallet(self.fx.tenant,wallet)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.reconciliation_issues "
                "(tenant_id,order_id,wallet_id,reason,evidence_hash) VALUES (%s,%s,%s,%s,%s)",
                (self.fx.tenant,order["id"],wallet,"invalid_credited_document","a"*64))
        before=self.snapshot()
        self.assertEqual(before["order_contexts"],[])
        report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assert_read_only_report(report,before)

    def test_old_rub_order_with_bonus_marker_cannot_be_skipped(self):
        self.add_order(historical=True,marker=True)
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assert_read_only_report(report,before)

    def test_historical_demo_does_not_replace_current_kzt_graph_validation(self):
        old=self.add_order(historical=True)
        current=self.add_order(historical=False,currency="KZT")
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["sampled_orders"],2)
        self.assertEqual(report["historical_orders"],1)
        self.assertEqual(report["outcomes"],{"historical":1,"waiting_shipment":1})
        self.assertEqual(report["failures"],[],report)
        self.assertTrue(report["operational_ready"],report)
        self.assertTrue(report["ready_for_live_acceptance"],report)
        self.assertTrue(any(request.url.path.endswith("/currency/"+old["currency_id"])
                            for request in self.requests))
        self.assertTrue(any(request.url.path.endswith("/currency/"+current["currency_id"])
                            for request in self.requests))
        self.assert_read_only_report(report,before)

    def test_old_rub_order_with_demand_stays_in_review_instead_of_historical_exemption(self):
        order=self.add_order(historical=True,demand=True)
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["sampled_orders"],1)
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assertTrue(any(request.url.path.endswith("/demand/"+order["demand"]["id"])
                            for request in self.requests))
        self.assert_read_only_report(report,before)

    def test_missing_historical_creation_date_cannot_hide_unsupported_currency(self):
        order=self.add_order(historical=True)
        del order["doc"]["created"]
        before=self.snapshot();report=self.probe()
        self.assertEqual(report["historical_orders"],0)
        self.assertEqual(report["failures"],[{"stage":"order_graph","code":"currency_unsupported"}])
        self.assertFalse(report["ready_for_live_acceptance"])
        self.assert_read_only_report(report,before)

if __name__=="__main__":unittest.main()
