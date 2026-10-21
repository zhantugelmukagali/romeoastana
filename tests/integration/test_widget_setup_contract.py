"""Real PostgreSQL endpoint plus documented REMAP HTTP responses."""
import json,unittest
from uuid import uuid4
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import httpx
from fastapi.testclient import TestClient
from bonuspro.api.app import create_app
from bonuspro.settings import Settings
from bonuspro.adapters.moysklad.remap import RemapClient,BASE
from bonuspro.adapters.postgres.redemptions import Redemptions
import test_return_api as fixture

class WidgetSetupContractTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.ReturnAPITests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.rows=[];self.calls=[];self.posts=0;self.denied=None;self.lose=False;self.denial_codes=[]
        self.block=False;self.entered=Event();self.release=Event();self.echo_query=False
        self.marker=str(uuid4());path="/entity/customerorder/metadata/attributes"
        self.row=dict(id=self.marker,name="БонусПро: операция списания",type="string",required=False,
            meta=dict(href=BASE+path+"/"+self.marker,type="attributemetadata",mediaType="application/json"))
        outer=self
        def transport(request):
            self.calls.append(request)
            if self.denied:return httpx.Response(self.denied,json={"errors":[
                {"error":"private provider detail","code":code,"moreInfo":"https://private.test/token"}
                for code in self.denial_codes] or [{"error":"private provider detail"}]})
            if request.method=="GET":
                if self.block:
                    self.entered.set()
                    if not self.release.wait(5):raise httpx.ReadTimeout("held fixture")
                return httpx.Response(200,json=dict(meta=dict(href=str(request.url) if self.echo_query else BASE+path,mediaType="application/json"),rows=list(self.rows)))
            outer.posts+=1
            self.assertEqual(json.loads(request.content),[dict(name=self.row["name"],type="string",required=False)])
            self.rows=[self.row]
            if self.lose:raise httpx.ReadTimeout("lost creation response")
            return httpx.Response(200,json=[self.row])
        class Provider(RemapClient):
            def __init__(self,token,tenant):super().__init__(token,tenant,transport=httpx.MockTransport(transport))
        config=Settings("https://testserver",self.fx.repo.config,self.fx.ledger.dsn,self.fx.repo.lifecycle_dsn)
        self.client=TestClient(create_app(config,store=self.fx.repo,remap_factory=Provider),base_url="https://testserver")
        self.addCleanup(self.client.close);self.headers=self.fx.headers()
        self.path="/api/admin/widget/setup"
        self.adapter=Redemptions(self.fx.ledger)
    def setup_widget(self):return self.client.post(self.path,headers=self.headers)
    def test_first_setup_and_repeat_follow_real_metadata_contract_without_moving_money(self):
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        for _ in range(2):
            result=self.setup_widget();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.posts,1)
        self.assertEqual(self.adapter.binding(self.fx.tenant),self.marker)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),before)
    def test_lost_creation_response_is_recovered_without_duplicate_creation(self):
        self.lose=True
        response=self.setup_widget();self.assertEqual(response.status_code,503,response.text)
        self.assertIsNone(self.adapter.binding(self.fx.tenant))
        self.lose=False
        response=self.setup_widget();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.posts,1)
    def test_permissions_and_token_failures_are_distinct_and_do_not_echo_provider_details(self):
        for status,code in ((401,"moysklad_token_rejected"),(403,"moysklad_permissions_required")):
            self.denied=status;response=self.setup_widget()
            self.assertEqual(response.status_code,403,response.text)
            self.assertEqual(response.json()["error"],code)
            self.assertNotIn("private",response.text)
        self.assertIsNone(self.adapter.binding(self.fx.tenant))
    def test_same_http_denial_preserves_distinct_api_codes_without_echoing_messages_or_changing_money(self):
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        for status,code in ((401,1056),(403,1016),(403,1070),(403,47000)):
            self.denied=status;self.denial_codes=[code,code,True,"synthetic-token",-1,1000000]
            response=self.setup_widget()
            self.assertEqual(response.status_code,403,response.text)
            self.assertEqual(response.json(),{
                "error":"moysklad_token_rejected" if status==401 else "moysklad_permissions_required",
                "provider_error_codes":[code]})
            self.assertNotIn("private",response.text)
            self.assertNotIn("synthetic-token",response.text)
        self.assertIsNone(self.adapter.binding(self.fx.tenant))
        self.assertEqual(self.posts,0)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),before)

    def test_deleted_previously_bound_field_is_not_silently_recreated(self):
        self.assertEqual(self.setup_widget().status_code,200)
        self.rows=[]
        response=self.setup_widget()
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.posts,1)
        self.assertEqual(self.adapter.binding(self.fx.tenant),self.marker)
    def test_two_windows_cannot_create_duplicate_remote_fields(self):
        self.block=True
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(self.setup_widget)
            try:
                self.assertTrue(self.entered.wait(5))
                second=self.setup_widget()
                self.assertEqual(second.status_code,409,second.text)
                self.assertEqual(second.json()["error"],"widget_setup_in_progress")
            finally:self.release.set()
            self.assertEqual(first.result(timeout=10).status_code,200)
        self.assertEqual(self.posts,1)

    def test_anonymous_operator_and_foreign_origin_never_reach_metadata_provider(self):
        self.assertEqual(self.client.post(self.path,headers={"Origin":"https://testserver"}).status_code,401)
        self.assertEqual(self.client.post(self.path,headers=self.fx.headers("worker")).status_code,403)
        headers={**self.headers,"Origin":"https://evil.test"}
        self.assertEqual(self.client.post(self.path,headers=headers).status_code,403)
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.adapter.binding(self.fx.tenant))

    def test_request_url_echo_in_metadata_allows_setup_and_repeat_without_duplicate_fields(self):
        self.echo_query=True
        before=self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet)
        for _ in range(2):
            response=self.setup_widget()
            self.assertEqual(response.status_code,200,response.text)
        self.assertIn("limit=1000",str(self.calls[0].url))
        self.assertEqual(self.posts,1)
        self.assertEqual(self.adapter.binding(self.fx.tenant),self.marker)
        self.assertEqual(self.fx.ledger.snapshot(self.fx.tenant,self.fx.wallet),before)
