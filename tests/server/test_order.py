import copy
from decimal import Decimal
from dataclasses import replace
from uuid import uuid4
import unittest
from bonuspro.adapters.moysklad.remap import OrderEvidence, InvalidDocument, BASE
from bonuspro.adapters.moysklad.order import read_order
from bonuspro.adapters.postgres.ledger import Conflict

def ref(kind,identity=None):
    return {"meta":{"type":kind,"href":BASE+"/entity/"+kind+"/"+(identity or str(uuid4()))}}

class OrderTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.order,self.marker=(str(uuid4()) for _ in range(3))
        self.doc=dict(id=self.order,accountId=self.tenant,applicable=True,updated="2026-10-14 10:00:00.000",
            sum=1200000,payedSum=0,shippedSum=0,agent=ref("counterparty"),organization=ref("organization"),
            rate={"currency":ref("currency"),"value":1},vatIncluded=True,vatEnabled=False,name="0001")
        self.lines=[dict(id=str(uuid4()),assortment=ref("product"),price=1000000,quantity=1,discount=0,vat=0),
                    dict(id=str(uuid4()),assortment=ref("service"),price=200000,quantity=1,discount=0,vat=0)]
    def client(self):
        outer=self
        class Client:
            tenant=outer.tenant
            def order_evidence(self,order):
                return OrderEvidence(copy.deepcopy(outer.doc),tuple(copy.deepcopy(outer.lines)),(),())
            def entity(self,kind,identity): return {"isoCode":"KZT"}
        return Client()
    def read(self): return read_order(self.client(),self.order,marker_id=self.marker)

    def test_only_goods_and_exact_discount_delivery_unchanged(self):
        saved=self.read()
        self.assertEqual(saved.snapshot.eligible_minor,1000000)
        plan=saved.plan(3000)
        self.assertEqual(plan["update_state"]["positions"],
                         [{"id":self.lines[0]["id"],"discount":30.0},{"id":self.lines[1]["id"]}])
        self.assertEqual(plan["after"]["total_minor"],900000)
        self.assertEqual(len(plan["update_state"]["positions"]),2)

    def test_existing_discount_uses_net_goods_basis(self):
        self.lines[0]["discount"]=10;self.doc["sum"]=1100000
        saved=self.read()
        self.assertEqual(saved.snapshot.eligible_minor,900000)
        plan=saved.plan(1000)
        line=next(x for x in plan["after"]["lines"] if x["kind"]=="product")
        self.assertEqual(line["discount_bps"],2000)

    def test_unrepresentable_amount_rejected_instead_of_rounding_points(self):
        self.lines[0].update(price=10000,quantity=3);self.doc["sum"]=230000
        with self.assertRaises(InvalidDocument):self.read().plan(1)

    def test_monetary_flags_and_precision_fail_closed(self):
        for field,value in (("price",True),("quantity",1.1),("discount","5"),("discount",Decimal("1.001")),
                            ("quantity",0),("price",Decimal("1.001")),("discount",-1)):
            with self.subTest(field=field,value=value):
                original=self.lines[0][field];self.lines[0][field]=value
                with self.assertRaises(InvalidDocument):self.read()
                self.lines[0][field]=original

    def test_total_mismatch_and_exclusive_vat_rejected(self):
        self.doc["sum"]+=1
        with self.assertRaises(InvalidDocument):self.read()
        self.doc["sum"]-=1;self.doc["vatIncluded"]=False;self.doc["vatEnabled"]=True
        with self.assertRaises(InvalidDocument):self.read()

    def test_all_services_has_no_bonus_discount(self):
        self.lines[0]["assortment"]=ref("service")
        self.assertEqual(self.read().snapshot.eligible_minor,0)
        with self.assertRaises(InvalidDocument):self.read().plan(1)

    def test_wrong_currency_and_currency_rate(self):
        client=self.client();client.entity=lambda *args:{"isoCode":"RUB"}
        with self.assertRaises(InvalidDocument):read_order(client,self.order)
        self.doc["rate"]["value"]=Decimal("1.1")
        with self.assertRaises(InvalidDocument):self.read()

    def test_payment_shipment_cancellation_and_marker_block_plan(self):
        for field,value in (("payedSum",1),("shippedSum",1),("applicable",False)):
            original=self.doc[field];self.doc[field]=value
            with self.assertRaises(Conflict):self.read().plan(1)
            self.doc[field]=original
        self.doc["attributes"]=[{"id":self.marker,"value":"bonuspro:v1:existing"}]
        with self.assertRaises(InvalidDocument):self.read().plan(1)

    def test_changed_customer_quantity_and_revision_change_source(self):
        first=self.read().source_hash
        self.doc["agent"]=ref("counterparty")
        self.assertNotEqual(first,self.read().source_hash)
        first=self.read().source_hash;self.doc["updated"]="2026-10-14 11:00:00.000"
        self.assertNotEqual(first,self.read().source_hash)

    def test_unknown_assortment_duplicate_and_ssrf_reference(self):
        self.lines[0]["assortment"]=ref("evil")
        with self.assertRaises(InvalidDocument):self.read()
        self.lines[0]["assortment"]=ref("product")
        self.lines[1]["id"]=self.lines[0]["id"]
        with self.assertRaises(InvalidDocument):self.read()
        self.lines[1]["id"]=str(uuid4())
        self.doc["agent"]["meta"]["href"]="http://127.0.0.1/internal"
        with self.assertRaises(InvalidDocument):self.read()

    def test_canonical_plan_does_not_depend_on_line_order_for_verification(self):
        before=self.read().financial()
        self.lines.reverse()
        self.assertEqual(before,self.read().financial())

    def test_disabled_vat_may_omit_vat_included_but_explicit_flags_are_preserved(self):
        del self.doc["vatIncluded"]
        saved=self.read()
        self.assertFalse(saved.vat_enabled)
        self.assertTrue(saved.vat_included)
        self.assertEqual(saved.plan(1000)["after"]["total_minor"],1100000)
        for flag in (False,True):
            self.doc["vatIncluded"]=flag
            self.assertIs(self.read().financial()["vat_included"],flag)

    def test_missing_enabled_vat_or_non_boolean_flags_still_fail_closed(self):
        for enabled,included in ((True,None),(False,None),(False,"false"),("false",True)):
            self.doc["vatEnabled"]=enabled;self.doc["vatIncluded"]=included
            with self.subTest(enabled=enabled,included=included),self.assertRaises(InvalidDocument) as error:
                self.read()
            self.assertEqual(error.exception.reason,"vat_mode_missing")
        self.doc["vatEnabled"]=True
        del self.doc["vatIncluded"]
        with self.assertRaises(InvalidDocument):self.read()

    def test_fractional_quantity_and_ordinary_discount_have_exact_saved_plan(self):
        self.lines=[{**self.lines[0],"quantity":Decimal("1.25"),"price":10000,"discount":10}]
        self.doc["sum"]=11250
        plan=self.read().plan(1)
        self.assertEqual(plan["after"]["total_minor"],11150)
        self.assertEqual(plan["update_state"]["positions"],[{"id":self.lines[0]["id"],"discount":10.8}])

    def test_price_fallback_applies_5000_points_to_99990_and_keeps_delivery_and_vat(self):
        self.lines[0].update(price=9999000,vat=16,vatEnabled=True)
        self.doc.update(sum=10199000,vatEnabled=True,vatIncluded=True)
        saved=self.read()
        plan=saved.plan(5000)
        self.assertEqual(plan["update_state"]["positions"],
            [{"id":self.lines[0]["id"],"price":9499000},{"id":self.lines[1]["id"]}])
        self.assertIs(type(plan["update_state"]["positions"][0]["price"]),int)
        before={line["id"]:line for line in plan["before"]["lines"]}
        after={line["id"]:line for line in plan["after"]["lines"]}
        self.assertEqual(after[self.lines[0]["id"]],{**before[self.lines[0]["id"]],
            "price":str(Decimal(9499000).normalize())})
        self.assertEqual(after[self.lines[1]["id"]],before[self.lines[1]["id"]])
        self.assertEqual(plan["after"]["total_minor"],9699000)
        self.assertEqual(plan["after"]["total_minor"],sum(
            replace(line,price=Decimal(9499000).normalize()).amount if line.kind=="product" else line.amount
            for line in saved.lines))
        # A real provider read normalizes scientific notation in the same way.
        self.lines[0]["price"]=9499000
        self.doc["sum"]=9699000
        self.assertEqual(self.read().financial(),plan["after"])

    def test_price_fallback_preserves_the_ordinary_discount(self):
        self.lines[0].update(price=9999000,discount=10)
        self.doc["sum"]=9199100
        plan=self.read().plan(5000)
        self.assertEqual(plan["update_state"]["positions"],
            [{"id":self.lines[0]["id"],"price":9443444},{"id":self.lines[1]["id"]}])
        changed=next(line for line in plan["after"]["lines"] if line["kind"]=="product")
        self.assertEqual(changed["discount_bps"],1000)
        self.assertEqual(plan["after"]["total_minor"],8699100)

    def test_one_plan_can_mix_percentage_and_integral_price_changes(self):
        self.lines[0]["price"]=9999000
        extra={**copy.deepcopy(self.lines[0]),"id":str(uuid4()),"assortment":ref("product"),"price":10000}
        self.lines.insert(1,extra)
        self.doc["sum"]=10209000
        plan=self.read().plan(5000)
        self.assertEqual(plan["update_state"]["positions"],[
            {"id":self.lines[0]["id"],"price":9499500},
            {"id":extra["id"],"discount":5.0},{"id":self.lines[2]["id"]}])
        self.assertEqual(plan["before"]["total_minor"]-plan["after"]["total_minor"],500000)

    def test_price_fallback_cannot_pool_distinct_original_return_groups(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=price,quantity=1,discount=0,vat=0)
                    for price in (1000000,1000000,1000000,1000000,1000001,1000001)]
        self.doc["sum"]=6000002
        with self.assertRaises(InvalidDocument) as error:self.read().plan(2)
        self.assertEqual(error.exception.reason,"discount_not_representable")

    def test_percentage_plan_cannot_pool_different_original_discounts_on_full_redemption(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=10000,
                        quantity=1,discount=discount,vat=0) for discount in (0,10)]
        self.doc["sum"]=19000
        # Both rows would be saved at 100% off, making a one-unit return
        # ambiguous between the original 100-point and 90-point allocations.
        with self.assertRaises(InvalidDocument) as error:self.read().plan(190)
        self.assertEqual(error.exception.reason,"discount_not_representable")

    def test_plans_match_exhaustive_integral_price_and_percentage_representations(self):
        base=self.read()
        for price in (10001,11111,19999):
            for quantity in (1,3,7):
                for ordinary_bps in (0,1000,2500):
                    line=replace(base.lines[0],price=Decimal(price).normalize(),
                        quantity=Decimal(quantity),discount_bps=ordinary_bps)
                    order=replace(base,lines=(line,),snapshot=replace(base.snapshot,
                        total_minor=line.amount,eligible_minor=line.amount))
                    percentages={replace(line,discount_bps=bps).amount for bps in range(ordinary_bps,10001)}
                    integral_prices={replace(line,price=Decimal(value).normalize()).amount for value in range(price+1)}
                    for points in (1,2,7):
                        with self.subTest(price=price,quantity=quantity,ordinary_bps=ordinary_bps,points=points):
                            target=line.amount-points*100
                            if target not in percentages|integral_prices:
                                with self.assertRaises(InvalidDocument):order.plan(points)
                                continue
                            plan=order.plan(points)
                            record=plan["after"]["lines"][0]
                            changed=replace(line,price=Decimal(record["price"]),discount_bps=record["discount_bps"])
                            self.assertEqual(changed.amount,target)
                            self.assertEqual(plan["after"]["total_minor"],target)
                            self.assertLessEqual(changed.price,line.price)
                            self.assertEqual(changed.quantity,line.quantity)
                            if changed.price!=line.price:
                                self.assertEqual(changed.discount_bps,line.discount_bps)
                                self.assertEqual(changed.price,changed.price.to_integral_value())
                                self.assertIs(type(plan["update_state"]["positions"][0]["price"]),int)

if __name__=="__main__":unittest.main()
