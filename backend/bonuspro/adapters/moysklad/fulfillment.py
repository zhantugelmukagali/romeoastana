"""Conservative proof of full payment AND full shipment from a stable document graph.

This is an internal adapter. Browser events and summary payedSum/shippedSum do not
authorize accrual. Invoice-only allocations and returns are held for reconciliation.
"""
from collections import defaultdict
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json,time
from hashlib import sha256
from bonuspro.adapters.moysklad.remap import OrderEvidence, InvalidDocument, RetryLater, reference, exact_minor, uuid
from bonuspro.adapters.moysklad.order import SavedOrder, normalize_order

MSK=timezone(timedelta(hours=3))


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


class FulfillmentReader:
    def __init__(self,client):
        self.client=client

    def graph(self,order_id):
        evidence=self.client.order_evidence(order_id)
        if len(evidence.demands)>20 or len(evidence.payments)>40:
            raise InvalidDocument("too many related documents for one reconciliation")
        demands=[]
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
            demands.append((demand,positions))
        return evidence,demands,payments

    def read(self,order_id,*,marker_id=None):
        start=time.monotonic()
        first=self.graph(order_id)
        second=self.graph(order_id)
        if first!=second:
            raise RetryLater(1,"conflict")
        if time.monotonic()-start>90:
            raise RetryLater(5,"provider_timeout")
        evidence,demands,payments=second
        doc=evidence.order
        saved=normalize_order(self.client,evidence,marker_id=marker_id,allow_fulfilled=True)
        created=moment(doc.get("created"))
        if saved.snapshot.cancelled:
            return Fulfillment(saved,created,0,0,"cancelled",digest(second),start+120)
        posted=[]
        paid=0
        has_returns=False
        for demand,rows in demands:
            if link(demand,"customerOrder",{"customerorder"})[1]!=saved.snapshot.order_id:
                raise InvalidDocument("shipment belongs to another order")
            party_check(demand,doc)
            returns=demand.get("returns",[])
            if not isinstance(returns,list):
                raise InvalidDocument("invalid return links")
            has_returns=has_returns or bool(returns)
            if not demand["applicable"]:
                continue
            normalized=normalize_order(self.client,OrderEvidence(
                {**demand,"payedSum":0,"shippedSum":0},tuple(rows),(),()),allow_fulfilled=True)
            if normalized.vat_included!=saved.vat_included or normalized.vat_enabled!=saved.vat_enabled:
                raise InvalidDocument("shipment tax mismatch")
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
                kind,identity=reference(operation.get("meta"),{
                    "customerorder","demand","invoiceout","retailshift","commissionreportout","prepayment"})
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
        shipped=sum(x.snapshot.total_minor for x in posted)
        actual=basket([line for demand in posted for line in demand.lines])
        expected=basket(saved.lines)
        if has_returns:
            outcome="return_review"
        elif actual!=expected:
            # Partial shipment waits; alien lines/over-shipment require review, never full earning.
            outcome="waiting_shipment" if all(k in expected and q<=expected[k] for k,q in actual.items()) else "document_review"
        elif shipped!=saved.snapshot.total_minor:
            outcome="document_review"
        elif paid<saved.snapshot.total_minor:
            outcome="waiting_payment"
        else:
            outcome="eligible"
        return Fulfillment(saved,created,paid,shipped,outcome,digest(second),start+120)
