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
