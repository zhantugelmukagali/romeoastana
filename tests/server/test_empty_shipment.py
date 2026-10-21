"""An extra empty shipment cannot replace independent saved shipment proof."""
import copy
import unittest
from decimal import Decimal
from uuid import uuid4

from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader
from bonuspro.adapters.moysklad.remap import InvalidDocument,OrderEvidence,RetryLater
from test_fulfillment import Fixture,ref


class EmptyShipmentFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.empty_id=str(uuid4())
        self.empty={**copy.deepcopy(self.ship),"id":self.empty_id,
                    "applicable":True,"sum":0,"payedSum":0,"shippedSum":0}
        self.empty_rows=()
        self.empty_returns=[]
        self.change_empty=False

    def order_evidence(self,identity):
        evidence=super().order_evidence(identity)
        empty=copy.deepcopy(self.empty)
        if self.change_empty and self.reads==2:
            empty["applicable"]=not empty["applicable"]
        return OrderEvidence(evidence.order,evidence.positions,evidence.payments,
                             evidence.demands+(empty,))

    def positions(self,kind,identity):
        if kind=="demand" and identity==self.empty_id:
            return copy.deepcopy(self.empty_rows)
        return super().positions(kind,identity)

    def linked(self,doc,field,allowed):
        if doc["id"]==self.empty_id:
            return copy.deepcopy(self.empty_returns) if field=="returns" else []
        return super().linked(doc,field,allowed)


class EmptyShipmentTests(unittest.TestCase):
    def setUp(self):
        self.fx=EmptyShipmentFixture()

    def read(self):
        return FulfillmentReader(self.fx).read(self.fx.order)

    def test_extra_posted_or_unposted_zero_is_not_shipment_or_payment_proof(self):
        self.fx.empty["sum"]=Decimal("0.0")
        for posted in (True,False):
            with self.subTest(posted=posted):
                self.fx.empty["applicable"]=posted
                proof=self.read()
                self.assertEqual(proof.outcome,"eligible")
                self.assertEqual((proof.paid_minor,proof.shipped_minor),(1200000,1200000))
                self.assertTrue(proof.full_payment and proof.full_shipment)
                self.assertIn(("demand",self.fx.empty_id),proof.related)
                self.assertFalse(proof.presale_safe)
                self.assertFalse(proof.cancel_safe)
        self.fx.doc["applicable"]=False
        self.assertFalse(self.read().cancel_safe)

    def test_zero_empty_cannot_replace_the_only_full_shipment_or_complete_partial_rows(self):
        self.fx.include_ship=False
        with self.assertRaises(InvalidDocument):
            self.read()
        self.fx.include_ship=True
        self.fx.ship_lines[0]["quantity"]=Decimal("0.5")
        self.fx.ship["sum"]=700000
        with self.assertRaises(InvalidDocument):
            self.read()

    def test_empty_nonzero_or_malformed_positions_never_use_the_exception(self):
        for value in (1,True,None,-1):
            with self.subTest(sum=value):
                self.fx.empty["sum"]=value
                with self.assertRaises(InvalidDocument):
                    self.read()
        self.fx.empty["sum"]=0
        for rows in (({},),(dict(id=str(uuid4())),)):
            with self.subTest(rows=rows):
                self.fx.empty_rows=rows
                with self.assertRaises(InvalidDocument):
                    self.read()

    def test_zero_header_still_validates_party_currency_vat_and_posting(self):
        variants=(
            {"agent":ref("counterparty")},
            {"organization":ref("organization")},
            {"rate":{"currency":ref("currency"),"value":1}},
            {"vatEnabled":True,"vatIncluded":True},
            {"vatEnabled":True,"vatIncluded":False},
            {"vatEnabled":None},
            {"applicable":1},
            {"rate":{**self.fx.empty["rate"],"value":0}},
            {"attributes":{}},
            {"updated":None},
            {"updated":""},
            {"updated":"  "},
            {"updated":"x"*257},
        )
        original=copy.deepcopy(self.fx.empty)
        for fields in variants:
            with self.subTest(fields=fields):
                self.fx.empty={**copy.deepcopy(original),**fields}
                with self.assertRaises(InvalidDocument):
                    self.read()
        self.fx.empty=original

    def test_any_posted_allocation_to_zero_child_is_rejected_without_reusing_order_payment(self):
        for amount in (0,1):
            with self.subTest(amount=amount):
                self.fx.pay["sum"]=1200000+amount
                self.fx.pay["operations"]=[
                    {**ref("customerorder",self.fx.order),"linkedSum":1200000},
                    {**ref("demand",self.fx.empty_id),"linkedSum":amount},
                ]
                with self.assertRaises(InvalidDocument):
                    self.read()

    def test_zero_child_returns_retain_normal_reference_party_and_posting_checks(self):
        returned=self.fx.return_goods()
        self.fx.returns=[]
        returned["demand"]=ref("demand",self.fx.empty_id)
        self.fx.empty_returns=[returned]
        with self.assertRaises(InvalidDocument):
            self.read()
        self.fx.empty["applicable"]=False
        with self.assertRaisesRegex(InvalidDocument,"posted return references unposted shipment"):
            self.read()
        returned["applicable"]=False
        with self.assertRaises(InvalidDocument):
            self.read()
        returned["demand"]=ref("demand")
        with self.assertRaisesRegex(InvalidDocument,"return belongs to another shipment"):
            self.read()

    def test_empty_posting_change_between_graph_reads_is_a_conflict(self):
        self.fx.change_empty=True
        with self.assertRaises(RetryLater) as error:
            self.read()
        self.assertEqual(error.exception.code,"conflict")
