import json
import unittest
from decimal import Decimal
from uuid import uuid4
import httpx

from bonuspro.adapters.moysklad.remap import (
    BASE,RemapClient,RetryLater,AccessDenied,EntityMissing,InvalidDocument,exact_minor,reference,
)


class RemapTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.order=str(uuid4()),str(uuid4())
        self.calls=[]
        self.document={"id":self.order,"accountId":self.tenant,"meta":{"type":"customerorder"},
                       "applicable":True,"sum":1000000,"payedSum":1000000,"shippedSum":1000000,
                       "updated":"2026-10-13 10:00:00.000"}

    def client(self,handler):
        def request(req):
            self.calls.append(req)
            return handler(req)
        return RemapClient("synthetic-token",self.tenant,transport=httpx.MockTransport(request))

    def test_fixed_destination_region_and_decimal_money(self):
        def response(req):
            payload=json.dumps(self.document).replace('"sum": 1000000','"sum": 1000000.0')
            return httpx.Response(200,text=payload,headers={"Content-Type":"application/json"})
        document=self.client(response).entity("customerorder",self.order)
        request=self.calls[0]
        self.assertEqual(str(request.url),BASE+"/entity/customerorder/"+self.order)
        self.assertEqual(request.headers["Accept-Encoding"],"gzip")
        self.assertEqual(request.headers["X-Lognex-Accept-Region"],"kz")
        self.assertIsInstance(document["sum"],Decimal)
        self.assertEqual(exact_minor(document["sum"]),1000000)

    def test_money_never_rounds_or_coerces_strings_bool_float(self):
        for value in (True,1.2,"100",Decimal("0.1"),Decimal("NaN"),Decimal("Infinity"),-1):
            with self.assertRaises(InvalidDocument):
                exact_minor(value)

    def test_account_or_document_identity_mismatch_is_rejected(self):
        for key in ("accountId","id"):
            body={**self.document,key:str(uuid4())}
            with self.assertRaises(InvalidDocument):
                self.client(lambda req:httpx.Response(200,json=body)).entity("customerorder",self.order)

    def test_untrusted_reference_and_redirect_never_receive_credentials(self):
        for href in ("http://127.0.0.1/"+self.order,BASE+"/entity/customerorder/"+self.order+"?x=1",
                     BASE.replace("api.moysklad.ru","evil.example")+"/entity/customerorder/"+self.order):
            with self.assertRaises(InvalidDocument):
                reference({"type":"customerorder","href":href},{"customerorder"})
        self.assertEqual(self.calls,[])
        with self.assertRaises(InvalidDocument):
            self.client(lambda req:httpx.Response(302,headers={"Location":"https://evil.example"})).entity("customerorder",self.order)
        self.assertEqual(len(self.calls),1)

    def test_rate_limit_respects_millisecond_retry_header_without_sleep_loop(self):
        with self.assertRaises(RetryLater) as error:
            self.client(lambda req:httpx.Response(429,headers={"X-Lognex-Retry-After":"2501","Retry-After":"2"})).entity("customerorder",self.order)
        self.assertEqual(error.exception.delay_seconds,3)
        self.assertEqual(len(self.calls),1)

    def test_auth_missing_and_provider_errors_are_distinct_and_redacted(self):
        for status,expected in ((401,AccessDenied),(403,AccessDenied),(404,EntityMissing),(503,RetryLater)):
            with self.assertRaises(expected) as error:
                self.client(lambda req:httpx.Response(status,text="synthetic-token")).entity("customerorder",self.order)
            self.assertNotIn("synthetic-token",str(error.exception))

    def test_denials_keep_only_bounded_numeric_codes_without_inventing_a_permission_cause(self):
        for status,code in ((401,1056),(403,1016),(403,1070),(403,47000)):
            with self.subTest(status=status,code=code):
                body={"errors":[{"code":code,"error":"synthetic-token","moreInfo":"https://private.test/secret"},
                                {"code":code},{"code":True},{"code":str(code)},{"code":-1},{"code":1000000}]}
                with self.assertRaises(AccessDenied) as caught:
                    self.client(lambda req:httpx.Response(status,json=body)).entity("customerorder",self.order)
                self.assertEqual(caught.exception.status_code,status)
                self.assertEqual(caught.exception.provider_error_codes,(code,))
                self.assertNotIn("synthetic-token",str(caught.exception.__dict__))
                self.assertNotIn("private.test",str(caught.exception.__dict__))
                self.assertNotIn(str(code),str(caught.exception))
        values=[{"code":code} for code in range(1020,1009,-1)]
        with self.assertRaises(AccessDenied) as caught:
            self.client(lambda req:httpx.Response(403,json={"errors":values})).entity("customerorder",self.order)
        self.assertEqual(caught.exception.provider_error_codes,tuple(range(1010,1018)))

    def test_denial_diagnostics_fail_closed_for_malformed_oversized_or_non_json_bodies(self):
        bodies=[("application/json",'{"errors":[{"code":1070}],"padding":"'+("x"*65536)+'"}'),
                ("application/json",'{"errors":[{"code":1070,"code":1016}]}'),
                ("application/json",'{"errors":[{"code":NaN}]}'),
                ("application/json",'{"errors":'),
                ("application/json",json.dumps({"errors":[{"code":1070}]*65})),
                ("text/html",'<script>synthetic-token</script>')]
        for content_type,body in bodies:
            with self.subTest(content_type=content_type,length=len(body)),self.assertRaises(AccessDenied) as caught:
                self.client(lambda req:httpx.Response(403,text=body,headers={"Content-Type":content_type})).entity("customerorder",self.order)
            self.assertEqual(caught.exception.provider_error_codes,())
            self.assertEqual(str(caught.exception),"MoySklad access denied")

    def test_interrupted_denial_body_keeps_access_denied_instead_of_becoming_retryable(self):
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"errors":['
                raise httpx.ReadError("synthetic-token")
        with self.assertRaises(AccessDenied) as caught:
            self.client(lambda req:httpx.Response(403,stream=Interrupted(),headers={"Content-Type":"application/json"})).entity("customerorder",self.order)
        self.assertEqual(caught.exception.provider_error_codes,())
        self.assertNotIn("synthetic-token",str(caught.exception))

    def test_timeout_is_retryable_without_exposing_request(self):
        def timeout(req):
            raise httpx.ReadTimeout("synthetic-token",request=req)
        with self.assertRaises(RetryLater) as error:
            self.client(timeout).entity("customerorder",self.order)
        self.assertEqual(error.exception.code,"provider_timeout")
        self.assertNotIn("synthetic-token",str(error.exception))

    def test_invalid_json_duplicate_keys_nan_and_html_are_rejected(self):
        for text in ('{"id":1,"id":2}','{"sum":NaN}',"[1]",'{"broken":'):
            with self.assertRaises(InvalidDocument):
                self.client(lambda req:httpx.Response(200,text=text,headers={"Content-Type":"application/json"})).entity("customerorder",self.order)
        with self.assertRaises(InvalidDocument):
            self.client(lambda req:httpx.Response(200,text="<html>",headers={"Content-Type":"text/html"})).entity("customerorder",self.order)

    def test_position_pagination_ignores_remote_next_href(self):
        rows=[{"id":str(uuid4()),"quantity":1,"price":100} for _ in range(1001)]
        def response(req):
            offset=int(req.url.params["offset"])
            return httpx.Response(200,json={"meta":{"size":1001,"nextHref":"http://127.0.0.1/secret"},
                                           "rows":rows[offset:offset+1000]})
        result=self.client(response).positions("customerorder",self.order)
        self.assertEqual(result,rows)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(r.url.host=="api.moysklad.ru" for r in self.calls))

    def test_incomplete_or_duplicate_positions_do_not_authorize_partial_snapshot(self):
        duplicate={"id":str(uuid4())}
        for rows,size in (([],1),([duplicate,duplicate],2)):
            with self.assertRaises(RetryLater):
                self.client(lambda req:httpx.Response(200,json={"meta":{"size":size},"rows":rows})).positions("customerorder",self.order)

    def test_order_changed_during_read_is_retried(self):
        count=0
        def response(req):
            nonlocal count
            if req.url.path.endswith("/positions"):
                return httpx.Response(200,json={"meta":{"size":0},"rows":[]})
            count+=1
            return httpx.Response(200,json={**self.document,"payedSum":count})
        with self.assertRaises(RetryLater):
            self.client(response).order_evidence(self.order)

    def test_order_evidence_is_read_only_and_has_no_earning_decision(self):
        def response(req):
            return httpx.Response(200,json={"meta":{"size":0},"rows":[]} if req.url.path.endswith("/positions") else self.document)
        evidence=self.client(response).order_evidence(self.order)
        self.assertEqual(evidence.order["id"],self.order)
        self.assertFalse(hasattr(evidence,"eligible_for_earning"))
        self.assertTrue(all(r.method=="GET" for r in self.calls))


    def test_missing_entity_is_typed_from_the_fixed_requested_resource_only(self):
        client=self.client(lambda req:httpx.Response(404,text="private-provider-token-document"))
        for kind in ("customerorder","paymentin"):
            with self.subTest(kind=kind),self.assertRaises(EntityMissing) as missing:
                client.entity(kind,self.order)
            self.assertEqual(missing.exception.requested_kind,kind)
            self.assertEqual(missing.exception.requested_id,self.order)
            self.assertNotIn(self.order,str(missing.exception))
            self.assertNotIn("private-provider-token-document",str(missing.exception))
        # A positions collection 404 cannot prove the main order was deleted.
        with self.assertRaises(EntityMissing) as missing:
            client.positions("customerorder",self.order)
        self.assertIsNone(missing.exception.requested_kind)
        self.assertIsNone(missing.exception.requested_id)

