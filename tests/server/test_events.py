import json,unittest
from datetime import datetime,timezone,timedelta
from uuid import uuid4
import httpx
from bonuspro.events import signature,verify_signature,parse_events,affected_orders
from bonuspro.security import AuthenticationFailed
from bonuspro.adapters.moysklad.remap import RemapClient,BASE,InvalidDocument,RetryLater,ScanTooLarge

class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.app,self.generation,self.document=(str(uuid4()) for _ in range(4))
        self.key=b"k"*32;self.calls=[]
    def ref(self,kind,id):return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+id}}
    def event(self,**changes):
        return {**self.ref("customerorder",self.document),"accountId":self.tenant,"action":"UPDATE",**changes}
    def client(self,handler):
        def run(req):self.calls.append(req);return handler(req)
        return RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(run))
    def test_capability_is_bound_to_application_account_and_generation(self):
        tag=signature(self.key,self.app,self.tenant,self.generation)
        verify_signature(self.key,self.app,self.tenant,self.generation,tag)
        for app,tenant,generation in ((str(uuid4()),self.tenant,self.generation),(self.app,str(uuid4()),self.generation),(self.app,self.tenant,str(uuid4()))):
            with self.assertRaises(AuthenticationFailed):verify_signature(self.key,app,tenant,generation,tag)
    def test_capability_rejects_malformed_or_changed_value(self):
        for value in ("","x"*64,"0"*64,"a"*10000,None):
            with self.assertRaises(AuthenticationFailed):verify_signature(self.key,self.app,self.tenant,self.generation,value)
    def test_envelope_strips_untrusted_financial_claims(self):
        body=json.dumps({"events":[self.event(balance=100000,points=100000,updatedFields=["sum"])]})
        self.assertEqual(parse_events(body,self.tenant),[dict(kind="customerorder",id=self.document,action="UPDATE")])
    def test_envelope_rejects_foreign_account_untrusted_host_or_action(self):
        values=[self.event(accountId=str(uuid4())),self.event(action="PROCESSED"),
                self.event(meta={"type":"customerorder","href":"http://127.0.0.1/"+self.document})]
        for event in values:
            with self.assertRaises(InvalidDocument):parse_events(json.dumps({"events":[event]}),self.tenant)
    def test_envelope_rejects_duplicates_constants_and_unbounded_batches(self):
        for body in ('{"events":[]}', '{"events":[],"events":[]}', '{"events":NaN}',
                     json.dumps({"events":[self.event()]*101})):
            with self.assertRaises(InvalidDocument):parse_events(body,self.tenant)
    def test_demand_reference_cannot_recurse_or_target_another_resource(self):
        class Client:
            def entity(inner,*args):return dict(customerOrder=self.ref("demand",self.document))
        with self.assertRaises(InvalidDocument):affected_orders(Client(),"demand",self.document)
    def test_payment_resolves_order_shipment_and_return_links(self):
        order,demand,returned=(str(uuid4()) for _ in range(3))
        outer=self
        class Client:
            def entity(inner,kind,id):
                if kind=="paymentin":return {"operations":[outer.ref("customerorder",order),outer.ref("demand",demand),outer.ref("salesreturn",returned)]}
                if kind=="salesreturn":return {"demand":outer.ref("demand",demand)}
                return {"customerOrder":outer.ref("customerorder",order)}
        self.assertEqual(affected_orders(Client(),"paymentin",self.document),{order})
    def test_nested_index_is_a_hint_cache_and_does_not_skip_current_root(self):
        order,demand=str(uuid4()),str(uuid4())
        outer=self;calls=[]
        class Client:
            def entity(inner,kind,id):
                calls.append(kind)
                return {"operations":[outer.ref("demand",demand)]}
        value=affected_orders(Client(),"paymentin",self.document,known=lambda k,i:{order} if (k,i)==("demand",demand) else set())
        self.assertEqual(value,{order});self.assertEqual(calls,["paymentin"])

    def test_updated_scan_validates_timestamp_scope_and_region(self):
        start=datetime(2026,10,14,tzinfo=timezone.utc);end=start+timedelta(minutes=1)
        row={"id":self.document,"accountId":self.tenant,"meta":{"type":"paymentin"},"updated":"2026-10-14 03:00:30.000"}
        client=self.client(lambda req:httpx.Response(200,json={"meta":{"size":1},"rows":[row]}))
        self.assertEqual(client.changed_ids_between("paymentin","updated",start,end),[self.document])
        self.assertIn("updated>=",self.calls[0].url.params["filter"])
        self.assertEqual(self.calls[0].url.host,"api.moysklad.ru")
    def test_updated_scan_refuses_foreign_or_outside_window(self):
        start=datetime(2026,10,14,tzinfo=timezone.utc);end=start+timedelta(minutes=1)
        for row in ({"id":self.document,"accountId":str(uuid4()),"meta":{"type":"paymentin"},"updated":"2026-10-14 03:00:30.000"},
                    {"id":self.document,"accountId":self.tenant,"meta":{"type":"paymentin"},"updated":"2026-10-14 04:00:00.000"}):
            with self.assertRaises((InvalidDocument,RetryLater)):
                self.client(lambda req:httpx.Response(200,json={"meta":{"size":1},"rows":[row]})).changed_ids_between("paymentin","updated",start,end)
    def test_overfull_window_requests_subdivision_and_short_page_never_advances(self):
        start=datetime(2026,10,14,tzinfo=timezone.utc);end=start+timedelta(minutes=1)
        for size,rows,error in ((10001,[],ScanTooLarge),(1,[],RetryLater)):
            with self.assertRaises(error):
                self.client(lambda req:httpx.Response(200,json={"meta":{"size":size},"rows":rows})).changed_ids_between("customerorder","updated",start,end)
    def test_generic_document_write_is_still_forbidden(self):
        with self.assertRaises(InvalidDocument):
            self.client(lambda req:None)._request("PUT","/entity/customerorder/"+self.document,body={"sum":0})
        self.assertEqual(self.calls,[])
    def test_setup_retry_finds_own_webhooks_and_resume_updates_urls(self):
        rows=[];old="https://hooks.test/hooks/remap/"+self.app+"/"+self.tenant+"/"+self.generation+"/"+"a"*64
        new=old.rsplit("/",2)[0]+"/"+str(uuid4())+"/"+"b"*64
        def response(req):
            if req.method=="GET":return httpx.Response(200,json={"meta":{"size":len(rows)},"rows":rows})
            body=json.loads(req.content)
            if req.method=="POST":
                id=str(uuid4());row={**body,"id":id,"accountId":self.tenant,**self.ref("webhook",id)};rows.append(row)
            else:
                row=next(x for x in rows if x["id"]==req.url.path.rsplit("/",1)[-1]);row.update(body)
            return httpx.Response(200,json=row)
        client=self.client(response)
        self.assertEqual(client.ensure_webhooks(old),21)
        self.assertEqual(client.ensure_webhooks(old),21)
        self.assertEqual(len([x for x in self.calls if x.method=="POST"]),21)
        self.assertEqual(client.ensure_webhooks(new),21)
        self.assertEqual(len([x for x in self.calls if x.method=="PUT"]),21)
        self.assertEqual(len(rows),21)

if __name__=="__main__":unittest.main()
