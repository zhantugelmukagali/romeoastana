import os,time,unittest
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import psycopg
from bonuspro.security import Secrets
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.imports import Imports
from bonuspro.adapters.postgres.ledger import Ledger,Conflict,NotFound
from bonuspro.importing import ImportInvalid

class Client:
    def __init__(self):self.calls=[];self.archived=False
    def entity(self,kind,identity):
        assert kind=="counterparty"
        self.calls.append(identity)
        return dict(id=identity,name="Customer "+identity,archived=self.archived)

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.customer=(str(uuid4()) for _ in range(3))
        self.ledger=Ledger(os.environ["TEST_APP_DSN"]);self.imports=Imports(self.ledger);self.client=Client()
        self.install=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],
                                  Secrets(str(uuid4()),"synthetic.test","x"*40,b"k"*32))
        self.install.change(self.tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),"Install","synthetic")
    def csv(self,points=100,customer=None):
        return "counterparty_id,bonus_balance\n"+(customer or self.customer)+","+str(points)+"\n"
    def preview(self,points=100,text=None):return self.imports.preview(self.tenant,text or self.csv(points),self.actor,self.client)
    def commit(self,batch,reason="Reconciled migration"):
        return self.imports.commit(self.tenant,batch["id"],digest=batch["digest"],actor=self.actor,reason=reason)
    def count(self,table):
        with self.ledger.transaction(self.tenant) as conn:return conn.execute("SELECT count(*) n FROM bonuspro."+table).fetchone()["n"]
    def test_preview_no_credit_and_commit_is_idempotent(self):
        b=self.preview()
        self.assertEqual(self.client.calls,[self.customer]);self.assertEqual(self.count("ledger_transactions"),0)
        self.assertEqual(self.preview()["id"],b["id"])
        self.assertEqual(self.commit(b),dict(committed=True,total=100,customers=1))
        self.assertEqual(self.commit(b),dict(committed=True,total=100,customers=1))
        self.assertEqual(self.count("ledger_transactions"),1)
        self.assertIsNotNone(self.imports.history(self.tenant)["items"][0]["committed_at"])
        with self.assertRaises(Conflict):self.commit(b,"Changed reason")
    def test_concurrent_commit_posts_once(self):
        b=self.preview()
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:self.commit(b),range(3)))
        self.assertTrue(all(x["committed"] for x in results));self.assertEqual(self.count("ledger_transactions"),1)
    def test_zero_balance_and_duplicate_customer_across_batches(self):
        zero=self.preview(0);other=self.preview(10)
        self.commit(zero)
        self.assertEqual(self.count("opening_balances"),1);self.assertEqual(self.count("ledger_transactions"),0)
        with self.assertRaises(Conflict):self.commit(other)
        self.assertEqual(self.count("import_commits"),1)
    def test_changed_generation_rejects_preview(self):
        b=self.preview()
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET generation=%s WHERE tenant_id=%s",(str(uuid4()),self.tenant))
        with self.assertRaises(Conflict):self.commit(b)
        self.assertEqual(self.count("ledger_transactions"),0)
    def test_active_program_cannot_import_but_successful_retry_still_works(self):
        b=self.preview();self.commit(b)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.installations SET state='Activated' WHERE tenant_id=%s",(self.tenant,))
        self.assertTrue(self.commit(b)["committed"])
        with self.assertRaises(Conflict):self.preview()
    def test_dirty_wallet_rolls_back_whole_batch(self):
        second=str(uuid4());customers=sorted([self.customer,second]);wallet=str(uuid4())
        with self.ledger.transaction(self.tenant) as conn:
            conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(self.tenant,wallet))
            conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",(self.tenant,customers[-1],wallet))
        self.ledger.credit(self.tenant,wallet,points=5,key="existing",actor_id=self.actor,reason="Existing")
        b=self.preview(text="counterparty_id,bonus_balance\n"+"\n".join(c+",100" for c in customers))
        with self.assertRaises(Conflict):self.commit(b)
        self.assertEqual(self.count("opening_balances"),0);self.assertEqual(self.count("import_commits"),0)
        self.assertEqual(self.count("ledger_transactions"),1)
    def test_tenant_isolation_archived_and_immutable_history(self):
        b=self.preview()
        with self.assertRaises(NotFound):
            self.imports.commit(str(uuid4()),b["id"],digest=b["digest"],actor=self.actor,reason="Test")
        self.client.archived=True
        with self.assertRaises(ImportInvalid):self.preview()
        with self.assertRaises(psycopg.Error):
            with self.ledger.transaction(self.tenant) as conn:conn.execute("DELETE FROM bonuspro.import_batches")
    def test_mismatched_digest_never_credits(self):
        b=self.preview()
        with self.assertRaises(Conflict):self.imports.commit(self.tenant,b["id"],digest="0"*64,actor=self.actor,reason="Test")
        self.assertEqual(self.count("ledger_transactions"),0)

if __name__=="__main__":unittest.main()
