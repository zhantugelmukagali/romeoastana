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
    before={x["id"]:x for x in attempt["plan"]["before"]["lines"]} if attempt else {}
    spent=attempt["points"] if attempt else 0
    groups={}
    for line in saved.lines:
        k=key(line)
        if k not in groups:
            groups[k]=[Fraction(0),0,0]
        quantity,original,discount=groups[k]
        original_line=before.get(line.id)
        if attempt and not original_line:
            raise InvalidDocument("missing original sale position")
        amount=rounded(Decimal(original_line["price"])*Decimal(original_line["quantity"])*
                       (10000-original_line["discount_bps"])/10000) if original_line else line.amount
        if amount<line.amount:
            raise InvalidDocument("negative bonus allocation")
        groups[k]=[quantity+Fraction(line.quantity),original+amount,discount+amount-line.amount]
    returned=defaultdict(Fraction)
    for line in proof.returned:
        returned[key(line)]+=Fraction(line.quantity)
    for k,quantity in returned.items():
        if k not in groups or quantity>groups[k][0]:
            raise InvalidDocument("return exceeds original sale quantities")
    remaining=Fraction(0);refund_minor=Fraction(0)
    for k,(quantity,original,discount) in groups.items():
        if k[0] not in GOODS:
            continue
        fraction=returned[k]/quantity
        remaining+=original*(1-fraction)
        refund_minor+=discount*fraction
    if sum(x[2] for k,x in groups.items())!=spent*100:
        raise InvalidDocument("bonus allocation does not match journal")
    full=(proof.full_payment and proof.full_shipment) or proof.outcome=="eligible"
    earn=int(remaining*rules.earn_bps//1000000) if full and not saved.snapshot.cancelled else 0
    refund=spent if proof.cancel_safe else int(refund_minor//100)
    natural(earn,"earn");natural(refund,"refund",spent)
    digest=fingerprint(dict(financial=saved.financial(),marker=saved.marker,
        returned=sorted([(list(k),str(v)) for k,v in returned.items()],key=lambda x:str(x[0])),
        full=full,cancel_safe=proof.cancel_safe,cancelled=saved.snapshot.cancelled,earn=earn,refund=refund))
    return Target(earn,refund,digest)
