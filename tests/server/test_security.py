import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import jwt
import httpx
from cryptography.exceptions import InvalidTag
from bonuspro.security import Secrets, AuthenticationFailed
from bonuspro.adapters.moysklad.vendor import VendorClient, ProviderUnavailable


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.config = Secrets(str(uuid4()),"test.vendor","synthetic-vendor-secret-"+"x"*32,b"k"*32)

    def claims(self, **overrides):
        now=int(time.time())
        return dict({"iat":now-1,"exp":now+119,"jti":str(uuid4())},**overrides)

    def test_valid_inbound_and_fresh_outbound_tokens(self):
        token=jwt.encode(self.claims(),self.config.vendor_secret,algorithm="HS256")
        self.assertIn("jti",self.config.verify_vendor(token))
        first=self.config.outbound_jwt()
        second=self.config.outbound_jwt()
        self.assertNotEqual(first,second)
        self.assertEqual(jwt.decode(first,self.config.vendor_secret,algorithms=["HS256"])["sub"],self.config.app_uid)
        with self.assertRaises(AuthenticationFailed):
            self.config.verify_vendor(first)

    def test_reject_wrong_signature_algorithm_and_type(self):
        cases=[
            jwt.encode(self.claims(),"wrong-key-"+"z"*32,algorithm="HS256"),
            jwt.encode(self.claims(),self.config.vendor_secret,algorithm="HS384"),
            jwt.encode(self.claims(),self.config.vendor_secret,algorithm="HS256",headers={"typ":"OTHER"}),
            jwt.encode(self.claims(),key=None,algorithm="none"),
        ]
        for token in cases:
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(token)


    def test_optional_type_and_case_variants_require_valid_signature(self):
        for token_type in (None,"jwt","Jwt","JWT"):
            token=jwt.encode(self.claims(),self.config.vendor_secret,algorithm="HS256",
                             headers={"typ":token_type})
            self.assertIn("jti",self.config.verify_vendor(token))
            forged=jwt.encode(self.claims(),"wrong-key-"+"z"*32,algorithm="HS256",
                              headers={"typ":token_type})
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(forged)

    def test_outbound_reflection_is_rejected_without_type_metadata(self):
        claims=jwt.decode(self.config.outbound_jwt(),self.config.vendor_secret,algorithms=["HS256"])
        token=jwt.encode(claims,self.config.vendor_secret,algorithm="HS256",headers={"typ":None})
        with self.assertRaises(AuthenticationFailed):
            self.config.verify_vendor(token)

    def test_header_reasons_are_specific_and_never_echo_header_values(self):
        cases=[
            ({"alg":"HS384","typ":"JWT"},"HS384","header_algorithm_hs384"),
            ({"alg":"HS512","typ":"JWT"},"HS512","header_algorithm_hs512"),
            ({"typ":"private-untrusted-header"},"HS256","header_type"),
            ({"typ":True},"HS256","header_type"),
            ({"crit":["private-untrusted-header"]},"HS256","header_critical"),
            ({"crit":[]},"HS256","header_critical"),
        ]
        for headers,algorithm,reason in cases:
            token=jwt.encode(self.claims(),self.config.vendor_secret,algorithm=algorithm,headers=headers)
            with self.assertLogs("bonuspro.vendor_auth",level="WARNING") as logs:
                with self.assertRaises(AuthenticationFailed):
                    self.config.verify_vendor(token)
            self.assertEqual(len(logs.output),1)
            self.assertIn("reason="+reason,logs.output[0])
            self.assertNotIn("private-untrusted-header",logs.output[0])
            self.assertNotIn(token,logs.output[0])

    def test_time_bounds_and_required_jti(self):
        now=int(time.time())
        cases=[self.claims(exp=now-1),self.claims(iat=now+10),
               self.claims(exp=now-2,iat=now-1),self.claims(jti=""),self.claims(iat=True)]
        missing=self.claims()
        del missing["jti"]
        cases.append(missing)
        for claims in cases:
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(jwt.encode(claims,self.config.vendor_secret,algorithm="HS256"))


    def test_inbound_lifetime_is_not_outbound_300_second_limit(self):
        claims=self.claims(exp=int(time.time())+3600)
        token=jwt.encode(claims,self.config.vendor_secret,algorithm="HS256")
        self.assertEqual(self.config.verify_vendor(token)["exp"],claims["exp"])
        outbound=jwt.decode(self.config.outbound_jwt(),self.config.vendor_secret,algorithms=["HS256"])
        self.assertLessEqual(outbound["exp"]-outbound["iat"],300)

    def test_fractional_numeric_dates_preserve_expiration_boundary(self):
        with patch("bonuspro.security.time.time",return_value=2000000000.5):
            claims=self.claims(iat=2000000000.25,exp=2000000000.75)
            token=jwt.encode(claims,self.config.vendor_secret,algorithm="HS256")
            actual=self.config.verify_vendor(token)
            self.assertEqual(actual["iat"],2000000000)
            self.assertEqual(actual["exp"],2000000000.75)
            with patch("bonuspro.security.time.time",return_value=2000000000.75):
                with self.assertRaises(AuthenticationFailed):
                    self.config.verify_vendor(token)

    def test_invalid_numeric_dates_and_not_before_are_rejected(self):
        now=time.time()
        for override in ({"iat":"1"},{"exp":True},{"exp":float("nan")},
                         {"exp":float("inf")},{"iat":-1},{"exp":10**400},
                         {"nbf":now+60},{"nbf":"1"},{"nbf":float("nan")},
                         {"iat":now+0.5}):
            token=jwt.encode(self.claims(**override),self.config.vendor_secret,algorithm="HS256")
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(token)

    def test_authentication_diagnostics_do_not_log_secrets_or_claims(self):
        claims=self.claims(jti="private-replay-id",arbitrary="private-payload")
        wrong=jwt.encode(claims,"wrong-key-"+"z"*32,algorithm="HS256")
        with self.assertLogs("bonuspro.vendor_auth",level="WARNING") as logs:
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(wrong)
        self.assertEqual(len(logs.output),1)
        self.assertIn("reason=signature",logs.output[0])
        for private in (wrong,self.config.vendor_secret,"private-replay-id","private-payload"):
            self.assertNotIn(private,"".join(logs.output))

    def test_expired_token_diagnostic_is_specific_and_redacted(self):
        claims=self.claims(exp=int(time.time())-1)
        token=jwt.encode(claims,self.config.vendor_secret,algorithm="HS256")
        with self.assertLogs("bonuspro.vendor_auth",level="WARNING") as logs:
            with self.assertRaises(AuthenticationFailed):
                self.config.verify_vendor(token)
        self.assertIn("reason=expired",logs.output[0])
        self.assertNotIn(token,"".join(logs.output))

    def test_credentials_are_encrypted_and_bound_to_tenant(self):
        account=str(uuid4())
        encrypted=self.config.encrypt(account,"synthetic-secret-token")
        self.assertNotIn("synthetic-secret-token",encrypted)
        self.assertEqual(self.config.decrypt(account,encrypted),"synthetic-secret-token")
        self.assertNotEqual(encrypted,self.config.encrypt(account,"synthetic-secret-token"))
        with self.assertRaises(InvalidTag):
            self.config.decrypt(str(uuid4()),encrypted)
        with self.assertRaises(ValueError):
            self.config.decrypt(account,"v2.invalid")

    def test_secret_representation_is_redacted(self):
        self.assertNotIn(self.config.vendor_secret,repr(self.config))
        self.assertNotIn("kkkk",repr(self.config))

    def test_exchange_uses_fixed_url_gzip_and_validated_identity(self):
        account,user=str(uuid4()),str(uuid4())
        def handler(request):
            self.assertEqual(str(request.url),VendorClient.URL)
            self.assertEqual(request.headers["accept-encoding"],"gzip")
            self.assertEqual(request.method,"POST")
            self.assertNotIn("one-time-context",str(request.url))
            return httpx.Response(200,json={"accountId":account,"userId":user,"role":"admin"})
        identity=VendorClient(self.config,httpx.MockTransport(handler)).exchange("one-time-context")
        self.assertEqual(identity.tenant_id,account)

    def test_redirects_and_untrusted_roles_are_rejected(self):
        for response in (httpx.Response(302,headers={"Location":"http://127.0.0.1"}),
                         httpx.Response(200,json={"accountId":str(uuid4()),"userId":str(uuid4()),"role":"root"})):
            transport=httpx.MockTransport(lambda request:response)
            with self.assertRaises((AuthenticationFailed,ProviderUnavailable)):
                VendorClient(self.config,transport).exchange("test")

    def test_provider_failure_does_not_expose_token(self):
        transport=httpx.MockTransport(lambda request:httpx.Response(500,text="synthetic-secret"))
        with self.assertRaises(ProviderUnavailable) as raised:
            VendorClient(self.config,transport).exchange("synthetic-secret")
        self.assertNotIn("synthetic-secret",str(raised.exception))


if __name__=="__main__":
    unittest.main()
