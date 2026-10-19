"""Personal-discount administration through authenticated HTTP and real tenant RLS."""
import os
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.security import Secrets
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.moysklad.vendor import UserContext
from bonuspro.adapters.moysklad.remap import EntityMissing


class PersonalDiscountAPITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.customer=(str(uuid4()) for _ in range(3))
        self.archived=False;self.provider_reads=[]
        secret=Secrets(str(uuid4()),"personal.synthetic","synthetic-"+"x"*40,b"k"*32)
        self.repo=Installations(os.environ["TEST_APP_DSN"],os.environ["TEST_LIFECYCLE_DSN"],secret)
        self.repo.change(self.tenant,dict(iat=int(time.time())-10,exp=int(time.time())+60,jti=str(uuid4())),
                         "Install","synthetic-remap")
        outer=self
        class Provider:
            def __init__(self,token,tenant):
                assert token=="synthetic-remap" and tenant==outer.tenant
            def entity(self,kind,identity):
                outer.provider_reads.append((kind,identity))
                if kind!="counterparty" or identity!=outer.customer:
                    raise EntityMissing("missing")
                return dict(name="Контрольный клиент",phone="",archived=outer.archived)
            def counterparties(self,q,offset):
                return dict(items=[dict(id=outer.customer,name="Контрольный клиент",phone="",
                                        archived=outer.archived)],next_offset=None)
        config=Settings("https://testserver",secret,self.repo.ledger.dsn,self.repo.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=self.repo,remap_factory=Provider),
                               base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.headers=self.auth()
        self.policy="/api/admin/personal-discounts/policy"
        self.assignment="/api/admin/customers/"+self.customer+"/personal-discount"

    def auth(self,role="admin"):
        token=self.repo.create_session(UserContext(self.tenant,self.actor,role))
        return {"Authorization":"Bearer "+token,"Origin":"https://testserver"}

    def policy_body(self,**patch):
        return dict(enabled=True,external_policy="skip",allow_bonus_stack=False,
                    expected_version=0,request_id=str(uuid4()),reason="Согласованные условия",**patch)

    def assignment_body(self,rate=1000,version=0):
        return dict(rate_bps=rate,expected_version=version,request_id=str(uuid4()),
                    reason="Индивидуальные условия клиента")

    def counts(self):
        with self.repo.ledger.transaction(self.tenant) as conn:
            return tuple(conn.execute(
                "SELECT (SELECT count(*) FROM bonuspro.wallets) AS wallets,"
                "(SELECT count(*) FROM bonuspro.ledger_transactions) AS transactions,"
                "(SELECT count(*) FROM bonuspro.reservations) AS reservations,"
                "(SELECT count(*) FROM bonuspro.customer_personal_discount_versions) AS assignments"
            ).fetchone().values())

    def test_defaults_are_disabled_and_reading_customer_does_not_enrol(self):
        response=self.client.get(self.policy,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["current"],dict(version=0,enabled=False,
                         external_policy="skip",allow_bonus_stack=False))
        customer=self.client.get("/api/admin/customers/"+self.customer,headers=self.headers).json()
        personal=customer["personal_discount"]
        self.assertEqual((personal["version"],personal["rate_bps"],personal["effective_rate_bps"]),
                         (0,None,0))
        self.assertFalse(customer["enrolled"])
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_policy_and_assignment_have_exact_idempotent_versions_and_audit(self):
        policy=self.policy_body()
        first=self.client.put(self.policy,headers=self.headers,json=policy)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),self.client.put(self.policy,headers=self.headers,json=policy).json())
        body=self.assignment_body()
        assigned=self.client.put(self.assignment,headers=self.headers,json=body)
        self.assertEqual(assigned.status_code,200,assigned.text)
        self.assertEqual(assigned.json()["current"]["actor_id"],self.actor)
        changed=self.client.put(self.assignment,headers=self.headers,json=self.assignment_body(1500,1))
        self.assertEqual(changed.status_code,200,changed.text)
        # Retrying the older command returns its own recorded version.
        replay=self.client.put(self.assignment,headers=self.headers,json=body)
        self.assertEqual(replay.json(),assigned.json())
        customer=self.client.get("/api/admin/customers/"+self.customer,headers=self.headers).json()
        self.assertEqual((customer["personal_discount"]["version"],
                          customer["personal_discount"]["effective_rate_bps"]),(2,1500))
        directory=self.client.get("/api/admin/customers",headers=self.headers).json()
        self.assertEqual(directory["items"][0]["personal_discount"]["rate_bps"],1500)
        history=self.client.get(self.assignment+"/history",headers=self.headers).json()["items"]
        self.assertEqual([row["rate_bps"] for row in history],[1500,1000])
        self.assertEqual(self.counts(),(0,0,0,2))
        self.assertEqual(self.repo.state(self.tenant)["state"],"SettingsRequired")

    def test_explicit_zero_and_reset_are_distinct_and_policy_disable_keeps_assignment(self):
        self.client.put(self.policy,headers=self.headers,json=self.policy_body())
        self.client.put(self.assignment,headers=self.headers,json=self.assignment_body())
        zero=self.client.put(self.assignment,headers=self.headers,json=self.assignment_body(0,1))
        self.assertEqual(zero.status_code,200,zero.text)
        self.assertEqual(zero.json()["current"]["rate_bps"],0)
        reset=self.client.put(self.assignment,headers=self.headers,json=self.assignment_body(None,2))
        self.assertEqual(reset.status_code,200,reset.text)
        self.assertIsNone(reset.json()["current"]["rate_bps"])
        self.client.put(self.assignment,headers=self.headers,json=self.assignment_body(1234,3))
        disabled={**self.policy_body(),"enabled":False,"expected_version":1}
        self.assertEqual(self.client.put(self.policy,headers=self.headers,json=disabled).status_code,200)
        item=self.client.get("/api/admin/customers/"+self.customer,headers=self.headers).json()["personal_discount"]
        self.assertEqual((item["rate_bps"],item["effective_rate_bps"]),(1234,0))
        self.assertFalse(item["enabled"])

    def test_stale_or_changed_commands_cannot_overwrite(self):
        policy=self.policy_body()
        self.client.put(self.policy,headers=self.headers,json=policy)
        self.assertEqual(self.client.put(self.policy,headers=self.headers,json=self.policy_body()).status_code,409)
        self.assertEqual(self.client.put(self.policy,headers=self.headers,
                         json={**policy,"allow_bonus_stack":True}).status_code,409)
        body=self.assignment_body()
        self.client.put(self.assignment,headers=self.headers,json=body)
        for changed in (self.assignment_body(2000),{**body,"rate_bps":2000}):
            self.assertEqual(self.client.put(self.assignment,headers=self.headers,json=changed).status_code,409)
        self.assertEqual(self.counts(),(0,0,0,1))

    def test_anonymous_staff_wrong_origin_and_revoked_sessions_cannot_write(self):
        reads=len(self.provider_reads)
        for headers,status in (({},401),(self.auth("worker"),403),
                               ({**self.headers,"Origin":"https://evil.test"},403)):
            self.assertEqual(self.client.put(self.policy,headers=headers,json=self.policy_body()).status_code,status)
            self.assertEqual(self.client.put(self.assignment,headers=headers,json=self.assignment_body()).status_code,status)
        self.assertEqual(len(self.provider_reads),reads)
        self.repo.logout(self.headers["Authorization"].split(" ",1)[1])
        self.assertEqual(self.client.put(self.assignment,headers=self.headers,
                                        json=self.assignment_body()).status_code,401)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_archived_or_missing_provider_customer_cannot_get_assignment(self):
        self.archived=True
        rejected=self.client.put(self.assignment,headers=self.headers,json=self.assignment_body())
        self.assertEqual(rejected.status_code,409,rejected.text)
        self.assertEqual(rejected.json(),{"error":"customer_archived"})
        self.assertEqual(self.client.put("/api/admin/customers/"+str(uuid4())+"/personal-discount",
                         headers=self.headers,json=self.assignment_body()).status_code,404)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_strict_input_cannot_supply_identity_or_non_integer_rates(self):
        for patch in (dict(rate_bps=True),dict(rate_bps="1000"),dict(rate_bps=1.5),
                      dict(rate_bps=-1),dict(rate_bps=10001),dict(reason=" "),
                      dict(actor_id=str(uuid4())),dict(tenant_id=str(uuid4()))):
            response=self.client.put(self.assignment,headers=self.headers,
                                     json={**self.assignment_body(),**patch})
            self.assertEqual(response.status_code,422,(patch,response.text))
        for patch in (dict(enabled=1),dict(allow_bonus_stack="true"),
                      dict(external_policy="unknown"),dict(expected_version=True)):
            response=self.client.put(self.policy,headers=self.headers,
                                     json={**self.policy_body(),**patch})
            self.assertEqual(response.status_code,422,(patch,response.text))
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_policy_history_is_bounded_and_rejects_bad_cursor(self):
        self.client.put(self.policy,headers=self.headers,json=self.policy_body())
        history=self.client.get(self.policy+"/history",headers=self.headers)
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual(len(history.json()["items"]),1)
        for cursor in ("0","-1","2147483648","garbage"):
            self.assertEqual(self.client.get(self.policy+"/history",params={"before":cursor},
                                            headers=self.headers).status_code,422)
            self.assertEqual(self.client.get(self.assignment+"/history",params={"before":cursor},
                                            headers=self.headers).status_code,422)


if __name__=="__main__":
    unittest.main()
