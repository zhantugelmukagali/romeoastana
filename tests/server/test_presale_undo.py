"""Presale proof, frozen inverse and exact basket restoration boundaries."""
import copy
import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from test_fulfillment import Fixture
from bonuspro.adapters.postgres.presale_undo import (
    original_financial_matches,inverse_state,PresaleUndoConflict,
)


class PresaleUndoProofTests(unittest.TestCase):
    def test_only_stable_unlinked_unpaid_unshipped_order_is_safe(self):
        fixture=Fixture()
        self.assertFalse(fixture.read().presale_safe)
        fixture.include_ship=fixture.include_pay=False
        self.assertTrue(fixture.read().presale_safe)
        for field in ("payedSum","shippedSum"):
            with self.subTest(field=field):
                fixture.doc[field]=1
                self.assertFalse(fixture.read().presale_safe)
                fixture.doc[field]=0
        for field in ("invoicesOut","prepayments"):
            with self.subTest(field=field):
                fixture.doc[field]=[{"id":str(uuid4())}]
                self.assertFalse(fixture.read().presale_safe)
                del fixture.doc[field]
        fixture.doc["applicable"]=False
        self.assertFalse(fixture.read().presale_safe)

    def test_unposted_link_is_not_presale_proof(self):
        fixture=Fixture()
        fixture.include_pay=False
        fixture.ship["applicable"]=False
        self.assertFalse(fixture.read().presale_safe)
        fixture.include_ship=False
        fixture.include_pay=True
        fixture.pay["applicable"]=False
        self.assertFalse(fixture.read().presale_safe)

    def original(self):
        fixture=Fixture()
        fixture.include_ship=fixture.include_pay=False
        return fixture.read().saved

    def test_recreated_ids_require_the_full_per_row_multiset(self):
        original=self.original().financial()
        replacement=copy.deepcopy(original)
        for row in replacement["lines"]:
            row["id"]=str(uuid4())
        replacement["lines"].reverse()
        self.assertTrue(original_financial_matches(replacement,original))
        replacement["lines"][0]["quantity"]="2"
        self.assertFalse(original_financial_matches(replacement,original))
        replacement=copy.deepcopy(original)
        replacement["lines"][1]["id"]=replacement["lines"][0]["id"]
        self.assertFalse(original_financial_matches(replacement,original))
        replacement=copy.deepcopy(original)
        replacement["lines"][1]["price"]="0"
        replacement["lines"][0]["price"]="1200000"
        self.assertFalse(original_financial_matches(replacement,original))
        for field in ("counterparty_id","organization_id","currency","vat_enabled"):
            replacement=copy.deepcopy(original)
            replacement[field]="changed"
            self.assertFalse(original_financial_matches(replacement,original))

    def test_inverse_is_absolute_and_removes_only_our_marker(self):
        saved=self.original()
        operation=dict(plan=dict(before=saved.financial(),after=saved.financial()),marker_id=str(uuid4()))
        update=inverse_state(saved,operation,restore_original=False)
        self.assertEqual(len(update["positions"]),len(saved.lines))
        self.assertEqual(update["attributes"],[{"id":operation["marker_id"],"value":None}])
        self.assertEqual(update["positions"],[{"id":line.id} for line in saved.lines])

    def test_changed_or_deleted_rows_require_explicit_restore(self):
        saved=self.original()
        saved=replace(saved,recreatable_ids=frozenset(line.id for line in saved.lines))
        operation=dict(plan=dict(before=saved.financial(),after=saved.financial(),
                               reconstruction=saved.reconstruction_manifest()),marker_id=str(uuid4()))
        changed=replace(saved,lines=(replace(saved.lines[0],quantity=Decimal(2)),saved.lines[1]))
        with self.assertRaises(PresaleUndoConflict) as rejected:
            inverse_state(changed,operation,restore_original=False)
        self.assertEqual(rejected.exception.reason,"undo_restore_original")
        update=inverse_state(changed,operation,restore_original=True)
        self.assertEqual(next(x for x in update["positions"] if x["id"]==saved.lines[0].id)["quantity"],1)
        deleted=replace(saved,lines=(saved.lines[1],))
        update=inverse_state(deleted,operation,restore_original=True)
        created=next(x for x in update["positions"] if "id" not in x)
        self.assertEqual(created["assortment"]["meta"]["type"],"product")
        self.assertTrue(created["assortment"]["meta"]["href"].endswith("/"+saved.lines[0].assortment))

    def test_reconstruction_manifest_requires_explicit_non_serial_non_marked_original_evidence(self):
        fixture=Fixture();fixture.include_ship=fixture.include_pay=False
        unexpanded=fixture.read().saved
        product=fixture.lines[0]
        self.assertNotIn(product["id"],unexpanded.recreatable_ids)
        self.assertIn(fixture.lines[1]["id"],unexpanded.recreatable_ids)
        product["assortment"].update(isSerialTrackable=False,trackingType="NOT_TRACKED")
        proven=fixture.read().saved
        self.assertIn(product["id"],proven.recreatable_ids)
        self.assertEqual(proven.financial(),unexpanded.financial())
        self.assertEqual(proven.source_hash,unexpanded.source_hash)
        plan=proven.plan(1000)
        self.assertEqual(plan["before"],unexpanded.financial())
        row=next(x for x in plan["reconstruction"]["rows"] if x["id"]==product["id"])
        self.assertTrue(row["recreatable"])
        for fields in ({"things":[]},{"trackingCodes":[]},{"unknown":None},{"reserve":1}):
            with self.subTest(fields=fields):
                original=copy.deepcopy(product);product.update(fields)
                self.assertNotIn(product["id"],fixture.read().saved.recreatable_ids)
                product.clear();product.update(original)
        for fields in ({"isSerialTrackable":True},{"trackingType":"TOBACCO"},
                       {"mod__marking__uz":{"trackingType":"LP_SHOES"}},
                       {"mod__unknown":{}}):
            with self.subTest(fields=fields):
                original=copy.deepcopy(product["assortment"]);product["assortment"].update(fields)
                self.assertNotIn(product["id"],fixture.read().saved.recreatable_ids)
                product["assortment"].clear();product["assortment"].update(original)

    def test_service_with_unfrozen_module_is_not_marked_recreatable(self):
        fixture=Fixture();fixture.include_ship=fixture.include_pay=False
        service=fixture.lines[1]
        self.assertIn(service["id"],fixture.read().saved.recreatable_ids)
        service["assortment"]["mod__unknown"]={}
        self.assertNotIn(service["id"],fixture.read().saved.recreatable_ids)

    def test_legacy_plan_missing_manifest_cannot_recreate_deleted_goods(self):
        saved=self.original()
        operation=dict(plan=dict(before=saved.financial(),after=saved.financial()),marker_id=str(uuid4()))
        with self.assertRaises(PresaleUndoConflict) as rejected:
            inverse_state(replace(saved,lines=(saved.lines[1],)),operation,restore_original=True)
        self.assertEqual(rejected.exception.reason,"undo_not_representable")

    def test_manually_restored_new_ids_preserve_unknown_fields_without_recreation(self):
        saved=self.original()
        operation=dict(plan=dict(before=saved.financial(),after=saved.financial()),marker_id=str(uuid4()))
        manual=replace(saved,lines=tuple(replace(line,id=str(uuid4())) for line in saved.lines))
        update=inverse_state(manual,operation,restore_original=False)
        self.assertEqual(update["positions"],[{"id":line.id} for line in manual.lines])
        self.assertEqual(update["attributes"],[{"id":operation["marker_id"],"value":None}])

    def test_deleted_packed_or_bundle_rows_fail_without_a_guessed_reconstruction(self):
        saved=self.original()
        for values in (dict(pack=str(uuid4())),dict(kind="bundle")):
            with self.subTest(values=values):
                original=replace(saved,lines=(replace(saved.lines[0],**values),saved.lines[1]))
                operation=dict(plan=dict(before=original.financial(),after=original.financial()),marker_id=str(uuid4()))
                with self.assertRaises(PresaleUndoConflict) as rejected:
                    inverse_state(replace(original,lines=(saved.lines[1],)),operation,restore_original=True)
                self.assertEqual(rejected.exception.reason,"undo_not_representable")
