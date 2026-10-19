"""Legacy immutable plans must not pool ambiguous partial-return allocations."""
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


if __name__=="__main__":unittest.main()
