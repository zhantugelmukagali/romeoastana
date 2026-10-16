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
        self.lines[0]["price"]=3000000;self.doc["sum"]=3200000
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

if __name__=="__main__":unittest.main()
