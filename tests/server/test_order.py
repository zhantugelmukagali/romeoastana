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

    def test_wrong_document_currency_is_rejected(self):
        client=self.client();client.entity=lambda *args:{"isoCode":"RUB"}
        with self.assertRaises(InvalidDocument):read_order(client,self.order)

    def test_kzt_base_conversion_rate_does_not_convert_loyalty_amounts_or_host_plan(self):
        original=self.read();plan=original.plan(1000)
        for rate in (Decimal("0.2"),Decimal("1.1"),Decimal("1000")):
            with self.subTest(rate=rate):
                self.doc["rate"]["value"]=rate
                saved=self.read();current=saved.plan(1000)
                self.assertEqual(saved.financial(),original.financial())
                self.assertEqual(saved.source_hash,original.source_hash)
                self.assertEqual(current["before"],plan["before"])
                self.assertEqual(current["after"],plan["after"])
                self.assertEqual(current["update_state"],plan["update_state"])
                self.assertNotIn("rate",current["update_state"])
        self.doc["rate"].pop("value")
        self.assertEqual(self.read().financial(),original.financial())
        self.assertEqual(self.read().source_hash,original.source_hash)
        self.doc["updated"]="2026-10-14 10:00:01.000"
        self.assertNotEqual(self.read().source_hash,original.source_hash)

    def test_present_kzt_conversion_rate_must_be_exact_finite_and_positive(self):
        for value in (True,False,1.1,None,"1.1",0,-1,Decimal("NaN"),Decimal("Infinity"),Decimal("-Infinity")):
            with self.subTest(value=value):
                self.doc["rate"]["value"]=value
                with self.assertRaises(InvalidDocument) as rejected:
                    self.read()
                self.assertEqual(rejected.exception.reason,"exchange_rate_unsupported")

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

    def test_fractional_lines_cannot_pool_different_rounded_amounts_per_unit(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=10000,
                        quantity=Decimal(quantity),discount=0,vat=0)
                    for quantity in ("1.000049","1.009951")]
        self.doc["sum"]=20100
        saved=self.read()
        self.assertEqual([line.amount for line in saved.lines],[10000,10100])
        with self.assertRaises(InvalidDocument) as error:saved.plan(201)
        self.assertEqual(error.exception.reason,"discount_not_representable")

    def test_price_fallback_cannot_pool_different_bonus_amounts_per_unit(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=20010,
                        quantity=quantity,discount=10,vat=0) for quantity in (1,2)]
        self.doc["sum"]=54027
        # Both rows would keep 10% off and save price 19084. Their original
        # amounts per unit agree, but the bonus allocations are 833 and 1667
        # minor units, which cannot be pooled over quantities one and two.
        with self.assertRaises(InvalidDocument) as error:self.read().plan(25)
        self.assertEqual(error.exception.reason,"discount_not_representable")

    def test_different_quantities_with_equal_per_unit_allocations_may_pool(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=10000,
                        quantity=quantity,discount=0,vat=0) for quantity in (1,2)]
        self.doc["sum"]=30000
        plan=self.read().plan(300)
        self.assertEqual(plan["after"]["total_minor"],0)
        self.assertEqual(plan["update_state"]["positions"],
                         [{"id":line["id"],"discount":100.0} for line in self.lines])

    def test_ineligible_services_do_not_require_equal_per_unit_allocations(self):
        service=ref("service")
        self.lines.extend(dict(id=str(uuid4()),assortment=service,price=10000,
                               quantity=Decimal(quantity),discount=0,vat=0)
                          for quantity in ("1.000049","1.009951"))
        self.doc["sum"]=1220100
        plan=self.read().plan(1000)
        self.assertEqual([line for line in plan["after"]["lines"] if line["kind"]=="service"],
                         [line for line in plan["before"]["lines"] if line["kind"]=="service"])
        self.assertEqual(plan["after"]["total_minor"],1120100)

    def test_unchanged_goods_group_does_not_require_equal_per_unit_allocations(self):
        first=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=first,price=10000,
                        quantity=Decimal(quantity),discount=0,vat=0)
                    for quantity in ("0.199951","0.200049")]
        self.lines.append(dict(id=str(uuid4()),assortment=ref("product"),price=1000000,
                               quantity=1,discount=0,vat=0))
        self.doc["sum"]=1004000
        saved=self.read()
        plan=saved.plan(1)
        self.assertEqual(plan["after"]["total_minor"],1003900)
        self.assertEqual(plan["update_state"]["positions"],[
            {"id":self.lines[0]["id"]},{"id":self.lines[1]["id"]},
            {"id":self.lines[2]["id"],"discount":0.01}])
        before={line["id"]:line for line in plan["before"]["lines"]}
        after={line["id"]:line for line in plan["after"]["lines"]}
        for line in self.lines[:2]:
            self.assertEqual(after[line["id"]],before[line["id"]])

    def test_personal_skip_only_preserves_goods_with_an_existing_discount(self):
        self.lines[0].update(price=10000,discount=10)
        extra={**copy.deepcopy(self.lines[0]),"id":str(uuid4()),"assortment":ref("product"),"discount":0}
        self.lines.insert(1,extra)
        self.doc["sum"]=219000
        saved=self.read()
        before=saved.financial()
        projected,records=saved.personal_discount(2000,"skip")
        self.assertEqual(projected.snapshot.total_minor,217000)
        self.assertEqual(projected.snapshot.eligible_minor,17000)
        self.assertEqual([line.amount for line in projected.lines],[9000,8000,200000])
        self.assertEqual(records,[
            dict(id=self.lines[0]["id"],ordinary_discount_bps=1000,personal_rate_bps=2000,
                 policy="skip",before_minor=9000,personal_basis_minor=9000,personal_discount_minor=0),
            dict(id=extra["id"],ordinary_discount_bps=0,personal_rate_bps=2000,
                 policy="skip",before_minor=10000,personal_basis_minor=8000,personal_discount_minor=2000)])
        self.assertEqual(projected.lines[-1],saved.lines[-1])
        self.assertEqual(saved.financial(),before)

    def test_personal_best_and_sequential_use_the_agreed_ordinary_discount_bases(self):
        self.lines[0].update(price=10000,discount=10)
        self.doc["sum"]=209000
        saved=self.read()
        for policy,amount,bps,delta in (("best",8000,2000,1000),("sequential",7200,2800,1800)):
            with self.subTest(policy=policy):
                projected,records=saved.personal_discount(2000,policy)
                self.assertEqual(projected.lines[0].amount,amount)
                self.assertEqual(projected.lines[0].discount_bps,bps)
                self.assertEqual(projected.snapshot.total_minor,200000+amount)
                self.assertEqual(projected.snapshot.eligible_minor,amount)
                self.assertEqual(records[0]["ordinary_discount_bps"],1000)
                self.assertEqual(records[0]["personal_basis_minor"],amount)
                self.assertEqual(records[0]["personal_discount_minor"],delta)
        projected,records=saved.personal_discount(500,"best")
        self.assertEqual(projected.financial(),saved.financial())
        self.assertEqual(records[0]["personal_discount_minor"],0)

    def test_personal_sequential_rounds_the_saved_net_line_amount_first(self):
        self.lines[0].update(price=10001,quantity=Decimal("0.5"))
        self.doc["sum"]=205001
        saved=self.read()
        best,_=saved.personal_discount(5000,"best")
        sequential,_=saved.personal_discount(5000,"sequential")
        self.assertEqual(best.lines[0].amount,2500)
        self.assertEqual(sequential.lines[0].amount,2501)
        for policy in ("skip","best","sequential"):
            projected,records=saved.personal_discount(0,policy)
            self.assertEqual(projected.financial(),saved.financial())
            self.assertEqual(records[0]["personal_discount_minor"],0)

    def test_personal_projection_preserves_supported_goods_packs_tax_and_order_metadata(self):
        saved=self.read()
        pack=str(uuid4())
        goods=tuple(replace(saved.lines[0],id=str(uuid4()),kind=kind,assortment=str(uuid4()),
                            price=Decimal(10000),vat=Decimal(16),vat_enabled=True,pack=pack)
                    for kind in ("product","variant","bundle"))
        saved=replace(saved,lines=goods+(saved.lines[-1],),vat_enabled=True,
            marker="existing-marker",discount_blocked_reason="order_has_marker",
            snapshot=replace(saved.snapshot,total_minor=230000,eligible_minor=30000,
                             cancelled=True,paid_minor=1,shipped_minor=1))
        projected,records=saved.personal_discount(2000,"best")
        self.assertEqual([line.amount for line in projected.lines],[8000,8000,8000,200000])
        self.assertEqual([line.pack for line in projected.lines[:3]],[pack]*3)
        self.assertTrue(all(line.vat==16 and line.vat_enabled for line in projected.lines[:3]))
        self.assertEqual(projected.lines[-1],saved.lines[-1])
        self.assertEqual((projected.marker,projected.name,projected.discount_blocked_reason),
                         (saved.marker,saved.name,saved.discount_blocked_reason))
        self.assertEqual(projected.snapshot,replace(saved.snapshot,total_minor=224000,eligible_minor=24000))
        self.assertEqual(len(records),3)

    def test_personal_sequential_uses_exact_integral_price_fallback_and_readback(self):
        self.lines[0].update(price=9999000,discount=Decimal("10.01"))
        self.doc["sum"]=9198100
        saved=self.read()
        projected,records=saved.personal_discount(2001,"sequential")
        changed=projected.lines[0]
        self.assertEqual((changed.price,changed.discount_bps,changed.amount),(Decimal(7998200),1001,7197580))
        self.assertEqual(changed.price,changed.price.to_integral_value())
        self.assertEqual(records[0]["personal_discount_minor"],1800520)
        self.assertEqual(projected.snapshot.total_minor,7397580)
        self.lines[0]["price"]=7998200
        self.doc["sum"]=7397580
        self.assertEqual(self.read().financial(),projected.financial())

    def test_personal_unrepresentable_target_fails_without_changing_the_saved_order(self):
        self.lines[0].update(price=10001,quantity=3,discount=10)
        self.doc["sum"]=227003
        saved=self.read()
        before=saved.financial()
        # Saved ordinary net 27003 * 66.67% rounds to 18003. Neither
        # percentage steps nor integral prices retaining 10% reach that value.
        with self.assertRaises(InvalidDocument) as error:saved.personal_discount(3333,"sequential")
        self.assertEqual(error.exception.reason,"discount_not_representable")
        self.assertEqual(saved.financial(),before)

    def test_personal_projection_may_merge_personal_bases_before_bonus_allocation(self):
        product=ref("product")
        self.lines=[dict(id=str(uuid4()),assortment=product,price=10000,
                        quantity=1,discount=discount,vat=0) for discount in (0,10)]
        self.doc["sum"]=19000
        projected,records=self.read().personal_discount(2000,"best")
        self.assertEqual(projected.snapshot.total_minor,16000)
        self.assertEqual([record["personal_discount_minor"] for record in records],[2000,1000])
        plan=projected.plan(1)
        self.assertEqual(plan["before"],projected.financial())
        self.assertEqual(plan["after"]["total_minor"],15900)

    def test_personal_rate_and_policy_contract_are_bounded(self):
        saved=self.read()
        for rate in (-1,True,1.0,10001):
            with self.subTest(rate=rate),self.assertRaises(ValueError):
                saved.personal_discount(rate,"best")
        for policy in (None,"",False,[],{},"unknown"):
            with self.subTest(policy=policy),self.assertRaises(ValueError):
                saved.personal_discount(1000,policy)
        projected,records=saved.personal_discount(10000,"best")
        self.assertEqual(projected.snapshot.eligible_minor,0)
        self.assertEqual(projected.snapshot.total_minor,200000)
        self.assertEqual(records[0]["personal_basis_minor"],0)

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
