"""Read-only checks against the installed account; never financial acceptance.

Run inside the application container. Output contains finite codes and counts only:
no credentials, provider documents, prices, or customer/order identifiers.
"""
import argparse
from collections import Counter
import json

from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader
from bonuspro.adapters.moysklad.remap import (
    RemapClient, InvalidDocument, RetryLater, AccessDenied, EntityMissing, RemapError, reference, uuid,
)
from bonuspro.adapters.postgres.ledger import Ledger, uuid_value
from bonuspro.adapters.postgres.readiness import Readiness
from bonuspro.adapters.postgres.redemptions import Redemptions
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.security import AuthenticationFailed
from bonuspro.settings import Settings

REASONS=frozenset((
    "vat_mode_missing","vat_mode_unsupported","customer_missing","currency_unsupported",
    "exchange_rate_unsupported","positions_missing","positions_limit",
    "position_precision_unsupported","totals_mismatch","linked_documents_present",
    "order_has_marker","saved_order_invalid",
))
OUTCOMES=frozenset(("eligible","waiting_payment","waiting_shipment","cancelled",
                    "document_review","return_review"))
DETAILS={
    "shipment positions mismatch":"shipment_mismatch",
    "shipment tax mismatch":"shipment_tax_mismatch",
    "return tax mismatch":"return_tax_mismatch",
    "payment allocation is missing or too large":"payment_allocation_invalid",
    "payment allocations exceed payment":"payment_allocation_invalid",
    "duplicate payment allocation":"payment_allocation_invalid",
    "related document party mismatch":"related_party_mismatch",
    "related document currency mismatch":"related_currency_mismatch",
    "too many related documents for one reconciliation":"related_document_limit",
}
def failure_code(error):
    if isinstance(error,AccessDenied):return "provider_access_denied"
    if isinstance(error,AuthenticationFailed):return "installation_access_unavailable"
    if isinstance(error,RetryLater):return "provider_retry_required"
    if isinstance(error,EntityMissing):return "provider_document_missing"
    if isinstance(error,InvalidDocument):
        # Match exact static adapter messages, never reflect arbitrary exception text.
        detail=DETAILS.get(str(error))
        if detail:return detail
        reason=getattr(error,"reason",None)
        return reason if reason in REASONS else "saved_order_invalid"
    if isinstance(error,RemapError):return "provider_response_invalid"
    return "check_unavailable"

def recent_orders(client,limit):
    page=client._get("/entity/customerorder",{"limit":limit,"offset":0,"order":"updated,desc"})
    if not isinstance(page,dict):raise InvalidDocument()
    meta=page.get("meta")
    if not isinstance(meta,dict):raise InvalidDocument()
    rows=page.get("rows");size=meta.get("size")
    if not isinstance(rows,list) or len(rows)>limit or type(size) is not int or size<0 or len(rows)!=min(size,limit):
        raise InvalidDocument()
    result=[]
    for row in rows:
        if not isinstance(row,dict):raise InvalidDocument()
        identity=uuid(row.get("id"))
        if (uuid(row.get("accountId"))!=client.tenant or
            reference(row.get("meta"),{"customerorder"})!=("customerorder",identity) or identity in result):
            raise InvalidDocument()
        result.append(identity)
    return result

def check_account(ledger,installations,tenant,*,sample_limit=3,client_factory=RemapClient):
    tenant=uuid_value(tenant)
    if type(sample_limit) is not int or not 1<=sample_limit<=5:
        raise ValueError("sample limit must be 1..5")
    report=dict(read_only=True,business_acceptance_verified=False,
                operational_ready=False,ready_for_live_acceptance=False,
                sampled_orders=0,outcomes={},failures=[])
    def fail(stage,error):report["failures"].append(dict(stage=stage,code=failure_code(error)))
    try:
        readiness=Readiness(ledger).report(tenant)
        report["checks"]=[dict(id=x["id"],complete=x["complete"]) for x in readiness["checks"]]
        report["attention"]=readiness.get("attention",{})
        report["operational_ready"]=bool(readiness["ready"])
        binding=Redemptions(ledger).binding(tenant)
        if binding is None:
            report["operational_ready"]=False
            report["failures"].append(dict(stage="widget",code="widget_not_configured"))
            return report
        token=installations.remap_token(tenant)
        client=client_factory(token,tenant,budget_seconds=90,minimum_interval=0.5)
    except Exception as error:
        fail("configuration",error);report["operational_ready"]=False;return report
    try:
        if client.marker_attribute(create=False)!=binding:
            report["failures"].append(dict(stage="widget",code="widget_binding_mismatch"))
            report["operational_ready"]=False
            return report
    except Exception as error:
        fail("widget",error);report["operational_ready"]=False;return report
    try:orders=recent_orders(client,sample_limit)
    except Exception as error:
        fail("discovery",error);return report
    outcomes=Counter()
    for order in orders:
        try:
            proof=FulfillmentReader(client).read(order,marker_id=binding)
            report["sampled_orders"]+=1
            outcome=proof.outcome if proof.outcome in OUTCOMES else "document_review"
            outcomes[outcome]+=1
        except Exception as error:
            report["sampled_orders"]+=1
            fail("order_graph",error)
            # Stop bounded probing after provider throttling/access loss.
            if isinstance(error,(RetryLater,AccessDenied,AuthenticationFailed)):break
    report["outcomes"]=dict(outcomes)
    report["ready_for_live_acceptance"]=bool(report["operational_ready"] and
        report["sampled_orders"] and not report["failures"] and not outcomes["document_review"])
    return report

def main():
    parser=argparse.ArgumentParser(description="Read-only installed-account checks; no financial operations.")
    parser.add_argument("--tenant",required=True)
    parser.add_argument("--sample-limit",type=int,default=3,choices=range(1,6))
    args=parser.parse_args()
    try:
        config=Settings.from_env()
        ledger=Ledger(config.database_url)
        installations=Installations(config.database_url,config.lifecycle_database_url,config.credentials)
        report=check_account(ledger,installations,args.tenant,sample_limit=args.sample_limit)
    except Exception as error:
        report=dict(read_only=True,business_acceptance_verified=False,
                    operational_ready=False,ready_for_live_acceptance=False,
                    failures=[dict(stage="configuration",code=failure_code(error))])
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report["ready_for_live_acceptance"] else 1)

if __name__=="__main__":main()
