import unittest
from types import SimpleNamespace
from uuid import uuid4
from bonuspro.releasecheck import recent_orders,failure_code
from bonuspro.adapters.moysklad.remap import BASE,InvalidDocument,RetryLater,AccessDenied

class ReleaseCheckTests(unittest.TestCase):
    def test_recent_sample_is_bounded_owned_and_never_follows_provider_links(self):
        tenant,order=str(uuid4()),str(uuid4())
        calls=[]
        row=dict(id=order,accountId=tenant,meta=dict(type="customerorder",href=BASE+"/entity/customerorder/"+order))
        def get(path,params):
            calls.append((path,params));return dict(meta={"size":1},rows=[row])
        client=SimpleNamespace(tenant=tenant,_get=get)
        self.assertEqual(recent_orders(client,3),[order])
        self.assertEqual(calls,[("/entity/customerorder",{"limit":3,"offset":0,"order":"updated,desc"})])
        for patch in ({"accountId":str(uuid4())},{"meta":{"type":"customerorder","href":"http://127.0.0.1/private"}}):
            original=dict(row);row.update(patch)
            with self.assertRaises(InvalidDocument):recent_orders(client,3)
            row.clear();row.update(original)
        def truncated(path,params):return dict(meta={"size":100},rows=[row])
        client._get=truncated
        with self.assertRaises(InvalidDocument):recent_orders(client,3)
        def excessive(path,params):return dict(meta={"size":2},rows=[row,row])
        client._get=excessive
        with self.assertRaises(InvalidDocument):recent_orders(client,1)

    def test_failure_reports_never_reflect_exception_credentials_or_provider_payload(self):
        private="private-token-customer-document"
        for error,expected in (
            (RuntimeError(private),"check_unavailable"),
            (InvalidDocument(private,reason=private),"saved_order_invalid"),
            (InvalidDocument(private,reason="vat_mode_unsupported"),"vat_mode_unsupported"),
            (InvalidDocument(private,reason="currency_unsupported"),"currency_unsupported"),
            (InvalidDocument("shipment tax mismatch"),"shipment_tax_mismatch"),
            (AccessDenied(private),"provider_access_denied"),
            (RetryLater(30,"provider_unavailable"),"provider_retry_required")):
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(failure_code(error),expected)
                self.assertNotIn(private,failure_code(error))

if __name__=="__main__":unittest.main()
