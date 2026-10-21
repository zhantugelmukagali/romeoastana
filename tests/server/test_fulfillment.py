import copy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import uuid4
import unittest
import httpx
from bonuspro.adapters.moysklad.remap import OrderEvidence,InvalidDocument,RetryLater,RemapClient,BASE
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader,moment

def ref(kind,identity=None):
    return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+(identity or str(uuid4()))}}

class Fixture:
    def __init__(self):
        self.tenant,self.order,self.demand,self.payment=(str(uuid4()) for _ in range(4))
        self.doc=dict(id=self.order,accountId=self.tenant,applicable=True,created="2026-10-14 10:00:00.000",
            updated="2026-10-14 11:00:00.000",sum=1200000,payedSum=0,shippedSum=0,
            agent=ref("counterparty"),organization=ref("organization"),rate={"currency":ref("currency"),"value":1},
            vatIncluded=True,vatEnabled=False,name="001")
        self.lines=[dict(id=str(uuid4()),assortment=ref("product"),price=1000000,quantity=1,discount=0,vat=0),
                    dict(id=str(uuid4()),assortment=ref("service"),price=200000,quantity=1,discount=0,vat=0)]
        self.ship={**copy.deepcopy(self.doc),"id":self.demand,"customerOrder":ref("customerorder",self.order),
                   "returns":[]}
        self.ship_lines=copy.deepcopy(self.lines)
        for line in self.ship_lines:line["id"]=str(uuid4())
        self.pay={**copy.deepcopy(self.doc),"id":self.payment,"meta":{"type":"paymentin"},
                  "operations":[{**ref("customerorder",self.order),"linkedSum":1200000}]}
        self.returns=[];self.return_lines={}
        self.include_ship=True;self.include_pay=True;self.on_demand=False;self.reads=0;self.mutate=False
    def order_evidence(self,identity):
        self.reads+=1
        if self.mutate and self.reads==2:self.pay["sum"]+=1
        return OrderEvidence(copy.deepcopy(self.doc),tuple(copy.deepcopy(self.lines)),
            (copy.deepcopy(self.pay),) if self.include_pay else (),
            (copy.deepcopy(self.ship),) if self.include_ship else ())
    def positions(self,kind,identity):
        return copy.deepcopy(self.return_lines[identity] if kind=="salesreturn" else self.ship_lines)
    def linked(self,doc,field,allowed):
        if field=="returns":return copy.deepcopy(self.returns)
        return [copy.deepcopy(self.pay)] if self.on_demand else []
    def return_goods(self,quantity=1):
        identity=str(uuid4())
        doc={**copy.deepcopy(self.ship),"id":identity,"demand":ref("demand",self.demand),"sum":int(1000000*quantity)}
        lines=[{**copy.deepcopy(self.ship_lines[0]),"id":str(uuid4()),"quantity":quantity}]
        self.returns.append(doc);self.return_lines[identity]=lines
        return doc
    def entity(self,*args):return {"isoCode":"KZT"}
    def read(self):return FulfillmentReader(self).read(self.order)

