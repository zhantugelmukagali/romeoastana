import os,time,unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
import psycopg
from bonuspro.security import Secrets
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.ledger import Ledger, Conflict
from bonuspro.adapters.postgres.checkout import Checkout, OrderSnapshot
from bonuspro.adapters.postgres.redemptions import Redemptions
from bonuspro.adapters.moysklad.order import SavedOrder, Line

class RedemptionTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.customer,self.order,self.marker=(str(uuid4()) for _ in range(5))
        self.ledger=Ledger(os.environ["TEST_APP_DSN"])
        self.checkout=Checkout(self.ledger);self.redemptions=Redemptions(self.ledger)
        secret=Secrets(str(uuid4()),"synthetic.test","x"*40,b"k"*32)
        self.install=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],secret)
        self.install.change(self.tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),
                            "Install","synthetic")
        self.checkout.save_rules(self.tenant,earn_bps=500,spend_bps=3000,delivery_policy="exclude",
                    expected_version=0,request_id=str(uuid4()),actor_id=self.actor,reason="initial")
        self.saved=SavedOrder(OrderSnapshot(self.tenant,self.order,self.customer,"rev-1","KZT",1200000,1000000),
             (Line(str(uuid4()),"product",str(uuid4()),Decimal(1),Decimal(1000000),0,Decimal(0),False,None),
              Line(str(uuid4()),"service",str(uuid4()),Decimal(1),Decimal(200000),0,Decimal(0),False,None)),
             str(uuid4()),True,False,None,"0001")
        preview=self.checkout.preview(self.tenant,self.saved.snapshot,requested_points=0,
                                       actor_id=self.actor,eligibility_version=1)
        self.wallet=preview["wallet_id"]
        self.ledger.credit(self.tenant,self.wallet,points=5000,key="opening",actor_id=self.actor,reason="synthetic")
        self.redemptions.bind(self.tenant,self.marker,self.actor)
        self.activate()
    def activate(self):
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET state='Activated' WHERE tenant_id=%s",(self.tenant,))
    def prepare(self,request=None,points=1000,saved=None,source=None):
        saved=saved or self.saved
        return self.redemptions.prepare(self.tenant,saved,points=points,request_id=request or str(uuid4()),
                         actor_id=self.actor,expected_source=source or saved.source_hash)
    def persisted(self,result):
        lines=(replace(self.saved.lines[0],discount_bps=1000),self.saved.lines[1])
        return replace(self.saved,lines=lines,marker="bonuspro:v1:"+result["id"],
                       snapshot=replace(self.saved.snapshot,revision="rev-2",total_minor=1100000,eligible_minor=900000))
    def test_prepare_reserves_without_debit_and_keeps_delivery(self):
        result=self.prepare()
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet),dict(balance=5000,reserved=1000,available=4000))
        self.assertEqual(result["state"],"verifying")
        self.assertEqual(result["update_state"]["positions"][1],{"id":self.saved.lines[1].id})
        self.assertEqual(result["update_state"]["attributes"],[{"id":self.marker,"value":"bonuspro:v1:"+result["id"]}])
    def test_saved_proof_commits_once_even_on_concurrent_retries(self):
        prepared=self.prepare();saved=self.persisted(prepared)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.redemptions.confirm_saved(self.tenant,saved),range(4)))
        self.assertEqual(len({r["transaction_id"] for r in results}),1)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet),dict(balance=4000,reserved=0,available=4000))
    def test_host_acknowledgement_without_saved_marker_cannot_debit(self):
        prepared=self.prepare();saved=self.persisted(prepared)
        for candidate in (self.saved,replace(saved,marker=None),replace(saved,marker="forged"),
                          replace(saved,snapshot=replace(saved.snapshot,counterparty_id=str(uuid4())))):
            self.assertFalse(self.redemptions.confirm_saved(self.tenant,candidate)["verified"])
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["balance"],5000)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["reserved"],1000)
    def test_same_total_but_changed_delivery_is_not_proof(self):
        prepared=self.prepare();saved=self.persisted(prepared)
        lines=(replace(saved.lines[0],price=Decimal(900000)),replace(saved.lines[1],price=Decimal(290000)))
        self.assertFalse(self.redemptions.confirm_saved(self.tenant,replace(saved,lines=lines))["verified"])
    def test_prepare_retry_does_not_redispatch_or_duplicate(self):
        request=str(uuid4());first=self.prepare(request)
        second=self.prepare(request)
        self.assertEqual(first["id"],second["id"]);self.assertIsNone(second["update_state"])
        with self.assertRaises(Conflict):self.prepare(request,points=2000)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["reserved"],1000)
    def test_concurrent_double_click_creates_one_attempt(self):
        request=str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.prepare(request),range(2)))
        self.assertEqual(len({r["id"] for r in results}),1)
        self.assertEqual(sum(r["update_state"] is not None for r in results),1)
    def test_stale_snapshot_is_rejected_before_reservation(self):
        with self.assertRaises(Conflict):self.prepare(source="0"*64)
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["reserved"],0)
    def test_order_customer_change_cannot_spend_original_wallet(self):
        saved=replace(self.saved,snapshot=replace(self.saved.snapshot,counterparty_id=str(uuid4())))
        with self.assertRaises(Conflict):self.prepare(saved=saved)
    def test_insufficient_balance_and_unactivated_installation(self):
        with self.assertRaises(Conflict):self.prepare(points=4000)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET state='SettingsRequired' WHERE tenant_id=%s",(self.tenant,))
        with self.assertRaises(Conflict):self.prepare()
        self.assertEqual(self.ledger.snapshot(self.tenant,self.wallet)["reserved"],0)
    def test_plan_immutable_and_tenant_isolated(self):
        prepared=self.prepare()
        with self.ledger.transaction(self.tenant) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.redemption_attempts").fetchone()["n"],1)
        with self.ledger.transaction(str(uuid4())) as conn:
            self.assertEqual(conn.execute("SELECT count(*) n FROM bonuspro.redemption_attempts").fetchone()["n"],0)
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.tenant) as conn:
                conn.execute("DELETE FROM bonuspro.redemption_attempts WHERE id=%s",(prepared["id"],))
    def test_unknown_result_reservation_cannot_expire_or_release(self):
        prepared=self.prepare()
        with self.assertRaises(Conflict):self.ledger.release_unapplied(self.tenant,prepared["id"])

if __name__=="__main__":unittest.main()
