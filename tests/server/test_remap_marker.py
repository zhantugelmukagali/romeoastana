import unittest
from uuid import uuid4
import httpx
from bonuspro.adapters.moysklad.remap import RemapClient,BASE,InvalidDocument,AccessDenied

class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.marker=str(uuid4());self.tenant=str(uuid4());self.calls=[]
        self.row={"id":self.marker,"name":"БонусПро: операция списания","type":"string",
                  "meta":{"type":"attributemetadata","href":BASE+"/entity/customerorder/metadata/attributes/"+self.marker}}
    def client(self,found=True):
        def handler(request):
            self.calls.append(request)
            if request.method=="GET":return httpx.Response(200,json={"meta":{"size":int(found)},"rows":[self.row] if found else []})
            return httpx.Response(200,json=self.row)
        return RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(handler))
    def test_existing_marker_does_not_write(self):
        self.assertEqual(self.client().marker_attribute(create=True),self.marker)
        self.assertEqual([x.method for x in self.calls],["GET"])
    def test_provision_only_fixed_metadata_resource(self):
        self.assertEqual(self.client(False).marker_attribute(create=True),self.marker)
        self.assertEqual([x.method for x in self.calls],["GET","POST"])
        self.assertEqual(str(self.calls[-1].url),BASE+"/entity/customerorder/metadata/attributes")
    def test_no_generic_order_write_or_untrusted_destination(self):
        for method,path in (("POST","/entity/customerorder"),("PUT","/entity/customerorder/"+str(uuid4())),
                            ("POST","/entity/customerorder/../metadata/attributes")):
            with self.assertRaises(InvalidDocument):self.client()._request(method,path,body={})
        self.assertEqual(self.calls,[])
    def test_missing_or_malformed_metadata_does_not_authorize_widget(self):
        with self.assertRaises(InvalidDocument):self.client(False).marker_attribute()
        self.row["type"]="text"
        with self.assertRaises(InvalidDocument):self.client().marker_attribute()

if __name__=="__main__":unittest.main()

class DocumentedMarkerContractTests(unittest.TestCase):
    def setUp(self):
        self.tenant=str(uuid4());self.marker=str(uuid4());self.calls=[]
        self.path="/entity/customerorder/metadata/attributes"
        self.row=dict(id=self.marker,name="БонусПро: операция списания",type="string",required=False,
            meta=dict(type="attributemetadata",href=BASE+self.path+"/"+self.marker))
    def client(self,page,created=None):
        def transport(request):
            self.calls.append(request)
            return httpx.Response(200,json=page if request.method=="GET" else (created if created is not None else [self.row]))
        return RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(transport))
    def test_documented_full_metadata_without_size_is_not_rejected(self):
        page=dict(meta=dict(href=BASE+self.path,mediaType="application/json"),rows=[self.row])
        self.assertEqual(self.client(page).marker_attribute(create=True),self.marker)
        self.assertEqual(len(self.calls),1)
    def test_documented_array_creation_posts_only_one_optional_string_field(self):
        import json
        page=dict(meta=dict(href=BASE+self.path,mediaType="application/json"),rows=[])
        self.assertEqual(self.client(page).marker_attribute(create=True),self.marker)
        sent=json.loads(self.calls[-1].content)
        self.assertEqual(sent,[dict(name="БонусПро: операция списания",type="string",required=False)])
    def test_invalid_or_partial_collections_never_create_a_field(self):
        pages=[dict(meta={},rows=[None]),dict(meta={"size":2},rows=[]),
            dict(meta={"offset":1},rows=[]),dict(meta={"nextHref":"https://evil.test"},rows=[]),
            dict(meta={"href":"https://evil.test"},rows=[]),dict(meta={},rows=[self.row,self.row])]
        for page in pages:
            self.calls=[]
            with self.assertRaises(InvalidDocument):self.client(page).marker_attribute(create=True)
            self.assertEqual([x.method for x in self.calls],["GET"])
    def test_wrong_creation_response_and_cross_account_fields_are_rejected(self):
        page=dict(meta={},rows=[])
        for response in ([],[self.row,self.row],{"id":self.marker,"type":"text"}):
            with self.assertRaises(InvalidDocument):self.client(page,response).marker_attribute(create=True)
        foreign={**self.row,"accountId":str(uuid4())}
        with self.assertRaises(InvalidDocument):self.client(dict(meta={},rows=[foreign])).marker_attribute(create=True)
