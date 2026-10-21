import json,unittest
from uuid import uuid4
import httpx
from bonuspro.adapters.moysklad.remap import RemapClient,InvalidDocument

class CustomerDirectoryTests(unittest.TestCase):
    def setUp(self):self.tenant=str(uuid4());self.customer=str(uuid4());self.calls=[]
    def client(self,rows,size=1):
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200,headers={"Content-Type":"application/json"},json={"rows":rows,"meta":{"size":size}})
        return RemapClient("synthetic-only",self.tenant,transport=httpx.MockTransport(handler))
    def row(self):return dict(id=self.customer,accountId=self.tenant,meta={"type":"counterparty"},name="Клиент",phone="+7700")
    def test_search_uses_fixed_endpoint_and_encoded_search_parameter(self):
        result=self.client([self.row()]).counterparties("+7 700",0)
        self.assertEqual(result["items"][0]["id"],self.customer)
        self.assertEqual(self.calls[0].url.host,"api.moysklad.ru")
        self.assertEqual(self.calls[0].url.params["search"],"+7 700")
        self.assertNotIn("access_token",str(self.calls[0].url))
        self.assertIsNone(result["next_offset"])
    def test_foreign_duplicate_wrong_kind_or_invalid_collection_is_rejected(self):
        for rows,size in (([{**self.row(),"accountId":str(uuid4())}],1),
                          ([self.row(),self.row()],2),
                          ([{**self.row(),"meta":{"type":"customerorder"}}],1),([self.row()],"1")):
            with self.subTest(rows=rows,size=size),self.assertRaises(InvalidDocument):
                self.client(rows,size).counterparties()
    def test_bounded_page_and_search_validation(self):
        for search,offset in (("a",0),("x"*101,0),("",-1),("",100001),("",True)):
            with self.assertRaises(InvalidDocument):self.client([]).counterparties(search,offset)
        self.assertEqual(self.calls,[])

if __name__=="__main__":unittest.main()
