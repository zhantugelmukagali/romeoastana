"""Conservative proof of full payment AND full shipment from a stable document graph.

This is an internal adapter. Browser events and summary payedSum/shippedSum do not
authorize accrual. Invoice-only allocations and returns are held for reconciliation.
"""
from collections import defaultdict
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
import json,time
from hashlib import sha256
from bonuspro.adapters.moysklad.remap import OrderEvidence, InvalidDocument, RetryLater, reference, exact_minor, uuid, BASE
from bonuspro.adapters.moysklad.order import SavedOrder, normalize_order

MSK=timezone(timedelta(hours=3))
# REMAP paymentin/cashin document links; unrelated allocations are validated,
# but only this order and its posted demands can prove this order's payment.
INCOMING_ALLOCATIONS=frozenset((
    "customerorder","purchasereturn","demand","invoiceout","commissionreportin","retailshift",
))


def moment(value):
    if not isinstance(value,str):
        raise InvalidDocument("missing provider creation timestamp")
    try:
        result=datetime.strptime(value,"%Y-%m-%d %H:%M:%S.%f") if "." in value else datetime.strptime(value,"%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise InvalidDocument("invalid provider timestamp") from None
    return result.replace(tzinfo=MSK).astimezone(timezone.utc)


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(",",":"),default=lambda x:asdict(x) if is_dataclass(x) else str(x)).encode()).hexdigest()


def link(doc,field,kinds):
    return reference(doc.get(field,{}).get("meta"),kinds)


def party_check(doc,order):
    for field,kind in (("agent","counterparty"),("organization","organization")):
        if link(doc,field,{kind})!=link(order,field,{kind}):
            raise InvalidDocument("related document party mismatch")
    if link(doc.get("rate",{}),"currency",{"currency"})!=link(order.get("rate",{}),"currency",{"currency"}):
        raise InvalidDocument("related document currency mismatch")


def tax_modes_match(first,second):
    # vatIncluded is inert when VAT is disabled and may be absent in REMAP.
    # Keep each document's financial representation unchanged for saved plans.
    return (first.vat_enabled==second.vat_enabled and
            (not first.vat_enabled or first.vat_included==second.vat_included))


def basket(lines):
    result=defaultdict(Decimal)
    for x in lines:
        key=(x.kind,x.assortment,x.price,x.discount_bps,x.vat,x.vat_enabled,x.pack)
        result[key]+=x.quantity
    return dict(result)


@dataclass(frozen=True)
class Fulfillment:
    saved: SavedOrder
    created_at: datetime
    paid_minor: int
    shipped_minor: int
    outcome: str
    evidence_hash: str
    deadline: float
    returned: tuple = ()
    full_shipment: bool = False
    full_payment: bool = False
    cancel_safe: bool = False
    related: tuple = ()
    presale_safe: bool = False


def historical_marker_clear(doc,marker_id):
    attributes=doc.get("attributes",[])
    if not isinstance(attributes,list):
        raise InvalidDocument("historical marker metadata is invalid")
    matches=[]
    prefix=BASE+"/entity/customerorder/metadata/attributes/"
    for item in attributes:
        if not isinstance(item,dict):
            raise InvalidDocument("historical marker metadata is invalid")
        identities=set()
        if "id" in item:
            identities.add(uuid(item["id"]))
        if "meta" in item:
            meta=item["meta"]
            if not isinstance(meta,dict) or not isinstance(meta.get("href"),str):
                raise InvalidDocument("historical marker reference is invalid")
            href=meta["href"]
            try:
                parsed=urlsplit(href)
                identity=uuid(parsed.path.rsplit("/",1)[-1])
            except ValueError:
                raise InvalidDocument("historical marker reference is invalid") from None
            if (parsed.query or parsed.fragment or href!=prefix+identity
                or meta.get("type","attributemetadata")!="attributemetadata"):
                raise InvalidDocument("historical marker reference is ambiguous")
            identities.add(identity)
        if len(identities)!=1:
            raise InvalidDocument("historical marker identity is ambiguous")
        if marker_id in identities:
            if "value" not in item:
                raise InvalidDocument("historical marker value is unavailable")
            matches.append(item["value"])
    if len(matches)>1:
        raise InvalidDocument("historical marker is ambiguous")
    return not matches or matches[0] is None


