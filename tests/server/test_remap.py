import json
import unittest
from decimal import Decimal
from uuid import uuid4
import httpx

from bonuspro.adapters.moysklad.remap import (
    BASE,RemapClient,RetryLater,AccessDenied,EntityMissing,InvalidDocument,MetadataRejected,exact_minor,reference,
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
        self.assertEqual(error.exception.provider_status,429)
        self.assertEqual(error.exception.code,"provider_unavailable")
        self.assertEqual(len(self.calls),1)

    def test_retryable_http_status_is_finite_diagnostic_without_changing_worker_codes(self):
        for status in (408,409,412,500,503,599):
            with self.subTest(status=status),self.assertRaises(RetryLater) as caught:
                self.client(lambda req:httpx.Response(status,text="private-provider-token")).entity("customerorder",self.order)
            self.assertEqual(caught.exception.provider_status,status)
            self.assertEqual(caught.exception.code,"conflict" if status in (409,412) else "provider_unavailable")
            self.assertEqual(caught.exception.delay_seconds,30)
            self.assertNotIn("private-provider-token",str(caught.exception.__dict__))
        for status in (True,"429",200,600,-1):
            self.assertIsNone(RetryLater(provider_status=status).provider_status)

    def test_metadata_validation_status_is_permanent_and_keeps_fixed_read_or_create_phase(self):
        path="/entity/customerorder/metadata/attributes"
        for method in ("GET","POST"):
            for status in (400,409,412):
                with self.subTest(method=method,status=status):
                    before=len(self.calls)
                    def response(request):
                        if method=="POST" and request.method=="GET":
                            return httpx.Response(200,json={"meta":{"href":BASE+path},"rows":[]})
                        return httpx.Response(status,json={"errors":[
                            {"code":2000,"error":"private-token","moreInfo":"https://private.test/secret"}]})
                    with self.assertRaises(MetadataRejected) as caught:
                        self.client(response).marker_attribute(create=method=="POST")
                    self.assertEqual(caught.exception.provider_status,status)
                    self.assertEqual(caught.exception.provider_error_codes,(2000,))
                    self.assertEqual(caught.exception.provider_operation,
                        "metadata_read" if method=="GET" else "metadata_create")
                    self.assertEqual(len(self.calls)-before,1 if method=="GET" else 2)
                    self.assertNotIn("private",str(caught.exception.__dict__))
                    self.assertNotIn("token",str(caught.exception))

    def test_metadata_only_explicit_single_concurrent_change_code_remains_retryable(self):
        for codes in ([1065],[1065,2000],[],["1065"]):
            expected=RetryLater if codes==[1065] else MetadataRejected
            with self.subTest(codes=codes),self.assertRaises(expected) as caught:
                self.client(lambda req:httpx.Response(412,json={
                    "errors":[{"code":code,"error":"private-token"} for code in codes]
                })).marker_attribute()
            self.assertEqual(caught.exception.provider_status,412)
            self.assertEqual(caught.exception.provider_operation,"metadata_read")
            if expected is RetryLater:
                self.assertEqual(caught.exception.code,"conflict")
                self.assertEqual(caught.exception.provider_error_codes,(1065,))
            self.assertNotIn("private-token",str(caught.exception.__dict__))

    def test_filtered_concurrency_diagnostic_does_not_hide_unknown_metadata_errors(self):
        path="/entity/customerorder/metadata/attributes"
        unknown=({},{"code":"2000"},{"code":True},{"code":1000000},{"code":-1},
                 {"code":1065.0},"private-token",None)
        for method in ("GET","POST"):
            for item in unknown:
                with self.subTest(method=method,item=item):
                    def response(request):
                        if method=="POST" and request.method=="GET":
                            return httpx.Response(200,json={"meta":{"href":BASE+path},"rows":[]})
                        return httpx.Response(412,json={"errors":[{"code":1065},item]})
                    with self.assertRaises(MetadataRejected) as caught:
                        self.client(response).marker_attribute(create=method=="POST")
                    self.assertEqual(caught.exception.provider_status,412)
                    self.assertEqual(caught.exception.provider_error_codes,(1065,))
                    self.assertEqual(caught.exception.provider_operation,
                        "metadata_read" if method=="GET" else "metadata_create")
                    self.assertNotIn("private-token",str(caught.exception.__dict__))

    def test_unreadable_metadata_rejection_stays_permanent_with_empty_bounded_diagnostics(self):
        bodies=[("application/json",'{"errors":[{"code":2000}],"padding":"'+("x"*65536)+'"}'),
                ("application/json",'{"errors":[{"code":1065,"code":2000}]}'),
                ("application/json",'{"errors":[{"code":1065}],"detail":1e9999999999999999999}'),
                ("application/json",'{"errors":'),
                ("text/html",'<script>private-token</script>')]
        for content_type,body in bodies:
            with self.subTest(content_type=content_type,length=len(body)),self.assertRaises(MetadataRejected) as caught:
                self.client(lambda req:httpx.Response(412,text=body,headers={
                    "Content-Type":content_type})).marker_attribute()
            self.assertEqual(caught.exception.provider_status,412)
            self.assertEqual(caught.exception.provider_operation,"metadata_read")
            self.assertEqual(caught.exception.provider_error_codes,())
            self.assertEqual(str(caught.exception),"MoySklad rejected metadata request")

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

    def test_decimal_overflow_in_denial_body_preserves_original_access_denial(self):
        body='{"errors":[{"code":1070}],"detail":1e9999999999999999999}'
        for status in (401,403):
            with self.subTest(status=status),self.assertRaises(AccessDenied) as caught:
                self.client(lambda req:httpx.Response(status,text=body,headers={"Content-Type":"application/json"})).entity("customerorder",self.order)
            self.assertEqual(caught.exception.status_code,status)
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
        self.assertIsNone(error.exception.provider_status)
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


class RegionalHeaderContractTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.order,self.marker=(str(uuid4()) for _ in range(3))
        self.calls=[]
        self.path="/entity/customerorder/metadata/attributes"
        self.document={"id":self.order,"accountId":self.tenant,"meta":{"type":"customerorder"},
                       "applicable":True,"sum":1000000,"payedSum":1000000,"shippedSum":1000000}
        self.row={"id":self.marker,"accountId":self.tenant,"name":"БонусПро: операция списания",
                  "type":"string","required":False,"meta":{"type":"attributemetadata",
                  "href":BASE+self.path+"/"+self.marker}}

    def rejection(self,*,status=412,errors=None):
        return httpx.Response(status,json={"errors":errors if errors is not None else [
            {"code":3030,"error":"Ошибка валидации заголовка 'X-Lognex-Accept-Region': private-token"}]})

    def client(self,handler,**kwargs):
        def send(request):
            self.calls.append(request);return handler(request)
        return RemapClient("synthetic-token",self.tenant,transport=httpx.MockTransport(send),**kwargs)

    def test_worker_order_graph_reuses_the_accepted_mode_for_all_later_reads(self):
        def respond(request):
            if request.headers.get("X-Lognex-Accept-Region")=="kz":return self.rejection()
            if request.url.path.endswith("/positions"):
                return httpx.Response(200,json={"meta":{"size":0},"rows":[]})
            return httpx.Response(200,json=self.document)
        proof=self.client(respond).order_evidence(self.order)
        self.assertEqual(proof.order,self.document)
        self.assertEqual([r.method for r in self.calls],["GET"]*4)
        self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in self.calls],["kz",None,None,None])
        original=dict(self.calls[0].headers);original.pop("x-lognex-accept-region")
        self.assertEqual(dict(self.calls[1].headers),original)
        self.assertEqual(self.calls[0].url,self.calls[1].url)
        self.assertEqual(self.calls[0].content,self.calls[1].content)

    def test_marker_creation_uses_accepted_get_mode_and_never_duplicates_post(self):
        rows=[]
        def respond(request):
            if request.headers.get("X-Lognex-Accept-Region")=="kz":return self.rejection()
            if request.method=="GET":
                return httpx.Response(200,json={"meta":{},"rows":list(rows)})
            self.assertEqual(json.loads(request.content),[
                {"name":self.row["name"],"type":"string","required":False}])
            rows.append(self.row);return httpx.Response(200,json=[self.row])
        client=self.client(respond)
        self.assertEqual(client.marker_attribute(create=True),self.marker)
        self.assertEqual(client.marker_attribute(create=True),self.marker)
        self.assertEqual([r.method for r in self.calls],["GET","GET","POST","GET"])
        self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in self.calls],["kz",None,None,None])
        self.assertEqual(len(rows),1)

    def test_region_header_proof_is_not_tied_to_localized_error_template(self):
        for message in ("X-Lognex-Accept-Region",
                        "Invalid header [x-lognex-accept-region].",
                        "Ошибка заголовка «X-Lognex-Accept-Region»: требуется регион аккаунта"):
            with self.subTest(message=message):
                self.calls=[]
                def respond(request):
                    if len(self.calls)==1:return self.rejection(errors=[{"code":3030,"error":message}])
                    return httpx.Response(200,json=self.document)
                self.assertEqual(self.client(respond).entity("customerorder",self.order),self.document)
                self.assertEqual(len(self.calls),2)

    def test_ambiguous_other_or_mixed_error_evidence_never_triggers_fallback(self):
        region={"code":3030,"error":"X-Lognex-Accept-Region"}
        bad_errors=(
            [],[{}],[{"code":3030}],[{"code":"3030","error":"X-Lognex-Accept-Region"}],
            [{"code":3030.0,"error":"X-Lognex-Accept-Region"}],
            [{"code":2000,"error":"X-Lognex-Accept-Region"}],
            [{"code":3030,"error":"Accept"}],
            [{"code":3030,"error":"X-Lognex-Accept-Region Accept"}],
            [{"code":3030,"error":"X-Lognex-Accept-Region Authorization"}],
            [{"code":3030,"error":"X-Lognex-Accept-Region User-Agent"}],
            [{"code":3030,"error":"X-Lognex-Accept-Region-X"}],
            [{"code":3030,"error":"X-X-Lognex-Accept-Region"}],
            [{"code":3030,"error":"X-Lognex-Accept-Region"+("x"*4096)}],
            [region,{}],[region,{"code":2000}],[region,{"code":1065}],
            [region,{"code":3030,"error":"Content-Type"}],[region,None],[region,"private-token"],
            [region]*65,
        )
        for errors in bad_errors:
            with self.subTest(errors=errors):
                self.calls=[]
                with self.assertRaises(MetadataRejected) as caught:
                    self.client(lambda request:self.rejection(errors=errors)).marker_attribute()
                self.assertEqual(len(self.calls),1)
                self.assertEqual(self.calls[0].headers["X-Lognex-Accept-Region"],"kz")
                self.assertNotIn("private-token",str(caught.exception))
                self.assertNotIn("X-Lognex",str(caught.exception.__dict__))

    def test_only_http412_can_trigger_the_read_fallback(self):
        for status,expected in ((400,MetadataRejected),(409,MetadataRejected),(401,AccessDenied),
                                (403,AccessDenied),(429,RetryLater),(503,RetryLater)):
            with self.subTest(status=status):
                self.calls=[]
                with self.assertRaises(expected):
                    self.client(lambda request:self.rejection(status=status)).marker_attribute()
                self.assertEqual(len(self.calls),1)

    def test_region_rejected_post_is_never_retried_or_used_to_select_a_mode(self):
        def respond(request):
            if request.method=="GET":return httpx.Response(200,json={"meta":{},"rows":[]})
            return self.rejection()
        client=self.client(respond)
        with self.assertRaises(MetadataRejected) as caught:client.marker_attribute(create=True)
        self.assertEqual(caught.exception.provider_operation,"metadata_create")
        self.assertEqual([r.method for r in self.calls],["GET","POST"])
        self.assertTrue(all(r.headers.get("X-Lognex-Accept-Region")=="kz" for r in self.calls))
        self.assertFalse(client._omit_region)

    def test_failed_fallback_preserves_backoff_and_is_not_repeated_on_later_calls(self):
        def respond(request):
            if len(self.calls)==2:
                return httpx.Response(429,headers={"Retry-After":"7"})
            return self.rejection()
        client=self.client(respond)
        with self.assertRaises(RetryLater) as caught:client.marker_attribute()
        self.assertEqual(caught.exception.delay_seconds,7)
        self.assertEqual(caught.exception.provider_status,429)
        with self.assertRaises(MetadataRejected):client.marker_attribute()
        self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in self.calls],["kz",None,"kz"])

    def test_invalid_or_rejected_fallback_never_caches_omission_or_tries_again(self):
        for second in ("broken","array","html","rejected","unauthorized","server"):
            with self.subTest(second=second):
                self.calls=[]
                def respond(request):
                    if len(self.calls)!=2:return self.rejection()
                    if second=="broken":return httpx.Response(200,text="{",headers={"Content-Type":"application/json"})
                    if second=="array":return httpx.Response(200,json=[])
                    if second=="html":return httpx.Response(200,text="<html>",headers={"Content-Type":"text/html"})
                    if second=="rejected":return self.rejection()
                    if second=="unauthorized":return httpx.Response(401)
                    return httpx.Response(503)
                client=self.client(respond)
                expected=AccessDenied if second=="unauthorized" else RetryLater if second=="server" else MetadataRejected if second=="rejected" else InvalidDocument
                with self.assertRaises(expected):client.marker_attribute()
                with self.assertRaises(MetadataRejected):client.marker_attribute()
                self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in self.calls],["kz",None,"kz"])

    def test_unreadable_region_rejection_never_authorizes_another_get(self):
        bodies=(
            ("application/json",'{"errors":[{"code":3030,"error":"X-Lognex-Accept-Region"}],"padding":"'+("x"*65536)+'"}'),
            ("application/json",'{"errors":[{"code":3030,"code":3030,"error":"X-Lognex-Accept-Region"}]}'),
            ("application/json",'{"errors":[{"code":3030,"error":"X-Lognex-Accept-Region"}],"detail":1e9999999999999999999}'),
            ("application/json",'{"errors":'),
            ("text/html","X-Lognex-Accept-Region private-token"),
        )
        for content_type,body in bodies:
            with self.subTest(content_type=content_type,length=len(body)):
                self.calls=[]
                with self.assertRaises(MetadataRejected):
                    self.client(lambda request:httpx.Response(412,text=body,headers={"Content-Type":content_type})).marker_attribute()
                self.assertEqual(len(self.calls),1)

    def test_rejection_is_parsed_once_and_original_stream_closes_before_fallback(self):
        class Once(httpx.SyncByteStream):
            def __init__(self):self.reads=0;self.closed=False
            def __iter__(self):
                self.reads+=1
                if self.reads!=1:raise AssertionError("body parsed twice")
                yield b'{"errors":[{"code":3030,"error":"X-Lognex-Accept-Region"}]}'
            def close(self):self.closed=True
        stream=Once()
        def respond(request):
            if len(self.calls)==1:
                return httpx.Response(412,stream=stream,headers={"Content-Type":"application/json"})
            self.assertTrue(stream.closed)
            return httpx.Response(200,json=self.document)
        self.assertEqual(self.client(respond).entity("customerorder",self.order),self.document)
        self.assertEqual(stream.reads,1)
        self.assertTrue(stream.closed)

    def test_compatibility_error_helper_keeps_two_values_and_strict_concurrency_proof(self):
        from bonuspro.adapters.moysklad.remap import _provider_error_details,_provider_error_evidence
        self.assertEqual(_provider_error_details(httpx.Response(412,json={"errors":[{"code":1065}]})),((1065,),True))
        self.assertEqual(_provider_error_details(self.rejection()),((3030,),False))
        self.assertEqual(_provider_error_evidence(self.rejection()),((3030,),False,True))
        self.assertEqual(_provider_error_evidence(self.rejection(errors=[
            {"code":3030,"error":"X-Lognex-Accept-Region"},{}])),((3030,),False,False))

    def test_accepted_region_mode_is_isolated_between_clients_and_accounts(self):
        other_tenant,other_order=str(uuid4()),str(uuid4())
        calls=[]
        def respond(request):
            calls.append(request)
            if request.headers["Authorization"]=="Bearer first-token":
                if request.headers.get("X-Lognex-Accept-Region")=="kz":return self.rejection()
                return httpx.Response(200,json=self.document)
            return httpx.Response(200,json={**self.document,"id":other_order,"accountId":other_tenant})
        transport=httpx.MockTransport(respond)
        first=RemapClient("first-token",self.tenant,transport=transport)
        second=RemapClient("second-token",other_tenant,transport=transport)
        first.entity("customerorder",self.order)
        second.entity("customerorder",other_order)
        first.entity("customerorder",self.order)
        fresh=RemapClient("first-token",self.tenant,transport=transport)
        fresh.entity("customerorder",self.order)
        self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in calls],
                         ["kz",None,"kz",None,"kz",None])

    def test_region_fallback_preserves_pacing_and_the_original_deadline(self):
        from unittest.mock import patch
        from bonuspro.adapters.moysklad import remap
        clock=[100.0];pauses=[]
        def pause(seconds):pauses.append(seconds);clock[0]+=seconds
        def respond(request):
            return self.rejection() if len(self.calls)==1 else httpx.Response(200,json=self.document)
        with patch.object(remap.time,"monotonic",side_effect=lambda:clock[0]),patch.object(remap.time,"sleep",side_effect=pause):
            client=self.client(respond,budget_seconds=30,minimum_interval=0.5)
            client.entity("customerorder",self.order);client.entity("customerorder",self.order)
            self.assertEqual(client.deadline,130)
        self.assertEqual(pauses,[0.5,0.5])
        self.assertEqual([r.headers.get("X-Lognex-Accept-Region") for r in self.calls],["kz",None,None])

    def test_remaining_budget_can_block_fallback_without_a_second_wire_request(self):
        from unittest.mock import patch
        from bonuspro.adapters.moysklad import remap
        clock=[100.0]
        def respond(request):clock[0]=102.0;return self.rejection()
        with patch.object(remap.time,"monotonic",side_effect=lambda:clock[0]):
            client=self.client(respond,budget_seconds=12)
            with self.assertRaises(RetryLater) as caught:client.marker_attribute()
            self.assertEqual(caught.exception.code,"provider_timeout")
            self.assertEqual(client.deadline,112)
        self.assertEqual(len(self.calls),1)
