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