@dataclass(frozen=True)
class HistoricalOrderEvidence:
    tenant_id: str
    order_id: str
    created_at: datetime
    revision: str
    generation: str
    cutoff: datetime
    marker_id: str
    evidence_hash: str
    deadline: float


class FulfillmentReader:
    def __init__(self,client):
        self.client=client
        self._historical_observation=None

    def historical(self,order_id,*,before,generation,marker_id):
        """Classify only a previously stable graph; never bypass financial parsing."""
        observed=self._historical_observation
        if observed is None:
            return None
        evidence,demands,_=observed
        # Legacy return cases have no order_id. Any return-bearing path remains
        # under ordinary review rather than being hidden by the cutoff.
        if evidence.demands or demands:
            return None
        observed_order=evidence.order
        for field in ("demands","returns","salesReturns"):
            refs=observed_order.get(field,[])
            if not isinstance(refs,list) or refs:
                return None
        start=time.monotonic()
        order_id=uuid(order_id);generation=uuid(generation);marker_id=uuid(marker_id)
        def read():
            doc=self.client.entity("customerorder",order_id)
            if (not isinstance(doc,dict) or uuid(doc.get("id"))!=order_id
                or uuid(doc.get("accountId"))!=self.client.tenant
                or reference(doc.get("meta"),{"customerorder"})!=("customerorder",order_id)):
                raise InvalidDocument("historical order identity mismatch")
            created=moment(doc.get("created"));updated=moment(doc.get("updated"))
            if updated<created:
                raise InvalidDocument("historical order revision precedes creation")
            clear=historical_marker_clear(doc,marker_id)
            return doc,created,clear
        first=read()
        if first[1]>=before or not first[2]:
            return None
        second=read()
        if first!=second or second[0]!=observed_order:
            raise RetryLater(1,"conflict")
        if time.monotonic()>start+30:
            raise RetryLater(5,"provider_timeout")
        doc,created,_=second
        evidence=digest(dict(tenant=self.client.tenant,order=order_id,created=doc["created"],
                             revision=doc["updated"],marker_id=marker_id,marker=None))
        return HistoricalOrderEvidence(self.client.tenant,order_id,created,doc["updated"],
            generation,before,marker_id,evidence,start+30)

    def graph(self,order_id):
        evidence=self.client.order_evidence(order_id)
        if len(evidence.demands)>20 or len(evidence.payments)>40:
            raise InvalidDocument("too many related documents for one reconciliation")
        demands=[]
        seen_returns=set()
        payments={(p["meta"]["type"]+"/"+uuid(p["id"])):p for p in evidence.payments}
        for demand in evidence.demands:
            did=uuid(demand["id"])
            positions=self.client.positions("demand",did)
            for payment in self.client.linked(demand,"payments",{"paymentin","cashin"}):
                key=payment["meta"]["type"]+"/"+uuid(payment["id"])
                if key in payments and payments[key]!=payment:
                    raise RetryLater(1,"conflict")
                payments[key]=payment
            if len(payments)>40:
                raise InvalidDocument("too many payments")
            returns=[]
            for returned in self.client.linked(demand,"returns",{"salesreturn"}):
                rid=uuid(returned["id"])
                if rid in seen_returns or len(seen_returns)>=40:
                    raise InvalidDocument("duplicate or excessive returns")
                seen_returns.add(rid)
                returns.append((returned,self.client.positions("salesreturn",rid)))
            demands.append((demand,positions,returns))
        return evidence,demands,payments

    def read(self,order_id,*,marker_id=None):
        self._historical_observation=None
        start=time.monotonic()
        first=self.graph(order_id)
        second=self.graph(order_id)
        if first!=second:
            raise RetryLater(1,"conflict")
        if time.monotonic()-start>90:
            raise RetryLater(5,"provider_timeout")
        self._historical_observation=second
        evidence,demands,payments=second
        doc=evidence.order
        saved=normalize_order(self.client,evidence,marker_id=marker_id,allow_fulfilled=True)
        created=moment(doc.get("created"))
        posted=[]
        paid=0
        linked_paid=0
        returned_lines=[]
        for demand,rows,returns in demands:
            if link(demand,"customerOrder",{"customerorder"})[1]!=saved.snapshot.order_id:
                raise InvalidDocument("shipment belongs to another order")
            party_check(demand,doc)
            normalized=normalize_order(self.client,OrderEvidence(
                {**demand,"payedSum":0,"shippedSum":0},tuple(rows),(),()),allow_fulfilled=True)
            if not tax_modes_match(normalized,saved):
                raise InvalidDocument("shipment tax mismatch")
            demand_returns=[]
            for returned,positions in returns:
                if link(returned,"demand",{"demand"})[1]!=uuid(demand["id"]):
                    raise InvalidDocument("return belongs to another shipment")
                party_check(returned,doc)
                if not returned["applicable"]:
                    continue
                if not demand["applicable"]:
                    raise InvalidDocument("posted return references unposted shipment")
                value=normalize_order(self.client,OrderEvidence(
                    {**returned,"payedSum":0,"shippedSum":0},tuple(positions),(),()),allow_fulfilled=True)
                if not tax_modes_match(value,saved):
                    raise InvalidDocument("return tax mismatch")
                demand_returns.extend(value.lines)
            shipped_basket=basket(normalized.lines)
            if any(k not in shipped_basket or qty>shipped_basket[k] for k,qty in basket(demand_returns).items()):
                raise InvalidDocument("returned quantity exceeds matching shipment")
            returned_lines.extend(demand_returns)
            if demand["applicable"]:
                posted.append(normalized)
        posted_ids={x.snapshot.order_id for x in posted}
        for payment in payments.values():
            party_check(payment,doc)
            if not payment["applicable"]:
                continue
            operations=payment.get("operations")
            if not isinstance(operations,list) or len(operations)>100:
                raise InvalidDocument("payment allocation is missing or too large")
            allocated=0;counted=0;seen=set()
            for operation in operations:
                kind,identity=reference(operation.get("meta"),INCOMING_ALLOCATIONS)
                if (kind,identity) in seen:
                    raise InvalidDocument("duplicate payment allocation")
                seen.add((kind,identity))
                amount=exact_minor(operation.get("linkedSum"))
                allocated+=amount
                if (kind=="customerorder" and identity==saved.snapshot.order_id) or (kind=="demand" and identity in posted_ids):
                    counted+=amount
            if allocated>exact_minor(payment["sum"]):
                raise InvalidDocument("payment allocations exceed payment")
            paid+=counted
            related={uuid(x[0]["id"]) for x in demands}
            linked_paid+=sum(exact_minor(o["linkedSum"]) for o in operations
                if reference(o.get("meta"),INCOMING_ALLOCATIONS) in
                ({("customerorder",saved.snapshot.order_id)}|{("demand",i) for i in related}))
        shipped=sum(x.snapshot.total_minor for x in posted)
        actual=basket([line for demand in posted for line in demand.lines])
        expected=basket(saved.lines)
        if saved.snapshot.cancelled:
            outcome="cancelled"
        elif actual!=expected:
            # Partial shipment waits; alien lines/over-shipment require review, never full earning.
            outcome="waiting_shipment" if all(k in expected and q<=expected[k] for k,q in actual.items()) else "document_review"
        elif shipped!=saved.snapshot.total_minor:
            outcome="document_review"
        elif paid<saved.snapshot.total_minor:
            outcome="waiting_payment"
        else:
            outcome="return_review" if returned_lines else "eligible"
        related={("customerorder",saved.snapshot.order_id)}
        related.update(("demand",uuid(d["id"])) for d,_,_ in demands)
        related.update((p["meta"]["type"],uuid(p["id"])) for p in payments.values())
        related.update(("salesreturn",uuid(r["id"])) for _,_,returns in demands for r,_ in returns)
        return Fulfillment(saved,created,paid,shipped,outcome,digest(second),start+120,
                           tuple(returned_lines),actual==expected and shipped==saved.snapshot.total_minor,
                           paid>=saved.snapshot.total_minor,
                           saved.snapshot.cancelled and not posted and linked_paid==0
                           and not doc.get("invoicesOut") and not doc.get("prepayments"),tuple(sorted(related)),
                           presale_safe=not saved.snapshot.cancelled and not saved.snapshot.paid_minor
                           and not saved.snapshot.shipped_minor and paid==0 and shipped==0
                           and not demands and not payments and not returned_lines
                           and not doc.get("invoicesOut") and not doc.get("prepayments"))
