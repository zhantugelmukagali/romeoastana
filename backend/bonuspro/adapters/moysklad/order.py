"""Authoritative saved-order normalization and exact discount/price plans.

No browser amount is trusted. Unsupported tax/rounding/assortment cases fail closed.
The UpdateRequest plan must be saved by MoySklad and read back before posting points.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from bonuspro.adapters.moysklad.remap import InvalidDocument, exact_minor, reference, uuid
from bonuspro.adapters.postgres.checkout import OrderSnapshot
from bonuspro.adapters.postgres.ledger import fingerprint
from bonuspro.domain.finance import allocate_discount, natural

GOODS = frozenset(("product", "variant", "bundle"))


def number(value, *, maximum=Decimal("9000000000000000")):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise InvalidDocument("exact numeric value required")
    value = Decimal(value)
    if not value.is_finite() or not 0 <= value <= maximum:
        raise InvalidDocument("numeric value out of bounds")
    return value.normalize()


def rounded(value):
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Line:
    id: str
    kind: str
    assortment: str
    quantity: Decimal
    price: Decimal
    discount_bps: int
    vat: Decimal
    vat_enabled: bool
    pack: str | None

    @property
    def amount(self):
        return rounded(self.price * self.quantity * (10000-self.discount_bps) / 10000)

    def record(self):
        return dict(id=self.id,kind=self.kind,assortment=self.assortment,quantity=str(self.quantity),
                    price=str(self.price),discount_bps=self.discount_bps,vat=str(self.vat),
                    vat_enabled=self.vat_enabled,pack=self.pack)


@dataclass(frozen=True)
class SavedOrder:
    snapshot: OrderSnapshot
    lines: tuple[Line, ...]
    organization_id: str
    vat_included: bool
    vat_enabled: bool
    marker: str | None
    name: str
    discount_blocked_reason: str | None = None

    def financial(self):
        s = self.snapshot
        return dict(order_id=s.order_id,counterparty_id=s.counterparty_id,
                    organization_id=self.organization_id,currency=s.currency,total_minor=s.total_minor,
                    vat_included=self.vat_included,vat_enabled=self.vat_enabled,
                    lines=[x.record() for x in sorted(self.lines,key=lambda line:line.id)])

    @property
    def source_hash(self):
        return fingerprint(dict(financial=self.financial(),revision=self.snapshot.revision,
                                paid=self.snapshot.paid_minor,shipped=self.snapshot.shipped_minor,
                                cancelled=self.snapshot.cancelled,marker=self.marker))

    def plan(self, points):
        natural(points,"points")
        if not points:
            raise InvalidDocument("positive redemption required")
        self.snapshot.validate_for_discount()
        if self.discount_blocked_reason:
            raise InvalidDocument("saved order is read-only",reason=self.discount_blocked_reason)
        if self.marker:
            raise InvalidDocument("order already carries a redemption marker",reason="order_has_marker")
        if points*100>self.snapshot.eligible_minor:
            raise InvalidDocument("discount exceeds eligible goods")
        allocation = allocate_discount(points*100,{x.id:x.amount for x in self.lines if x.kind in GOODS})
        result = []
        updates = []
        for line in self.lines:
            target = line.amount-allocation.get(line.id,0)
            changed = line
            if allocation.get(line.id,0):
                # Smallest additional 0.01% step reaching target, without approximation.
                lo,hi=line.discount_bps,10000
                while lo<hi:
                    mid=(lo+hi)//2
                    if replace(line,discount_bps=mid).amount>target:
                        lo=mid+1
                    else:
                        hi=mid
                changed=replace(line,discount_bps=lo)
                if changed.amount != target:
                    # Keep the ordinary discount and use an integral minor-unit
                    # price only when the provider percentage grid cannot fit.
                    lo,hi=0,int(line.price)
                    while lo<hi:
                        mid=(lo+hi+1)//2
                        candidate=replace(line,price=Decimal(mid).normalize())
                        if candidate.amount>target:
                            hi=mid-1
                        else:
                            lo=mid
                    changed=replace(line,price=Decimal(lo).normalize())
                    if changed.amount != target:
                        raise InvalidDocument("discount cannot represent requested points exactly",reason="discount_not_representable")
            result.append(changed)
            update={"id":line.id}
            if changed.discount_bps!=line.discount_bps:
                update["discount"]=changed.discount_bps/100  # wire Float; all decisions use integers
            if changed.price!=line.price:
                update["price"]=int(changed.price)
            updates.append(update)
        # Returns identify groups by the saved financial signature. Neither
        # percentage nor price changes may pool different original allocations.
        def signature(value):
            return (value.kind,value.assortment,value.price,value.discount_bps,
                    value.vat,value.vat_enabled,value.pack)
        groups={}
        for original,changed in zip(self.lines,result):
            groups.setdefault(signature(changed),set()).add(signature(original))
        if any(len(originals)>1 for originals in groups.values()):
            raise InvalidDocument("discount would merge distinct return allocations",reason="discount_not_representable")
        after=replace(self,lines=tuple(result),
                      snapshot=replace(self.snapshot,total_minor=self.snapshot.total_minor-points*100,
                                       eligible_minor=self.snapshot.eligible_minor-points*100))
        # ALL positions, including services, must remain in UpdateRequest (host replaces the list).
        return dict(before=self.financial(),after=after.financial(),source_hash=self.source_hash,
                    update_state={"positions":updates})


def read_order(client, order_id, *, marker_id=None, allow_fulfilled=False):
    return normalize_order(client,client.order_evidence(order_id),marker_id=marker_id,
                           allow_fulfilled=allow_fulfilled)


def normalize_order(client,evidence,*,marker_id=None,allow_fulfilled=False):
    doc=evidence.order
    # Conservative initial supported mode: VAT included in prices, or VAT disabled.
    vat_enabled=doc.get("vatEnabled")
    # REMAP may omit vatIncluded when VAT is disabled. Preserve explicit flags:
    # immutable legacy plans compare their original financial representation.
    vat_included=doc.get("vatIncluded",True) if vat_enabled is False else doc.get("vatIncluded")
    if type(vat_enabled) is not bool or type(vat_included) is not bool:
        raise InvalidDocument("tax mode missing",reason="vat_mode_missing")
    if vat_enabled and not vat_included:
        raise InvalidDocument("VAT exclusive prices are not supported for redemption",reason="vat_mode_unsupported")
    if not isinstance(doc.get("agent"),dict) or not isinstance(doc["agent"].get("meta"),dict):
        raise InvalidDocument("saved customer is missing",reason="customer_missing")
    _,customer=reference(doc.get("agent",{}).get("meta"),{"counterparty"})
    _,organization=reference(doc.get("organization",{}).get("meta"),{"organization"})
    _,currency_id=reference(doc.get("rate",{}).get("currency",{}).get("meta"),{"currency"})
    currency=client.entity("currency",currency_id)
    if currency.get("isoCode")!="KZT":
        raise InvalidDocument("only KZT is supported",reason="currency_unsupported")
    rate=doc.get("rate",{}).get("value")
    if rate is not None and number(rate)!=1:
        raise InvalidDocument("unexpected KZT exchange rate",reason="exchange_rate_unsupported")
    if type(doc.get("applicable")) is not bool:
        raise InvalidDocument("posting status missing")
    lines=[]
    seen=set()
    for raw in evidence.positions:
        identity=uuid(raw.get("id"))
        if identity in seen:
            raise InvalidDocument("duplicate position")
        seen.add(identity)
        kind,assortment=reference(raw.get("assortment",{}).get("meta"),GOODS|{"service"})
        quantity=number(raw.get("quantity"),maximum=Decimal("1000000000"))
        price=number(raw.get("price"))
        if quantity==0 or -quantity.as_tuple().exponent>6 or -price.as_tuple().exponent>2:
            raise InvalidDocument("unsupported quantity or price precision",reason="position_precision_unsupported")
        discount=number(raw.get("discount"),maximum=Decimal(100))*100
        if discount!=discount.to_integral_value():
            raise InvalidDocument("discount precision exceeds two decimals",reason="position_precision_unsupported")
        vat=number(raw.get("vat"),maximum=Decimal(100))
        line_vat=raw.get("vatEnabled",vat_enabled)
        if type(line_vat) is not bool:
            raise InvalidDocument("invalid position tax flag")
        pack=raw.get("pack")
        pack_id=uuid(pack.get("id")) if isinstance(pack,dict) else None
        if pack is not None and not isinstance(pack,dict):
            raise InvalidDocument("invalid pack")
        lines.append(Line(identity,kind,assortment,quantity,price,int(discount),vat,line_vat,pack_id))
    if not lines:
        raise InvalidDocument("saved positions are missing",reason="positions_missing")
    if len(lines)>1000:
        raise InvalidDocument("too many saved positions",reason="positions_limit")
    total=exact_minor(doc.get("sum"))
    if sum(x.amount for x in lines)!=total:
        raise InvalidDocument("position rounding does not match saved order sum",reason="totals_mismatch")
    paid=exact_minor(doc.get("payedSum"))
    shipped=exact_minor(doc.get("shippedSum"))
    # Any linked document blocks editing, even a draft: staff must settle that workflow first.
    if not allow_fulfilled and (evidence.payments or evidence.demands):
        raise InvalidDocument("order already has linked payment or shipment documents",reason="linked_documents_present")
    marker=None
    attrs=doc.get("attributes",[])
    if not isinstance(attrs,list):
        raise InvalidDocument("invalid attributes")
    if marker_id:
        matches=[a for a in attrs if isinstance(a,dict) and
                 (a.get("id")==marker_id or a.get("meta",{}).get("href","").endswith("/"+marker_id))]
        if len(matches)>1:
            raise InvalidDocument("duplicate marker")
        if matches:
            marker=matches[0].get("value")
            if marker is not None and not isinstance(marker,str):
                raise InvalidDocument("invalid redemption marker")
    revision=doc.get("updated")
    if not isinstance(revision,str) or not revision.strip() or len(revision)>256:
        raise InvalidDocument("missing saved revision")
    snapshot=OrderSnapshot(client.tenant,uuid(doc.get("id")),customer,revision,"KZT",total,
                           sum(x.amount for x in lines if x.kind in GOODS),paid,shipped,
                           not doc["applicable"])
    blocked=("order_not_posted" if snapshot.cancelled else
             "order_fulfilled" if paid or shipped else
             "linked_documents_present" if evidence.payments or evidence.demands else
             "order_has_marker" if marker else None)
    return SavedOrder(snapshot,tuple(lines),organization,vat_included,vat_enabled,marker,
                      str(doc.get("name",""))[:256],blocked)
