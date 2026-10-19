"""Shop configuration and checkout preparation; never applies a provider discount.

OrderSnapshot is an INTERNAL input from the future authoritative document/eligibility
adapter, not a browser request model or a proof of payment. No endpoint accepts these
amounts. Preview is informational, not a reservation and not authorization to debit.
"""
from dataclasses import asdict, dataclass
from uuid import uuid4

from bonuspro.domain.finance import Rules, natural, quote, spend_limit
from bonuspro.adapters.postgres.ledger import (
    Ledger, Conflict, NotFound, fingerprint, uuid_value, text_value,
)


@dataclass(frozen=True)
class OrderSnapshot:
    tenant_id: str
    order_id: str
    counterparty_id: str
    revision: str
    currency: str
    total_minor: int
    eligible_minor: int
    paid_minor: int = 0
    shipped_minor: int = 0
    cancelled: bool = False

    def __post_init__(self):
        for field in ("tenant_id", "order_id", "counterparty_id"):
            object.__setattr__(self, field, uuid_value(getattr(self, field)))
        text_value(self.revision, "revision", 256)
        for field in ("total_minor", "eligible_minor", "paid_minor", "shipped_minor"):
            natural(getattr(self, field), field)
        if self.eligible_minor > self.total_minor:
            raise ValueError("eligible amount exceeds total")
        if type(self.cancelled) is not bool:
            raise ValueError("cancelled must be boolean")

    def validate_for_discount(self):
        if self.currency != "KZT":
            raise Conflict("unsupported currency")
        if self.cancelled or self.paid_minor or self.shipped_minor:
            raise Conflict("discount requires an unpaid, unshipped, non-cancelled order")