class FulfillmentTests(unittest.TestCase):
    def setUp(self):self.fx=Fixture()
    def test_same_kzt_documents_keep_minor_units_with_different_base_conversion_rates(self):
        self.fx.doc["rate"]["value"]=Decimal("0.2")
        self.fx.ship["rate"]["value"]=Decimal("0.25")
        self.fx.pay["rate"]["value"]=Decimal("0.3")
        proof=self.fx.read()
        self.assertEqual((proof.saved.snapshot.total_minor,proof.paid_minor,proof.shipped_minor),
                         (1200000,1200000,1200000))
        self.assertEqual(proof.outcome,"eligible")
        self.fx.pay["operations"][0]["linkedSum"]=1199999
        self.assertEqual(self.fx.read().outcome,"waiting_payment")
        self.fx.pay["operations"][0]["linkedSum"]=1200000
        returned=self.fx.return_goods()
        returned["rate"]["value"]=Decimal("0.4")
        proof=self.fx.read()
        self.assertEqual(proof.outcome,"return_review")
        self.assertEqual((proof.paid_minor,proof.shipped_minor),(1200000,1200000))
        self.assertEqual(sum(line.amount for line in proof.returned),1000000)

    def test_complete_sale_uses_allocations_and_goods_only(self):
        p=self.fx.read()
        self.assertEqual(p.outcome,"eligible")
        self.assertEqual((p.paid_minor,p.shipped_minor,p.saved.snapshot.eligible_minor),(1200000,1200000,1000000))
        self.assertEqual(p.created_at,datetime(2026,10,14,7,tzinfo=timezone.utc))
    def test_summary_paid_and_shipped_do_not_authorize_earning(self):
        self.fx.doc.update(payedSum=1200000,shippedSum=1200000)
        self.fx.include_pay=False
        self.assertEqual(self.fx.read().outcome,"waiting_payment")
        self.fx.include_ship=False
        self.assertEqual(self.fx.read().outcome,"waiting_shipment")
    def test_unposted_and_partial_payment_not_counted(self):
        self.fx.pay["applicable"]=False
        self.assertEqual(self.fx.read().outcome,"waiting_payment")
        self.fx.pay["applicable"]=True
        self.fx.pay["operations"][0]["linkedSum"]=600000
        self.assertEqual(self.fx.read().outcome,"waiting_payment")
    def test_demand_allocation_and_duplicate_reference_count_once(self):
        self.fx.pay["operations"]=[{**ref("demand",self.fx.demand),"linkedSum":600000}]
        self.fx.on_demand=True
        p=self.fx.read()
        self.assertEqual(p.paid_minor,600000);self.assertEqual(p.outcome,"waiting_payment")
        self.fx.pay["operations"][0]["linkedSum"]=1200000;self.fx.include_pay=False
        self.assertEqual(self.fx.read().outcome,"eligible")
    def test_other_order_and_invoice_allocations_do_not_pay_this_order(self):
        for kind in ("customerorder","invoiceout"):
            self.fx.pay["operations"]=[{**ref(kind),"linkedSum":1200000}]
            self.assertEqual(self.fx.read().outcome,"waiting_payment")
    def test_duplicate_and_excessive_allocations_rejected(self):
        original=copy.deepcopy(self.fx.pay["operations"][0])
        self.fx.pay["operations"].append(original)
        with self.assertRaises(InvalidDocument):self.fx.read()
        self.fx.pay["operations"]=[{**original,"linkedSum":1200001}]
        with self.assertRaises(InvalidDocument):self.fx.read()
    def test_foreign_customer_or_currency_rejected(self):
        for field,value in (("agent",ref("counterparty")),("rate",{"currency":ref("currency")})):
            original=self.fx.pay[field];self.fx.pay[field]=value
            with self.assertRaises(InvalidDocument):self.fx.read()
            self.fx.pay[field]=original
    def test_partial_shipment_waits_and_unrelated_goods_require_review(self):
        self.fx.ship_lines=self.fx.ship_lines[:1];self.fx.ship["sum"]=1000000
        self.assertEqual(self.fx.read().outcome,"waiting_shipment")
        self.fx.ship_lines[0]["assortment"]=ref("product")
        self.assertEqual(self.fx.read().outcome,"document_review")
    def test_equal_total_different_quantities_not_full_shipment(self):
        self.fx.ship_lines[0].update(price=500000,quantity=2)
        self.assertEqual(self.fx.read().outcome,"document_review")
    def test_unposted_shipment_and_returns_do_not_earn(self):
        self.fx.ship["applicable"]=False
        self.assertEqual(self.fx.read().outcome,"waiting_shipment")
        self.fx.ship["applicable"]=True;self.fx.return_goods()
        self.assertEqual(self.fx.read().outcome,"return_review")
    def test_partial_return_has_verified_quantities_and_undo_is_observed(self):
        from decimal import Decimal
        returned=self.fx.return_goods(Decimal("0.5"))
        proof=self.fx.read()
        self.assertEqual(proof.returned[0].quantity,Decimal("0.5"))
        self.assertTrue(proof.full_shipment and proof.full_payment)
        returned["applicable"]=False
        self.assertEqual(self.fx.read().outcome,"eligible")
    def test_excessive_return_wrong_demand_and_unposted_parent_fail(self):
        returned=self.fx.return_goods(2)
        with self.assertRaises(InvalidDocument):self.fx.read()
        self.fx.return_lines[returned["id"]][0]["quantity"]=1;returned["sum"]=1000000
        returned["demand"]=ref("demand")
        with self.assertRaises(InvalidDocument):self.fx.read()
        returned["demand"]=ref("demand",self.fx.demand);self.fx.ship["applicable"]=False
        with self.assertRaises(InvalidDocument):self.fx.read()

    def test_cancelled_and_unstable_graph_do_not_earn(self):
        self.fx.doc["applicable"]=False
        self.assertEqual(self.fx.read().outcome,"cancelled")
        self.fx.doc["applicable"]=True;self.fx.reads=0;self.fx.mutate=True
        with self.assertRaises(RetryLater):self.fx.read()
    def test_two_partial_shipments_aggregate_by_assortment_not_position_id(self):
        first=copy.deepcopy(self.fx.ship);second=copy.deepcopy(first);second["id"]=str(uuid4())
        first["sum"]=1000000;second["sum"]=200000
        f=self.fx
        f.order_evidence=lambda _:OrderEvidence(copy.deepcopy(f.doc),tuple(copy.deepcopy(f.lines)),
                                                (copy.deepcopy(f.pay),),(first,second))
        f.positions=lambda kind,id:copy.deepcopy([f.ship_lines[0] if id==first["id"] else f.ship_lines[1]])
        self.assertEqual(f.read().outcome,"eligible")

    def test_documented_incoming_allocations_validate_without_paying_other_sales(self):
        for payment_kind in ("paymentin","cashin"):
            for unrelated_kind in ("purchasereturn","commissionreportin"):
                with self.subTest(payment=payment_kind,allocation=unrelated_kind):
                    f=Fixture()
                    f.pay["meta"]["type"]=payment_kind
                    f.pay["sum"]=1600000
                    f.pay["operations"].append({**ref(unrelated_kind),"linkedSum":400000})
                    proof=f.read()
                    self.assertEqual(proof.outcome,"eligible")
                    self.assertEqual(proof.paid_minor,1200000)
                    # The payment total cannot stand in for this order's allocation.
                    f.pay["operations"][0]["linkedSum"]=600000
                    f.pay["operations"][1]["linkedSum"]=1000000
                    proof=f.read()
                    self.assertEqual(proof.outcome,"waiting_payment")
                    self.assertEqual(proof.paid_minor,600000)
                    f.pay["operations"][1]["linkedSum"]+=1
                    with self.assertRaises(InvalidDocument):f.read()

    def test_unposted_demand_allocation_still_blocks_cancellation_with_mixed_payment(self):
        for payment_kind in ("paymentin","cashin"):
            for unrelated_kind in ("purchasereturn","commissionreportin"):
                with self.subTest(payment=payment_kind,allocation=unrelated_kind):
                    f=Fixture()
                    f.doc["applicable"]=False
                    f.ship["applicable"]=False
                    f.pay["meta"]["type"]=payment_kind
                    f.pay["sum"]=1600000
                    f.pay["operations"]=[{**ref("demand",f.demand),"linkedSum":1200000},
                                         {**ref(unrelated_kind),"linkedSum":400000}]
                    proof=f.read()
                    self.assertEqual(proof.paid_minor,0)
                    self.assertFalse(proof.cancel_safe)

    def test_undocumented_incoming_allocation_types_stay_rejected(self):
        for payment_kind in ("paymentin","cashin"):
            for unrelated_kind in ("commissionreportout","prepayment"):
                with self.subTest(payment=payment_kind,allocation=unrelated_kind):
                    f=Fixture()
                    f.pay["meta"]["type"]=payment_kind
                    f.pay["sum"]=1600000
                    f.pay["operations"].append({**ref(unrelated_kind),"linkedSum":400000})
                    with self.assertRaises(InvalidDocument):f.read()

    def test_disabled_vat_flags_may_differ_between_order_shipment_and_return(self):
        from decimal import Decimal
        for order_flag,shipment_flag,return_flag in ((False,None,True),(True,False,None),(None,False,False)):
            with self.subTest(order=order_flag,shipment=shipment_flag,returned=return_flag):
                f=Fixture()
                for document,flag in ((f.doc,order_flag),(f.ship,shipment_flag)):
                    if flag is None:document.pop("vatIncluded")
                    else:document["vatIncluded"]=flag
                proof=f.read()
                self.assertEqual(proof.outcome,"eligible")
                # Inert flags stay intact in immutable redemption-plan comparisons.
                self.assertIs(proof.saved.financial()["vat_included"],True if order_flag is None else order_flag)
                before=proof.saved.financial()
                returned=f.return_goods(Decimal("0.5"))
                if return_flag is None:returned.pop("vatIncluded",None)
                else:returned["vatIncluded"]=return_flag
                proof=f.read()
                self.assertEqual(proof.outcome,"return_review")
                self.assertTrue(proof.full_payment and proof.full_shipment)
                self.assertEqual(proof.returned[0].quantity,Decimal("0.5"))
                self.assertEqual(proof.saved.financial(),before)

    def test_enabled_vat_modes_must_match_across_related_documents(self):
        f=Fixture()
        f.doc["vatEnabled"]=True
        with self.assertRaisesRegex(InvalidDocument,"shipment tax mismatch"):f.read()
        f.ship["vatEnabled"]=True
        self.assertEqual(f.read().outcome,"eligible")
        returned=f.return_goods()
        returned["vatEnabled"]=False
        with self.assertRaisesRegex(InvalidDocument,"return tax mismatch"):f.read()
        returned["vatEnabled"]=True
        returned["vatIncluded"]=False
        with self.assertRaises(InvalidDocument):f.read()

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tenant=str(uuid4())
        self.start=datetime(2026,10,14,7,tzinfo=timezone.utc);self.end=self.start+timedelta(minutes=5)
    def test_msk_filter_and_identity_verified(self):
        identity=str(uuid4())
        def handler(request):
            self.assertEqual(request.url.params["filter"],"created>=2026-10-14 10:00:00.000;created<=2026-10-14 10:05:00.000")
            return httpx.Response(200,json={"meta":{"size":1},"rows":[{
                "id":identity,"accountId":self.tenant,"meta":{"type":"customerorder"},"created":"2026-10-14 10:01:00.000"}]})
        client=RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(handler))
        self.assertEqual(client.order_ids_between(self.start,self.end),[identity])
    def test_truncated_collection_cannot_advance_discovery(self):
        client=RemapClient("synthetic",self.tenant,transport=httpx.MockTransport(
            lambda _:httpx.Response(200,json={"meta":{"size":2},"rows":[]})))
        with self.assertRaises(RetryLater):client.order_ids_between(self.start,self.end)
    def test_budget_prevents_unbounded_network_calls(self):
        client=RemapClient("synthetic",self.tenant,budget_seconds=1,transport=httpx.MockTransport(
            lambda _:self.fail("expired budget must not call provider")))
        with self.assertRaises(RetryLater):client.order_ids_between(self.start,self.end)
    def test_invalid_timestamp_rejected(self):
        for value in (None,"2026-99-99","2026-10-14T10:00:00Z"):
            with self.assertRaises(InvalidDocument):moment(value)

class CancellationSafetyTests(unittest.TestCase):
    def test_invoice_link_prevents_unpaid_cancellation_proof(self):
        f=Fixture();f.include_ship=False;f.include_pay=False;f.doc["applicable"]=False
        f.doc["invoicesOut"]=[ref("invoiceout")]
        self.assertFalse(FulfillmentReader(f).read(f.order).cancel_safe)
    def test_payment_allocated_to_unposted_demand_prevents_safe_cancel(self):
        f=Fixture();f.doc["applicable"]=False;f.ship["applicable"]=False
        f.pay["operations"]=[{**ref("demand",f.demand),"linkedSum":1200000}]
        self.assertFalse(FulfillmentReader(f).read(f.order).cancel_safe)

if __name__=="__main__":unittest.main()
