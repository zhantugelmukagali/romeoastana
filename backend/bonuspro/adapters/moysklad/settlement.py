"""Cumulative return targets. Fractions avoid binary floats and repeated-return rounding."""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from collections import defaultdict
from bonuspro.domain.finance import Rules, natural
from bonuspro.adapters.postgres.ledger import fingerprint
from bonuspro.adapters.moysklad.remap import InvalidDocument
from bonuspro.adapters.moysklad.order import GOODS, rounded


def key(line):
    return (line.kind,line.assortment,str(line.price),line.discount_bps,str(line.vat),line.vat_enabled,line.pack)


@dataclass(frozen=True)
class Target:
    earn: int
    refund: int
    digest: str


def targets(proof,*,rules: Rules,attempt=None):
    saved=proof.saved
    basis=attempt["plan"].get("bonus_before",attempt["plan"]["before"]) if attempt else None
    before={x["id"]:x for x in basis["lines"]} if basis else {}
    spent=attempt["points"] if attempt else 0
    groups={}
    original_signatures=defaultdict(set)
    allocation_rates=defaultdict(set)
    for line in saved.lines:
        k=key(line)
        if k not in groups:
            groups[k]=[Fraction(0),0,0]
        quantity,original,discount=groups[k]
        original_line=before.get(line.id)
        if attempt and not original_line:
            raise InvalidDocument("missing original sale position")
        if original_line:
            # Old plans may have merged distinct original prices/discounts into
            # one saved basket key. A partial return cannot identify which
            # original earning base and bonus allocation actually came back.
            original_signatures[k].add((
                original_line["kind"],original_line["assortment"],Decimal(original_line["price"]),
                original_line["discount_bps"],Decimal(original_line["vat"]),
                original_line["vat_enabled"],original_line["pack"]))
        amount=rounded(Decimal(original_line["price"])*Decimal(original_line["quantity"])*
                       (10000-original_line["discount_bps"])/10000) if original_line else line.amount
        if amount<line.amount:
            raise InvalidDocument("negative bonus allocation")
        if attempt and line.kind in GOODS:
            # Equal prices do not imply equal allocations per returned unit:
            # each original and saved row is rounded independently.
            allocation_rates[k].add((Fraction(amount)/Fraction(line.quantity),
                                     Fraction(amount-line.amount)/Fraction(line.quantity)))
        groups[k]=[quantity+Fraction(line.quantity),original+amount,discount+amount-line.amount]
    returned=defaultdict(Fraction)
    returned_native=defaultdict(int)
    for line in proof.returned:
        k=key(line)
        returned[k]+=Fraction(line.quantity)
        returned_native[k]+=line.amount
    for k,quantity in returned.items():
        if k not in groups or quantity>groups[k][0]:
            raise InvalidDocument("return exceeds original sale quantities")
        if 0<quantity<groups[k][0]:
            if attempt and (len(original_signatures[k])>1 or
                            (groups[k][2]>0 and len(allocation_rates[k])>1)):
                raise InvalidDocument("partial return cannot identify original sale allocations")
            if k[0] in GOODS and groups[k][2]==0 and returned_native[k]>groups[k][1]:
                raise InvalidDocument("partial return amount exceeds original sale amount")
    remaining=Fraction(0);refund_minor=Fraction(0)
    for k,(quantity,original,discount) in groups.items():
        if k[0] not in GOODS:
            continue
        fraction=returned[k]/quantity
        if discount:
            remaining+=original*(1-fraction)
        elif returned[k]==0:
            remaining+=original
        elif returned[k]<quantity:
            # A group without bonus allocation keeps the provider's exact
            # rounded returned money, including when another group was redeemed.
            remaining+=original-returned_native[k]
        refund_minor+=discount*fraction
    if sum(x[2] for k,x in groups.items())!=spent*100:
        raise InvalidDocument("bonus allocation does not match journal")
    full=(proof.full_payment and proof.full_shipment) or proof.outcome=="eligible"
    earn=int(remaining*rules.earn_bps//1000000) if full and not saved.snapshot.cancelled else 0
    refund=spent if proof.cancel_safe else int(refund_minor//100)
    natural(earn,"earn");natural(refund,"refund",spent)
    evidence=dict(financial=saved.financial(),marker=saved.marker,
        returned=sorted([(list(k),str(v)) for k,v in returned.items()],key=lambda x:str(x[0])),
        full=full,cancel_safe=proof.cancel_safe,cancelled=saved.snapshot.cancelled,earn=earn,refund=refund)
    if not attempt:
        native_evidence=returned_native.items()
    else:
        native_evidence=((k,v) for k,v in returned_native.items()
                         if k[0] in GOODS and groups[k][2]==0)
    native_evidence=sorted([(list(k),v) for k,v in native_evidence],key=lambda x:str(x[0]))
    if not attempt or native_evidence:
        evidence["returned_native"]=native_evidence
    digest=fingerprint(evidence)
    return Target(earn,refund,digest)