class Checkout:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def _rule_dict(row):
        return dict(version=row["version"], earn_bps=row["earn_bps"], spend_bps=row["spend_bps"],
                    delivery_policy=row["delivery_policy"], currency="KZT", minor_per_point=100,
                    rounding="floor", expires=False, actor_id=str(row["actor_id"]),
                    reason=row["reason"], created_at=row["created_at"].isoformat())

    def rules(self, tenant_id):
        tenant = uuid_value(tenant_id)
        with self.ledger.transaction(tenant) as conn:
            row = conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s "
                               "ORDER BY version DESC LIMIT 1", (tenant,)).fetchone()
            return self._rule_dict(row) if row else None

    def history(self, tenant_id, before=None):
        tenant = uuid_value(tenant_id)
        if before is not None:
            natural(before, "before", 2147483647)
        with self.ledger.transaction(tenant) as conn:
            rows = conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s "
                                "AND (%s::integer IS NULL OR version<%s) ORDER BY version DESC LIMIT 51",
                                (tenant,before,before)).fetchall()
            return {"items":[self._rule_dict(r) for r in rows[:50]],
                    "next_cursor":rows[49]["version"] if len(rows)>50 else None}

    def save_rules(self, tenant_id, *, earn_bps, spend_bps, delivery_policy,
                   expected_version, request_id, actor_id, reason):
        tenant, request, actor = map(uuid_value, (tenant_id,request_id,actor_id))
        Rules(earn_bps,spend_bps)
        natural(expected_version, "expected_version", 2147483646)
        if delivery_policy != "exclude":
            raise ValueError("delivery is excluded in this release")
        reason = text_value(reason, "reason", 500).strip()
        digest = fingerprint(dict(earn_bps=earn_bps,spend_bps=spend_bps,
                                  delivery_policy=delivery_policy,expected_version=expected_version,
                                  actor=actor,reason=reason))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"settings")
            previous = conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s AND request_id=%s",
                                    (tenant,request)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise Conflict("request ID reused with different settings")
                return self._rule_dict(previous)
            current = conn.execute("SELECT COALESCE(max(version),0) AS version FROM bonuspro.rule_versions "
                                   "WHERE tenant_id=%s", (tenant,)).fetchone()["version"]
            if current != expected_version:
                raise Conflict("settings changed; reload before saving")
            row = conn.execute(
                "INSERT INTO bonuspro.rule_versions "
                "(tenant_id,version,earn_bps,spend_bps,delivery_policy,request_id,request_hash,actor_id,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant,current+1,earn_bps,spend_bps,delivery_policy,request,digest,actor,reason)).fetchone()
            return self._rule_dict(row)

    def preview(self, tenant_id, snapshot: OrderSnapshot, *, requested_points, actor_id,
                eligibility_version: int,authorize=None,read_only_reason=None):
        tenant, actor = map(uuid_value,(tenant_id,actor_id))
        natural(requested_points,"requested_points")
        natural(eligibility_version,"eligibility_version",2147483647)
        if snapshot.tenant_id != tenant:
            raise Conflict("document belongs to another tenant")
        if snapshot.currency!="KZT":
            raise Conflict("unsupported currency")
        if read_only_reason is None:
            snapshot.validate_for_discount()
        elif read_only_reason not in ("order_not_posted","order_fulfilled","linked_documents_present","order_has_marker"):
            raise ValueError("unsupported read-only reason")
        elif requested_points:
            raise Conflict("read-only preview cannot request a discount")
        source_hash = fingerprint(asdict(snapshot))
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            # Configuration selection and new enrolment cannot race with a settings change.
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+snapshot.order_id)
            context = conn.execute("SELECT * FROM bonuspro.order_contexts WHERE tenant_id=%s AND order_id=%s",
                                   (tenant,snapshot.order_id)).fetchone()
            if context and str(context["counterparty_id"]) != snapshot.counterparty_id:
                raise Conflict("order customer changed; reconciliation required")
            if context:
                row = conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s AND version=%s",
                                   (tenant,context["rule_version"])).fetchone()
            else:
                row = conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s "
                                   "ORDER BY version DESC LIMIT 1",(tenant,)).fetchone()
            if row is None:
                raise Conflict("shop rules are not configured")
            # The authoritative adapter must calculate eligibility with this exact version.
            if eligibility_version != row["version"]:
                raise Conflict("eligibility was computed with a different rule version")
            link = conn.execute("SELECT wallet_id FROM bonuspro.customer_wallets "
                                "WHERE tenant_id=%s AND counterparty_id=%s",
                                (tenant,snapshot.counterparty_id)).fetchone()
            if link is None:
                wallet = str(uuid4())
                conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(tenant,wallet))
                conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) "
                             "VALUES (%s,%s,%s)",(tenant,snapshot.counterparty_id,wallet))
            else:
                wallet = str(link["wallet_id"])
            self.ledger._wallet_lock(conn,tenant,wallet)
            if context is None:
                conn.execute("INSERT INTO bonuspro.order_contexts "
                             "(tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by) "
                             "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                             (tenant,snapshot.order_id,snapshot.counterparty_id,wallet,row["version"],source_hash,actor))
            from bonuspro.adapters.postgres.automation import Automation
            Automation.hint_in(conn,tenant,snapshot.order_id)
            funds = self.ledger._snapshot(conn,tenant,wallet)
            pending = conn.execute("SELECT 1 FROM bonuspro.return_cases WHERE tenant_id=%s AND wallet_id=%s "
                                   "AND state='pending' LIMIT 1",(tenant,wallet)).fetchone()
            settlement = conn.execute("SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=%s AND wallet_id=%s AND state='pending' LIMIT 1",
                                      (tenant,wallet)).fetchone()
            pending = pending or settlement
            review = conn.execute("SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND wallet_id=%s AND resolved_at IS NULL LIMIT 1",
                                  (tenant,wallet)).fetchone()
            pending = pending or review
            existing = conn.execute("SELECT state FROM bonuspro.reservations WHERE tenant_id=%s AND order_id=%s "
                                    "AND state IN ('held','verifying','committed')",
                                    (tenant,snapshot.order_id)).fetchone()
            blocked = "return_review" if pending else ("order_has_redemption" if existing else read_only_reason)
            rules = Rules(row["earn_bps"],row["spend_bps"])
            maximum = 0 if blocked else spend_limit(snapshot.eligible_minor,funds["available"],rules)
            if requested_points > maximum:
                raise Conflict("requested points exceed current limit")
            value = quote(total_minor=snapshot.total_minor,eligible_minor=snapshot.eligible_minor,
                          available_points=funds["available"],requested_points=requested_points,rules=rules)
            result = dict(order_id=snapshot.order_id,counterparty_id=snapshot.counterparty_id,
                          wallet_id=wallet,rule_version=row["version"],source_hash=source_hash,
                          source_revision=snapshot.revision,currency="KZT",**funds,
                          max_spend_points=maximum,blocked_reason=blocked,**asdict(value))
            result["quote_hash"] = fingerprint(dict(tenant=tenant,**result))
            return result
