"""Return money keeps provider rounding and immutable bonus allocations."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import time
import unittest
from uuid import uuid4

from bonuspro.adapters.moysklad.fulfillment import Fulfillment
from bonuspro.adapters.moysklad.order import Line, SavedOrder
from bonuspro.adapters.moysklad.remap import InvalidDocument
from bonuspro.adapters.moysklad.settlement import targets
from bonuspro.adapters.postgres.checkout import OrderSnapshot
from bonuspro.domain.finance import Rules


class LegacySettlementTests(unittest.TestCase):
    def setUp(self):
        tenant,order,customer,organization,sku=(str(uuid4()) for _ in range(5))
        first=Line(str(uuid4()),"product",sku,Decimal(1),Decimal("10000").normalize(),
                   0,Decimal(0),False,None)
        second=replace(first,id=str(uuid4()),discount_bps=1000)
        original=SavedOrder(OrderSnapshot(tenant,order,customer,"2026-10-19 10:00:00.000",
                            "KZT",19000,19000),(first,second),organization,True,False,None,"legacy")
        self.saved=replace(original,lines=tuple(replace(line,discount_bps=10000) for line in original.lines),
                           snapshot=replace(original.snapshot,total_minor=0,eligible_minor=0),
                           marker="bonuspro:v1:legacy")
        # Construct a persisted pre-guard plan, rather than generating a new plan.
        self.attempt={"points":190,"plan":{"before":original.financial(),"after":self.saved.financial()}}
        self.proof=Fulfillment(self.saved,datetime.now(timezone.utc),0,0,"eligible",
                               "a"*64,time.monotonic()+120,full_payment=True,full_shipment=True)

    def returned(self,quantity):
        return replace(self.proof,returned=(replace(self.saved.lines[0],id=str(uuid4()),
                                                    quantity=Decimal(quantity)),))

    def test_legacy_merged_different_original_allocations_block_partial_return(self):
        with self.assertRaisesRegex(InvalidDocument,"partial return cannot identify"):
            targets(self.returned(1),rules=Rules(),attempt=self.attempt)

    def test_legacy_merged_different_original_allocations_allow_full_return(self):
        result=targets(self.returned(2),rules=Rules(),attempt=self.attempt)
        self.assertEqual((result.earn,result.refund),(0,190))

    def test_legacy_merged_different_original_allocations_allow_no_return(self):
        result=targets(self.proof,rules=Rules(),attempt=self.attempt)
        self.assertEqual((result.earn,result.refund),(9,0))

    def test_same_original_signature_partial_return_keeps_exact_allocation(self):
        # Equivalent Decimal spellings must not make identical signatures ambiguous.
        before=self.attempt["plan"]["before"]
        for line in before["lines"]:
            line["discount_bps"]=0
            line["price"]="10000.00"
        before["total_minor"]=20000
        self.attempt["points"]=200
        result=targets(self.returned(1),rules=Rules(),attempt=self.attempt)
        self.assertEqual((result.earn,result.refund),(5,100))


class _SettlementFixture(unittest.TestCase):
    def make_proof(self,quantities,price="10000"):
        tenant,order,customer,organization,sku=(str(uuid4()) for _ in range(5))
        lines=tuple(Line(str(uuid4()),"product",sku,Decimal(q),Decimal(price).normalize(),
                         0,Decimal(0),False,None) for q in quantities)
        amount=sum(line.amount for line in lines)
        saved=SavedOrder(OrderSnapshot(tenant,order,customer,"2026-10-19 10:00:00.000",
                         "KZT",amount,amount),lines,organization,True,False,None,"rounded")
        return Fulfillment(saved,datetime.now(timezone.utc),0,0,"eligible",
                           "b"*64,time.monotonic()+120,full_payment=True,full_shipment=True)

    def return_quantities(self,proof,quantities):
        return replace(proof,returned=tuple(replace(proof.saved.lines[0],id=str(uuid4()),
                            quantity=Decimal(q)) for q in quantities))


class RoundedReturnSettlementTests(_SettlementFixture):
    def test_ordinary_partial_return_uses_native_money_instead_of_pooled_quantity(self):
        proof=self.make_proof(("0.199951","0.200049"))
        self.assertEqual([line.amount for line in proof.saved.lines],[2000,2000])
        result=targets(self.return_quantities(proof,("0.200049",)),rules=Rules())
        self.assertEqual((result.earn,result.refund),(1,0))

    def test_ordinary_no_return_and_full_return_keep_exact_endpoints(self):
        proof=self.make_proof(("0.199951","0.200049"))
        self.assertEqual((targets(proof,rules=Rules()).earn,
                          targets(proof,rules=Rules()).refund),(2,0))
        full=self.return_quantities(proof,("0.4",))
        self.assertEqual((targets(full,rules=Rules()).earn,
                          targets(full,rules=Rules()).refund),(0,0))

    def test_single_row_partial_return_uses_provider_rounded_money(self):
        proof=self.make_proof(("4",))
        partial=self.return_quantities(proof,("2.000049",))
        self.assertEqual(partial.returned[0].amount,20000)
        self.assertEqual(targets(partial,rules=Rules()).earn,10)

    def test_partial_native_return_cannot_exceed_original_money(self):
        proof=self.make_proof(("1.4",),price="1")
        partial=self.return_quantities(proof,("0.6","0.6"))
        self.assertEqual(sum(line.amount for line in partial.returned),2)
        self.assertEqual(proof.saved.lines[0].amount,1)
        with self.assertRaisesRegex(InvalidDocument,"partial return amount exceeds"):
            targets(partial,rules=Rules())

    def test_full_quantity_return_removes_entire_base_despite_split_rounding(self):
        proof=self.make_proof(("1.4",),price="1")
        full=self.return_quantities(proof,("0.7","0.7"))
        self.assertEqual(sum(line.amount for line in full.returned),2)
        result=targets(full,rules=Rules())
        self.assertEqual((result.earn,result.refund),(0,0))

    def test_native_return_money_is_part_of_digest_even_if_targets_match(self):
        proof=self.make_proof(("10",),price="1")
        single=self.return_quantities(proof,("0.6",))
        split=self.return_quantities(proof,("0.3","0.3"))
        self.assertEqual(sum(line.quantity for line in single.returned),
                         sum(line.quantity for line in split.returned))
        self.assertEqual(sum(line.amount for line in single.returned),1)
        self.assertEqual(sum(line.amount for line in split.returned),0)
        first=targets(single,rules=Rules());second=targets(split,rules=Rules())
        self.assertEqual((first.earn,first.refund),(second.earn,second.refund))
        self.assertNotEqual(first.digest,second.digest)


class RoundedLegacyAllocationTests(_SettlementFixture):
    def legacy_proof(self,quantities,price="10000",ordinary_discount=0,after_price=None):
        proof=self.make_proof(quantities,price=price)
        original_lines=tuple(replace(line,discount_bps=ordinary_discount)
                             for line in proof.saved.lines)
        original_amount=sum(line.amount for line in original_lines)
        original=replace(proof.saved,lines=original_lines,
                         snapshot=replace(proof.saved.snapshot,total_minor=original_amount,
                                          eligible_minor=original_amount))
        after_lines=tuple(replace(line,price=Decimal(after_price).normalize()) if after_price
                          else replace(line,discount_bps=10000) for line in original.lines)
        after_amount=sum(line.amount for line in after_lines)
        saved=replace(original,lines=after_lines,marker="bonuspro:v1:legacy-rounded",
                      snapshot=replace(original.snapshot,total_minor=after_amount,
                                       eligible_minor=after_amount))
        self.assertEqual((original_amount-after_amount)%100,0)
        attempt={"points":(original_amount-after_amount)//100,
                 "plan":{"before":original.financial(),"after":saved.financial()}}
        return replace(proof,saved=saved),attempt

    def test_legacy_same_signature_unequal_rounded_rates_block_partial_return(self):
        proof,attempt=self.legacy_proof(("1.000049","1.009951"))
        self.assertEqual(attempt["points"],201)
        self.assertEqual([line["discount_bps"] for line in attempt["plan"]["before"]["lines"]],[0,0])
        partial=self.return_quantities(proof,("1.009951",))
        with self.assertRaisesRegex(InvalidDocument,"partial return cannot identify"):
            targets(partial,rules=Rules(),attempt=attempt)

    def test_legacy_unequal_rounded_rates_allow_full_return(self):
        proof,attempt=self.legacy_proof(("1.000049","1.009951"))
        result=targets(self.return_quantities(proof,("2.01",)),rules=Rules(),attempt=attempt)
        self.assertEqual((result.earn,result.refund),(0,201))

    def test_legacy_unequal_rounded_rates_allow_no_return(self):
        proof,attempt=self.legacy_proof(("1.000049","1.009951"))
        result=targets(proof,rules=Rules(),attempt=attempt)
        self.assertEqual((result.earn,result.refund),(10,0))

    def test_legacy_different_quantities_with_equal_rates_allow_partial_return(self):
        proof,attempt=self.legacy_proof(("1","2"))
        result=targets(self.return_quantities(proof,("1",)),rules=Rules(),attempt=attempt)
        self.assertEqual((result.earn,result.refund),(10,100))

    def test_legacy_equal_original_rates_but_different_bonus_rates_block_partial_return(self):
        proof,attempt=self.legacy_proof(("1","2"),price="20010",ordinary_discount=1000,
                                        after_price="19084")
        self.assertEqual(attempt["points"],25)
        self.assertEqual([line.amount for line in proof.saved.lines],[17176,34351])
        with self.assertRaisesRegex(InvalidDocument,"partial return cannot identify"):
            targets(self.return_quantities(proof,("1",)),rules=Rules(),attempt=attempt)


    def mixed_proof(self,*,group_kind="product",quantities=("0.199951","0.200049"),price="10000"):
        proof=self.make_proof(quantities,price=price)
        group_lines=tuple(replace(line,kind=group_kind) for line in proof.saved.lines)
        bonus_line=replace(group_lines[0],id=str(uuid4()),kind="product",assortment=str(uuid4()),
                           quantity=Decimal(1),price=Decimal("10000").normalize())
        original_lines=group_lines+(bonus_line,)
        original_amount=sum(line.amount for line in original_lines)
        original_eligible=sum(line.amount for line in original_lines if line.kind!="service")
        original=replace(proof.saved,lines=original_lines,
                         snapshot=replace(proof.saved.snapshot,total_minor=original_amount,
                                          eligible_minor=original_eligible))
        saved=replace(original,lines=group_lines+(replace(bonus_line,discount_bps=1000),),
                      snapshot=replace(original.snapshot,total_minor=original_amount-1000,
                                       eligible_minor=original_eligible-1000),
                      marker="bonuspro:v1:mixed")
        attempt={"points":10,"plan":{"before":original.financial(),"after":saved.financial()}}
        return replace(proof,saved=saved),attempt

    def test_unallocated_group_uses_native_money_when_another_group_has_bonus(self):
        proof,attempt=self.mixed_proof()
        partial=self.return_quantities(proof,("0.200049",))
        result=targets(partial,rules=Rules(),attempt=attempt)
        # Unchanged returned goods are 2000 minor; the other goods had a 1000
        # minor bonus. Remaining original earning base is exactly 12000.
        self.assertEqual((result.earn,result.refund),(6,0))

    def test_unallocated_group_native_money_cannot_exceed_original(self):
        proof,attempt=self.mixed_proof(quantities=("1.4",),price="1")
        partial=self.return_quantities(proof,("0.6","0.6"))
        with self.assertRaisesRegex(InvalidDocument,"partial return amount exceeds"):
            targets(partial,rules=Rules(),attempt=attempt)

    def test_unallocated_group_native_money_is_in_attempt_digest(self):
        proof,attempt=self.mixed_proof(quantities=("10",),price="1")
        single=targets(self.return_quantities(proof,("0.6",)),rules=Rules(),attempt=attempt)
        split=targets(self.return_quantities(proof,("0.3","0.3")),rules=Rules(),attempt=attempt)
        self.assertEqual((single.earn,single.refund),(split.earn,split.refund))
        self.assertNotEqual(single.digest,split.digest)

    def test_service_rounding_does_not_block_partial_return_with_bonus_on_goods(self):
        proof,attempt=self.mixed_proof(group_kind="service")
        result=targets(self.return_quantities(proof,("0.200049",)),rules=Rules(),attempt=attempt)
        self.assertEqual((result.earn,result.refund),(5,0))

    def test_personal_discount_is_excluded_from_bonus_refund_and_earning_base(self):
        proof=self.make_proof(("2",))
        original=proof.saved
        personal=replace(original,lines=(replace(original.lines[0],discount_bps=1000),),
                         snapshot=replace(original.snapshot,total_minor=18000,eligible_minor=18000))
        saved=replace(personal,lines=(replace(personal.lines[0],discount_bps=1500),),
                      snapshot=replace(personal.snapshot,total_minor=17000,eligible_minor=17000),
                      marker="bonuspro:v1:personal-and-bonus")
        proof=replace(proof,saved=saved)
        attempt={"points":10,"plan":{"before":original.financial(),
                 "bonus_before":personal.financial(),"after":saved.financial()}}
        no_return=targets(proof,rules=Rules(),attempt=attempt)
        partial=targets(self.return_quantities(proof,("1",)),rules=Rules(),attempt=attempt)
        full=targets(self.return_quantities(proof,("2",)),rules=Rules(),attempt=attempt)
        self.assertEqual((no_return.earn,no_return.refund),(9,0))
        self.assertEqual((partial.earn,partial.refund),(4,5))
        self.assertEqual((full.earn,full.refund),(0,10))


if __name__=="__main__":unittest.main()
