"""Read-only cabinet over real PostgreSQL; provider directory uses a synthetic boundary."""
import time,unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.domain.finance import Rules
from bonuspro.adapters.moysklad.remap import EntityMissing
from bonuspro.adapters.moysklad.vendor import UserContext
import test_return_api as fixture

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
        self.fx.ledger.reserve(self.fx.tenant,self.fx.wallet,order_id=str(uuid4()),points=50,eligible_minor=1000000,
            rules=Rules(),quote_hash="a"*64,key="portal-held",actor_id=self.fx.user)
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        result=self.client.get("/api/admin/overview",headers=self.headers)
        self.assertEqual(result.status_code,200,result.text);data=result.json()
        self.assertEqual((data["customers"],data["balance"],data["reserved"],data["credited"],data["redeemed"]),(1,"300","50","1000","700"))
        self.assertEqual(data["state"],"SettingsRequired");self.assertIsNone(data["rules"])
        self.assertEqual(data["attention"],1)
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

if __name__=="__main__":unittest.main()