class CurrencyContractTests(unittest.TestCase):
    def client(self,body):
        return RemapClient("synthetic",self.tenant,
            transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
    def setUp(self):
        self.tenant,self.identity=str(uuid4()),str(uuid4())
        self.currency=dict(id=self.identity,meta=dict(type="currency",
            href=BASE+"/entity/currency/"+self.identity),isoCode="KZT",code="398",
            default=True,rate=1,multiplicity=1,indirect=False,system=True)
    def test_documented_currency_without_account_id_is_accepted(self):
        self.assertEqual(self.client(self.currency).entity("currency",self.identity),self.currency)
    def test_currency_optional_account_identity_is_still_checked(self):
        self.assertEqual(self.client({**self.currency,"accountId":self.tenant}).entity(
            "currency",self.identity)["accountId"],self.tenant)
        for value in (str(uuid4()),None,"invalid"):
            with self.subTest(value=value),self.assertRaises(InvalidDocument):
                self.client({**self.currency,"accountId":value}).entity("currency",self.identity)
    def test_currency_exception_does_not_relax_other_entity_identity_checks(self):
        for kind in ("customerorder","counterparty","organization","demand"):
            body={**self.currency,"meta":{"type":kind}}
            with self.subTest(kind=kind),self.assertRaises(InvalidDocument):
                self.client(body).entity(kind,self.identity)
        for patch in ({"id":str(uuid4())},{"meta":{"type":"counterparty"}}):
            with self.assertRaises(InvalidDocument):
                self.client({**self.currency,**patch}).entity("currency",self.identity)
