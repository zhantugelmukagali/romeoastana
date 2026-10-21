"""Read-only cabinet over real PostgreSQL; provider directory uses a synthetic boundary."""
import time,unittest
from dataclasses import replace
from uuid import uuid4
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.domain.finance import Rules
from bonuspro.adapters.moysklad.remap import EntityMissing
from bonuspro.adapters.moysklad.vendor import UserContext
import test_return_api as fixture
import test_settlements as settlement_fixture

class PortalAPITests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ReturnAPITests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.customer=str(uuid4());self.directory_id=self.customer;self.searches=[]
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                         (self.fx.tenant,self.customer,self.fx.wallet))
        outer=self
        class Provider:
            def __init__(self,token,tenant):assert token=="synthetic-remap" and tenant==outer.fx.tenant
            def counterparties(self,search,offset):
                outer.searches.append((search,offset))
                return {"items":[{"id":outer.directory_id,"name":"Алия <img src=x>","phone":"+7 700 111 22 33","archived":False}],"next_offset":None}
            def entity(self,kind,identity):
                if kind!="counterparty" or identity!=outer.directory_id:raise EntityMissing("missing")
                return {"name":"Алия <img src=x>","phone":"+7 700 111 22 33"}
        config=Settings("https://testserver",self.fx.repo.config,self.fx.repo.ledger.dsn,self.fx.repo.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=self.fx.repo,remap_factory=Provider),base_url="https://testserver")
        self.addCleanup(self.client.close);self.headers=self.fx.headers()

    def test_overview_balances_and_reservations_are_real_without_moving_money(self):
        # The fixture has a pending return. Resolve it through the real API
        # before reserving; the production guard must remain enforced.
        self.assertEqual(self.client.get("/api/admin/overview",headers=self.headers).json()["attention"],1)
        resolution=self.client.post("/api/admin/returns/"+self.fx.case["id"]+"/resolve",
            headers=self.headers,json={"decision":"waive","reason":"Reviewed fixture return"})
        self.assertEqual(resolution.status_code,200,resolution.text)
        self.fx.ledger.reserve(self.fx.tenant,self.fx.wallet,order_id=str(uuid4()),points=50,eligible_minor=1000000,
            rules=Rules(),quote_hash="a"*64,key="portal-held",actor_id=self.fx.user)
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        result=self.client.get("/api/admin/overview",headers=self.headers)
        self.assertEqual(result.status_code,200,result.text);data=result.json()
        self.assertEqual((data["customers"],data["balance"],data["reserved"],data["credited"],data["redeemed"]),(1,"300","50","1000","700"))
        self.assertEqual(data["state"],"SettingsRequired");self.assertIsNone(data["rules"]);self.assertIsNone(data["processing"])
        self.assertEqual(data["attention"],0)
        customer=self.client.get("/api/admin/customers/"+self.customer,headers=self.headers).json()
        self.assertEqual((customer["balance"],customer["available"],customer["reserved"]),(300,250,50))
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),before)

    def test_directory_search_and_customer_history_use_authoritative_mapping(self):
        rows=self.client.get("/api/admin/customers",params={"q":"700","offset":0},headers=self.headers)
        self.assertEqual(rows.status_code,200,rows.text)
        self.assertEqual(self.searches,[("700",0)])
        self.assertEqual(rows.json()["items"][0]["balance"],300)
        history=self.client.get("/api/admin/activity",params={"customer":self.customer},headers=self.headers)
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual(sorted(x["points"] for x in history.json()["items"]),[-700,1000])
        self.assertTrue(all(x["customer_id"]==self.customer for x in history.json()["items"]))
        self.assertNotIn("synthetic-remap",rows.text+history.text)
        self.assertEqual(self.client.post("/api/admin/overview",headers=self.headers,json={"balance":100}).status_code,405)

    def test_directory_read_does_not_enrol_a_new_customer(self):
        self.directory_id=str(uuid4())
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            before=conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"]
        self.client.get("/api/admin/customers",headers=self.headers)
        data=self.client.get("/api/admin/customers/"+self.directory_id,headers=self.headers).json()
        self.assertFalse(data["enrolled"]);self.assertEqual(data["balance"],0)
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"],before)

    def test_anonymous_non_admin_bad_search_and_foreign_ids_are_rejected(self):
        for path in ("/api/admin/overview","/api/admin/customers","/api/admin/activity","/api/admin/customers/"+self.customer):
            self.assertEqual(self.client.get(path).status_code,401)
            self.assertEqual(self.client.get(path,headers=self.fx.headers("worker")).status_code,403)
        for params in ({"q":"a"},{"q":"x"*101},{"offset":-1},{"offset":100001}):
            self.assertEqual(self.client.get("/api/admin/customers",params=params,headers=self.headers).status_code,422)
        self.assertEqual(self.client.get("/api/admin/customers/"+str(uuid4()),headers=self.headers).status_code,404)
        self.assertEqual(self.client.get("/api/admin/activity",params={"before":str(uuid4())},headers=self.headers).status_code,404)

    def test_other_tenant_money_and_cursor_are_not_visible(self):
        other,wallet,user=(str(uuid4()) for _ in range(3))
        self.fx.repo.change(other,{"iat":int(time.time())-1,"exp":int(time.time())+60,"jti":str(uuid4())},"Install","other-provider-token")
        self.fx.ledger.create_wallet(other,wallet)
        credit=self.fx.ledger.credit(other,wallet,points=99999,key="foreign-credit",actor_id=user,reason="Other account fixture")
        data=self.client.get("/api/admin/overview",headers=self.headers).json()
        self.assertEqual(data["balance"],"300")
        rows=self.client.get("/api/admin/activity",headers=self.headers).json()["items"]
        self.assertFalse(any(row["id"]==credit for row in rows))
        self.assertEqual(self.client.get("/api/admin/activity",params={"before":credit},headers=self.headers).status_code,404)
        token=self.fx.repo.create_session(UserContext(other,user,"admin"))
        own=self.client.get("/api/admin/overview",headers={"Authorization":"Bearer "+token}).json()
        self.assertEqual(own["balance"],"99999")

    def test_keyset_history_pages_do_not_repeat_or_leak_unmapped_customer_history(self):
        for i in range(53):
            self.fx.ledger.credit(self.fx.tenant,self.fx.wallet,points=1,key="page-"+str(i),actor_id=self.fx.user,reason="History fixture")
        first=self.client.get("/api/admin/activity",headers=self.headers).json()
        second=self.client.get("/api/admin/activity",params={"before":first["next_cursor"]},headers=self.headers).json()
        self.assertEqual(len(first["items"]),50);self.assertEqual(len(second["items"]),5)
        self.assertEqual(len({x["id"] for x in first["items"]+second["items"]}),55)
        missing=self.client.get("/api/admin/activity",params={"customer":str(uuid4())},headers=self.headers).json()
        self.assertEqual(missing["items"],[])


    def test_overview_exposes_only_current_tenant_failed_orders_despite_a_fresh_heartbeat(self):
        order=str(uuid4())
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.automation_config(tenant_id,actor_id) VALUES (%s,%s)",
                         (self.fx.tenant,self.fx.user))
            conn.execute("INSERT INTO bonuspro.sync_state(tenant_id,scan_until,heartbeat_at) "
                         "VALUES (%s,clock_timestamp(),clock_timestamp())",(self.fx.tenant,))
            conn.execute("INSERT INTO bonuspro.order_monitors(tenant_id,order_id,outcome) "
                         "VALUES (%s,%s,'provider_error')",(self.fx.tenant,order))
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        data=self.client.get("/api/admin/overview",headers=self.headers).json()
        self.assertEqual(data["processing"]["provider_errors"],1)
        self.assertIsNotNone(data["processing"]["heartbeat_at"])
        self.assertIsNone(data["processing"]["last_error"])
        with self.fx.ledger.transaction(self.fx.tenant) as conn:
            conn.execute("UPDATE bonuspro.order_monitors SET outcome='waiting_payment' "
                         "WHERE tenant_id=%s AND order_id=%s",(self.fx.tenant,order))
        other=str(uuid4())
        self.fx.repo.change(other,{"iat":int(time.time())-1,"exp":int(time.time())+60,"jti":str(uuid4())},
                            "Install","synthetic-private-token")
        with self.fx.ledger.transaction(other) as conn:
            conn.execute("INSERT INTO bonuspro.order_monitors(tenant_id,order_id,outcome) "
                         "VALUES (%s,%s,'provider_error')",(other,str(uuid4())))
        response=self.client.get("/api/admin/overview",headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["processing"]["provider_errors"],0)
        self.assertNotIn("synthetic-private-token",response.text)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),before)


