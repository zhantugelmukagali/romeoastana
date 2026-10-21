"""Historical classification needs stable owned metadata, never a financial proof."""
import copy
import unittest
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader,historical_marker_clear
from bonuspro.adapters.moysklad.remap import BASE,InvalidDocument,RetryLater,OrderEvidence


class HistoricalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.order,self.marker,self.generation=(str(uuid4()) for _ in range(4))
        self.cutoff=datetime(2026,10,21,10,tzinfo=timezone.utc)
        self.doc=dict(id=self.order,accountId=self.tenant,
            meta={"type":"customerorder","href":BASE+"/entity/customerorder/"+self.order},
            created="2026-10-20 12:00:00.000",updated="2026-10-21 14:00:00.000",attributes=[])
        self.reads=0
    def entity(self,kind,identity):
        self.assertEqual((kind,identity),("customerorder",self.order))
        self.reads+=1
        return copy.deepcopy(self.doc)
    def proof(self,entity=None):
        client=SimpleNamespace(tenant=self.tenant,entity=entity or self.entity)
        reader=FulfillmentReader(client)
        reader.graph=lambda identity:(OrderEvidence(copy.deepcopy(self.doc),(),(),()),[],{})
        with patch("bonuspro.adapters.moysklad.fulfillment.normalize_order",side_effect=InvalidDocument()):
            with self.assertRaises(InvalidDocument):reader.read(self.order,marker_id=self.marker)
        return reader.historical(self.order,before=self.cutoff,
            generation=self.generation,marker_id=self.marker)
    def attr(self,identity=None,**extra):
        return dict(id=identity or self.marker,value="bonuspro:v1:synthetic",**extra)
    def href(self,identity=None):
        return BASE+"/entity/customerorder/metadata/attributes/"+(identity or self.marker)

    def test_old_recently_edited_order_reuses_stable_rawgraph_before_financial_parse_failure(self):
        proof=self.proof()
        self.assertEqual(self.reads,2)
        self.assertEqual((proof.tenant_id,proof.order_id,proof.generation,proof.cutoff),
                         (self.tenant,self.order,self.generation,self.cutoff))
        self.assertLess(proof.created_at,self.cutoff)
        self.assertEqual(proof.revision,self.doc["updated"])

    def test_current_creation_or_any_own_marker_cannot_be_classified_historical(self):
        self.doc["created"]="2026-10-21 14:00:00.000"
        self.assertIsNone(self.proof())
        self.doc["created"]="2026-10-20 12:00:00.000"
        for attributes in ([self.attr()],[self.attr(identity=self.marker.upper())],
            [{"meta":{"type":"attributemetadata","href":self.href()},"value":"bonuspro:v1:synthetic"}],
            [dict(id=self.marker,value="")]):
            with self.subTest(attributes=attributes):
                self.doc["attributes"]=attributes
                self.assertIsNone(self.proof())

    def test_explicit_cleared_marker_and_unrelated_valid_attributes_are_safe(self):
        other=str(uuid4())
        self.doc["attributes"]=[dict(id=other,value={"custom":"value"}),
            {"meta":{"type":"attributemetadata","href":self.href()},"value":None}]
        self.assertIsNotNone(self.proof())

    def test_noncanonical_or_ambiguous_marker_references_never_prove_absence(self):
        for href in (self.href()+"?value=hidden",self.href()+"/",self.href(self.marker.upper()),
                     self.href()+"#fragment","https://untrusted.invalid/"+self.marker):
            with self.subTest(href=href):
                self.doc["attributes"]=[{"meta":{"type":"attributemetadata","href":href},"value":None}]
                with self.assertRaises(InvalidDocument):self.proof()
        for attributes in (
            [dict(id=str(uuid4()),meta={"href":self.href()},value=None)],
            [dict(id=self.marker,value=None),dict(id=self.marker.upper(),value=None)],
            [{"id":self.marker}], [{"name":"unidentified","value":None}]):
            with self.subTest(attributes=attributes):
                self.doc["attributes"]=attributes
                with self.assertRaises(InvalidDocument):self.proof()

    def test_wrong_identity_or_timestamps_and_unstable_reads_fail_closed(self):
        for fields in (dict(accountId=str(uuid4())),dict(id=str(uuid4())),
                       dict(created="invalid"),dict(updated="2019-01-01 00:00:00.000")):
            original=copy.deepcopy(self.doc);self.doc.update(fields)
            with self.subTest(fields=fields),self.assertRaises(InvalidDocument):self.proof()
            self.doc=original
        def changing(kind,identity):
            result=self.entity(kind,identity)
            if self.reads==2:result["updated"]="2026-10-21 14:00:01.000"
            return result
        self.reads=0
        with self.assertRaises(RetryLater) as error:self.proof(changing)
        self.assertEqual(error.exception.code,"conflict")
        with patch("bonuspro.adapters.moysklad.fulfillment.time.monotonic",side_effect=[0,0,0,31]):
            with self.assertRaises(RetryLater):self.proof()

    def test_absent_or_incomplete_graph_and_any_return_bearing_path_cannot_be_historical(self):
        client=SimpleNamespace(tenant=self.tenant,entity=self.entity)
        reader=FulfillmentReader(client)
        self.assertIsNone(reader.historical(self.order,before=self.cutoff,
            generation=self.generation,marker_id=self.marker))
        self.assertEqual(self.reads,0)
        def incomplete(identity):raise InvalidDocument("incomplete graph")
        reader.graph=incomplete
        with self.assertRaises(InvalidDocument):reader.read(self.order,marker_id=self.marker)
        self.assertIsNone(reader.historical(self.order,before=self.cutoff,
            generation=self.generation,marker_id=self.marker))
        for field in ("demands","returns","salesReturns"):
            for value in ([{"meta":{"type":"demand","href":BASE+"/entity/demand/"+str(uuid4())}}],None,{}):
                with self.subTest(field=field,value=value):
                    self.doc[field]=value
                    self.assertIsNone(self.proof())
                self.doc.pop(field)
        demand={"id":str(uuid4())}
        def graph(identity):
            return OrderEvidence(copy.deepcopy(self.doc),(),(),(demand,)),[],{}
        reader.graph=graph
        with patch("bonuspro.adapters.moysklad.fulfillment.normalize_order",side_effect=InvalidDocument()):
            with self.assertRaises(InvalidDocument):reader.read(self.order,marker_id=self.marker)
        self.assertIsNone(reader.historical(self.order,before=self.cutoff,
            generation=self.generation,marker_id=self.marker))
        self.assertEqual(self.reads,0)

    def test_fresh_metadata_must_match_the_already_observed_fullgraph(self):
        def newer(kind,identity):
            result=self.entity(kind,identity)
            result["updated"]="2026-10-21 14:00:01.000"
            return result
        with self.assertRaises(RetryLater) as error:self.proof(newer)
        self.assertEqual(error.exception.code,"conflict")
