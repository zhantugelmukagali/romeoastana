import unittest,json
from uuid import uuid4
import httpx,jwt
from bonuspro.security import Secrets
from bonuspro.adapters.moysklad.vendor import VendorClient,ProviderUnavailable

class VendorActivationTests(unittest.TestCase):
    def test_exact_endpoint_signed_status_and_no_followed_redirect(self):
        config=Secrets(str(uuid4()),"bonuspro.test","s"*40,b"k"*32);tenant=str(uuid4());seen=[]
        def transport(r):
            seen.append(r)
            self.assertEqual(r.method,"PUT")
            self.assertEqual(r.headers["Accept-Encoding"],"gzip")
            self.assertEqual(r.headers["Accept"],"application/json;charset=utf-8")
            self.assertEqual(str(r.url),f"https://apps-api.moysklad.ru/api/vendor/1.0/apps/{config.app_id}/{tenant}/status")
            self.assertEqual(json.loads(r.content),{"status":"Activated"})
            token=r.headers["Authorization"].split()[1]
            self.assertEqual(jwt.decode(token,config.vendor_secret,algorithms=["HS256"])["sub"],config.app_uid)
            return httpx.Response(200)
        VendorClient(config,httpx.MockTransport(transport)).activate(tenant);self.assertEqual(len(seen),1)
        for status in (302,401,404,409,429,500):
            with self.subTest(status=status),self.assertRaises(ProviderUnavailable):
                VendorClient(config,httpx.MockTransport(lambda r:httpx.Response(status,headers={"Location":"https://evil.test"}))).activate(tenant)

if __name__=="__main__":unittest.main()

class StrictVendorTransportTests(unittest.TestCase):
    def test_activation_uses_gzip_and_a_fresh_one_use_jwt_for_each_attempt(self):
        config=Secrets(str(uuid4()),"bonuspro.test","s"*40,b"k"*32)
        tokens=[]
        def transport(request):
            if request.headers.get("Accept-Encoding")!="gzip":
                return httpx.Response(415)
            token=request.headers["Authorization"].split()[1]
            tokens.append(jwt.decode(token,config.vendor_secret,algorithms=["HS256"])["jti"])
            return httpx.Response(200)
        client=VendorClient(config,httpx.MockTransport(transport));tenant=str(uuid4())
        client.activate(tenant);client.activate(tenant)
        self.assertEqual(len(tokens),2)
        self.assertNotEqual(tokens[0],tokens[1])