class CustomerRecalculationAPITests(unittest.TestCase):
    def setUp(self):
        self.fx=settlement_fixture.SettlementTests();self.fx.setUp()
        self.sale=self.fx.sale;self.remote_reads=[];outer=self
        class Provider:
            def __init__(inner,token,tenant):
                if token!="synthetic" or tenant!=outer.sale.tenant:raise AssertionError("unexpected provider identity")
            def entity(inner,kind,identity):
                outer.remote_reads.append((kind,identity))
                if kind!="counterparty" or identity!=outer.sale.customer:raise EntityMissing("missing")
                return {"name":"Settlement customer","phone":""}
        settings=Settings("https://testserver",self.sale.install.config,self.sale.ledger.dsn,self.sale.install.lifecycle_dsn)
        self.client=TestClient(create_app(settings,store=self.sale.install,remap_factory=Provider),base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.headers=self.headers_for(self.sale)
        self.path="/api/admin/customers/"+self.sale.customer+"/recalculations"

    def headers_for(self,sale,role="admin"):
        return {"Authorization":"Bearer "+sale.install.create_session(UserContext(sale.tenant,sale.actor,role)),
                "Origin":"https://testserver"}

    def zero_return(self):
        operation=self.sale.prepare(points=500);saved=self.sale.saved
        self.fx.saved=replace(saved,lines=(replace(saved.lines[0],discount_bps=500),saved.lines[1]),
            marker="bonuspro:v1:"+operation["id"],
            snapshot=replace(saved.snapshot,total_minor=1150000,eligible_minor=950000,revision="saved"))
        self.fx.process();self.fx.process(self.fx.proof(1))

    def financial_snapshot(self):
        with self.sale.ledger.transaction(self.sale.tenant) as conn:
            transactions=conn.execute("SELECT * FROM bonuspro.ledger_transactions ORDER BY id").fetchall()
            postings=conn.execute("SELECT * FROM bonuspro.ledger_postings ORDER BY transaction_id,account").fetchall()
            events=conn.execute("SELECT * FROM bonuspro.settlement_events ORDER BY id").fetchall()
        return transactions,postings,events,self.fx.funds()

    def listing(self,**params):
        response=self.client.get(self.path,params=params,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_zero_net_return_details_are_visible_without_a_zero_journal_or_read_side_effects(self):
        self.zero_return();before=self.financial_snapshot()
        activity=self.client.get("/api/admin/activity",params={"customer":self.sale.customer},headers=self.headers).json()
        data=self.listing();self.assertEqual(len(data["items"]),1);event=data["items"][0]
        self.assertEqual((event["earn_delta"],event["refund_delta"],event["net_delta"]),(-500,500,0))
        self.assertEqual(event["order_id"],self.sale.order);self.assertTrue(event["created_at"])
        self.assertEqual(set(event),{"id","order_id","earn_delta","refund_delta","net_delta","created_at"})
        self.assertIsNone(data["next_cursor"]);self.assertEqual(self.listing(),data)
        self.assertEqual(self.remote_reads,[])  # Local event history does not depend on REMAP availability.
        card=self.client.get("/api/admin/customers/"+self.sale.customer,headers=self.headers)
        self.assertEqual(card.status_code,200,card.text);self.assertEqual(card.json()["recalculations"],data)
        self.assertEqual(len(self.remote_reads),1)
        self.assertFalse(any(row["kind"]=="settlement" for row in activity["items"]))
        self.assertFalse(any(row["delta"]==0 for row in before[0]))
        self.assertTrue(all(row["settlement_event_id"] is None for row in before[0]))
        repeated=self.client.get("/api/admin/activity",params={"customer":self.sale.customer},headers=self.headers).json()
        self.assertEqual(repeated,activity);self.assertEqual(self.financial_snapshot(),before)
        self.assertEqual(self.fx.funds()["balance"],5000)

    def test_nonzero_return_details_describe_the_one_existing_balance_operation(self):
        self.fx.process();self.fx.process(self.fx.proof(1))
        before=self.financial_snapshot();event=self.listing()["items"][0]
        self.assertEqual((event["earn_delta"],event["refund_delta"],event["net_delta"]),(-500,0,-500))
        activity=self.client.get("/api/admin/activity",params={"customer":self.sale.customer},headers=self.headers).json()
        adjustments=[row for row in activity["items"] if row["kind"]=="settlement"]
        self.assertEqual(len(adjustments),1);self.assertEqual(adjustments[0]["points"],-500)
        postings=[row for row in before[0] if str(row["settlement_event_id"])==event["id"]]
        self.assertEqual(len(postings),1)
        self.assertEqual(self.financial_snapshot(),before);self.assertEqual(self.fx.funds()["balance"],5000)

    def test_keyset_pages_cover_real_return_and_undo_events_without_duplicates_or_cursor_coupling(self):
        self.zero_return()
        for index in range(20):
            self.fx.process(self.fx.proof() if index%2==0 else self.fx.proof(1))
        before=self.financial_snapshot();first=self.listing()
        self.assertEqual(len(first["items"]),20);self.assertTrue(first["next_cursor"])
        second=self.listing(before=first["next_cursor"])
        self.assertEqual(len(second["items"]),1);self.assertIsNone(second["next_cursor"])
        rows=first["items"]+second["items"]
        self.assertEqual(len({row["id"] for row in rows}),21)
        self.assertTrue(all(row["net_delta"]==0 for row in rows))
        self.assertEqual(self.listing(before=first["next_cursor"]),second)
        activity=self.client.get("/api/admin/activity",params={"customer":self.sale.customer},headers=self.headers).json()
        wrong=self.client.get(self.path,params={"before":activity["items"][0]["id"]},headers=self.headers)
        self.assertEqual(wrong.status_code,404)
        wrong=self.client.get("/api/admin/activity",params={"customer":self.sale.customer,"before":first["next_cursor"]},headers=self.headers)
        self.assertEqual(wrong.status_code,404)
        self.assertEqual(self.financial_snapshot(),before);self.assertEqual(self.fx.funds()["balance"],5000)
        self.assertEqual(self.remote_reads,[])

    def test_cursor_is_bound_to_tenant_and_customer_wallet_and_missing_customer_reads_do_not_enrol(self):
        self.zero_return();event=self.listing()["items"][0]
        customer,order=(str(uuid4()) for _ in range(2))
        self.sale.checkout.preview(self.sale.tenant,
            replace(self.sale.saved.snapshot,order_id=order,counterparty_id=customer),
            requested_points=0,actor_id=self.sale.actor,eligibility_version=1)
        sibling="/api/admin/customers/"+customer+"/recalculations"
        self.assertEqual(self.client.get(sibling,headers=self.headers).json(),{"items":[],"next_cursor":None})
        self.assertEqual(self.client.get(sibling,params={"before":event["id"]},headers=self.headers).status_code,404)
        other=settlement_fixture.SettlementTests();other.setUp();other.process();other.process(other.proof(1))
        foreign_headers=self.headers_for(other.sale)
        foreign_path="/api/admin/customers/"+other.sale.customer+"/recalculations"
        foreign=self.client.get(foreign_path,headers=foreign_headers).json()["items"][0]
        self.assertEqual(self.client.get(self.path,params={"before":foreign["id"]},headers=self.headers).status_code,404)
        self.assertEqual(self.client.get(foreign_path,params={"before":event["id"]},headers=foreign_headers).status_code,404)
        self.assertEqual(self.client.get(self.path,headers=foreign_headers).json(),{"items":[],"next_cursor":None})
        with self.sale.ledger.transaction(self.sale.tenant) as conn:
            before=conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"]
        missing="/api/admin/customers/"+str(uuid4())+"/recalculations"
        self.assertEqual(self.client.get(missing,headers=self.headers).json(),{"items":[],"next_cursor":None})
        self.assertEqual(self.client.get(missing,params={"before":event["id"]},headers=self.headers).status_code,404)
        self.assertEqual(len(self.listing()["items"]),1)
        with self.sale.ledger.transaction(self.sale.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM bonuspro.wallets").fetchone()["n"],before)
        self.assertEqual(self.remote_reads,[])

    def test_recalculations_are_admin_only_get_only_and_reject_invalid_or_unknown_cursors(self):
        self.assertEqual(self.client.get(self.path).status_code,401)
        self.assertEqual(self.client.get(self.path,headers=self.headers_for(self.sale,"worker")).status_code,403)
        self.assertEqual(self.client.post(self.path,headers=self.headers,json={}).status_code,405)
        self.assertEqual(self.client.get(self.path,params={"before":"not-a-uuid"},headers=self.headers).status_code,422)
        self.assertEqual(self.client.get(self.path,params={"before":str(uuid4())},headers=self.headers).status_code,404)
        self.assertEqual(self.remote_reads,[])

if __name__=="__main__":unittest.main()
