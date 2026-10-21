"""Exercise REMAP against a provider that rejects an unsupported Accept header."""
import unittest
from uuid import uuid4
import httpx
from bonuspro.adapters.moysklad.remap import BASE, RemapClient

class RemapAcceptContractTests(unittest.TestCase):
    def setUp(self):
        self.tenant=str(uuid4())
        self.marker=str(uuid4())
        self.path="/entity/customerorder/metadata/attributes"
        self.calls=[]
        self.exists=False
        self.row=dict(id=self.marker,name="БонусПро: операция списания",type="string",required=False,
            meta=dict(type="attributemetadata",href=BASE+self.path+"/"+self.marker))

    def transport(self,request):
        self.calls.append(request)
        if request.headers.get("accept")!="application/json;charset=utf-8":
            return httpx.Response(400,json={"errors":[{"code":1062}]})
        if request.url.path.endswith(self.path):
            if request.method=="POST":
                self.exists=True
                return httpx.Response(200,json=[self.row])
            return httpx.Response(200,json={
                "meta":{"href":BASE+self.path},
                "rows":[self.row] if self.exists else []})
        return httpx.Response(200,json={"ok":True})

    def client(self):
        return RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(self.transport))

    def test_document_customer_and_position_reads_use_utf8_accept(self):
        client=self.client()
        for path in ("/entity/customerorder","/entity/customerorder/"+str(uuid4()),
                     "/entity/customerorder/"+str(uuid4())+"/positions",
                     "/entity/counterparty","/entity/demand","/entity/salesreturn"):
            with self.subTest(path=path):
                self.assertEqual(client._get(path),{"ok":True})
        self.assertEqual(len(self.calls),6)

    def test_marker_creation_and_repeat_work_with_strict_provider(self):
        client=self.client()
        self.assertEqual(client.marker_attribute(create=True),self.marker)
        self.assertEqual(client.marker_attribute(create=True),self.marker)
        self.assertEqual([request.method for request in self.calls],["GET","POST","GET"])
        self.assertTrue(all(request.headers["accept"]=="application/json;charset=utf-8"
                            for request in self.calls))

    def test_fixture_reproduces_observed_1062_for_old_header(self):
        response=self.transport(httpx.Request("GET",BASE+self.path,
                                             headers={"Accept":"application/json"}))
        self.assertEqual(response.status_code,400)
        self.assertEqual(response.json()["errors"][0]["code"],1062)
