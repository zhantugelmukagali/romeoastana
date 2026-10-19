"""Durable plans for host-side discounts. Browser acknowledgements are never proof."""
from uuid import uuid4
from contextlib import closing
import psycopg
from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import (
    Ledger, Conflict, NotFound, VerifiedDiscount, uuid_value, fingerprint, digest_value,
)
from bonuspro.domain.finance import Rules, natural, spend_limit
from bonuspro.adapters.moysklad.order import SavedOrder


class SetupInProgress(Conflict):pass
class ProgramNotConfigured(Conflict):pass

class Redemptions:
    def __init__(self,ledger: Ledger):
        self.ledger=ledger

    def configure_marker(self,tenant,actor,provider,*,authorize):
        tenant,actor=map(uuid_value,(tenant,actor))
        # Serialize remote provisioning without holding a financial transaction.
        with closing(psycopg.connect(self.ledger.dsn,autocommit=True,connect_timeout=5)) as guard:
            guard.execute("SET statement_timeout='5s'")
            if not guard.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                 ("widget/provision/"+tenant,)).fetchone()[0]:
                raise SetupInProgress("widget setup already running")
            existing=self.binding(tenant)
            marker=provider.marker_attribute(create=existing is None)
            authorize()
            self.bind(tenant,marker,actor)
            return marker

    def binding(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            return str(row["marker_id"]) if row else None

    def bind(self,tenant,marker,actor):
        tenant,marker,actor=map(uuid_value,(tenant,marker,actor))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"widget/setup")
            row=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if row:
                if str(row["marker_id"])!=marker:
                    raise Conflict("marker metadata changed; review required")
            else:
                conn.execute("INSERT INTO bonuspro.widget_bindings(tenant_id,marker_id,actor_id) VALUES (%s,%s,%s)",
                             (tenant,marker,actor))
        return marker

    def rules_for_order(self,tenant,order):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT r.* FROM bonuspro.rule_versions r WHERE r.tenant_id=%s "
                "AND r.version=COALESCE((SELECT rule_version FROM bonuspro.order_contexts "
                "WHERE tenant_id=%s AND order_id=%s),(SELECT max(version) FROM bonuspro.rule_versions WHERE tenant_id=%s))",
                (tenant,tenant,order,tenant)).fetchone()
            if not row:
                raise ProgramNotConfigured("rules are not configured")
            return row

    def personal_preview(self,tenant,saved,original_quote):
        from bonuspro.adapters.postgres.personal_applications import PersonalApplications
        return PersonalApplications(self.ledger).preview(tenant,saved,original_quote)

    def existing(self,tenant,order):
        with self.ledger.transaction(tenant) as conn:
            from bonuspro.adapters.postgres.personal_applications import PersonalApplications
            personal=PersonalApplications.existing_in(conn,tenant,order)
            if personal:return personal
            row=conn.execute("SELECT a.*,r.state,r.points FROM bonuspro.redemption_attempts a "
                "JOIN bonuspro.reservations r ON r.tenant_id=a.tenant_id AND r.id=a.id "
                "WHERE a.tenant_id=%s AND a.order_id=%s AND r.state IN ('verifying','committed')",
                (tenant,order)).fetchone()
            return row

    @staticmethod
    def public(row):
        result=dict(id=str(row["id"]),order_id=str(row["order_id"]),state=row["state"],points=row["points"],
                    recoverable=row["state"]=="verifying")
        if row.get("personal_application"):
            result.update(application_id=str(row["id"]),apply_personal=True,
                          personal_discount_minor=row["plan"]["personal_discount_minor"],
                          personal_rate_bps=row["rate_bps"])
        return result

    def prepare(self,tenant,order: SavedOrder,*,points,request_id,actor_id,expected_source,authorize=None,
                apply_personal=False,expected_policy_version=None,expected_assignment_version=None):
        if type(apply_personal) is not bool:raise ValueError("invalid personal discount selection")
        if apply_personal:
            from bonuspro.adapters.postgres.personal_applications import PersonalApplications
            return PersonalApplications(self.ledger).prepare(tenant,order,points=points,request_id=request_id,
                actor_id=actor_id,expected_source=expected_source,expected_policy_version=expected_policy_version,
                expected_assignment_version=expected_assignment_version,authorize=authorize)
        tenant,request_id,actor=map(uuid_value,(tenant,request_id,actor_id))
        natural(points,"points")
        digest_value(expected_source)
        if tenant!=order.snapshot.tenant_id:
            raise Conflict("wrong tenant")
        body_hash=fingerprint(dict(order=order.snapshot.order_id,points=points,actor=actor,source=expected_source))
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            self.ledger._key_lock(conn,tenant,"widget/request/"+request_id)
            if conn.execute("SELECT 1 FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND request_id=%s",
                            (tenant,request_id)).fetchone():raise Conflict("request ID reused")
            previous=conn.execute("SELECT a.*,r.state,r.points FROM bonuspro.redemption_attempts a "
                "JOIN bonuspro.reservations r ON r.tenant_id=a.tenant_id AND r.id=a.id "
                "WHERE a.tenant_id=%s AND a.request_id=%s",(tenant,request_id)).fetchone()
            if previous:
                if previous["request_hash"]!=body_hash:
                    raise Conflict("request ID reused")
                # Never redispatch an old form mutation after a lost response or a saved edit.
                return dict(**self.public(previous),update_state=None,replayed=True)
            if order.source_hash!=expected_source:
                raise Conflict("order changed; reload")
            plan=order.plan(points)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+order.snapshot.order_id)
            installation=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",
                                      (tenant,)).fetchone()
            if not installation or installation["state"]!="Activated":
                raise Conflict("installation is not activated for sales")
            binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not binding:
                raise Conflict("widget setup required")
            context=conn.execute("SELECT o.*,r.earn_bps,r.spend_bps FROM bonuspro.order_contexts o "
                "JOIN bonuspro.rule_versions r ON r.tenant_id=o.tenant_id AND r.version=o.rule_version "
                "WHERE o.tenant_id=%s AND o.order_id=%s",(tenant,order.snapshot.order_id)).fetchone()
            if not context or str(context["counterparty_id"])!=order.snapshot.counterparty_id:
                raise Conflict("customer or order binding changed")
            try:
                conn.execute("SELECT bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                    (order.snapshot.order_id,order.snapshot.counterparty_id,order.source_hash,order.snapshot.revision,actor))
            except psycopg.errors.CheckViolation:
                raise Conflict("order changed; reload") from None
            if conn.execute("SELECT 1 FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND order_id=%s "
                            "AND state IN ('verifying','confirmed')",(tenant,order.snapshot.order_id)).fetchone():
                raise Conflict("order already has a personal discount operation")
            wallet=str(context["wallet_id"])
            self.ledger._wallet_lock(conn,tenant,wallet)
            if conn.execute("SELECT 1 FROM bonuspro.reservations WHERE tenant_id=%s AND order_id=%s "
                "AND state IN ('held','verifying','committed')",(tenant,order.snapshot.order_id)).fetchone():
                raise Conflict("order already has a redemption")
            if conn.execute("SELECT 1 FROM bonuspro.return_cases WHERE tenant_id=%s AND wallet_id=%s AND state='pending'",
                            (tenant,wallet)).fetchone():
                raise Conflict("return review required")
            if conn.execute("SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=%s AND wallet_id=%s AND state='pending' LIMIT 1",
                            (tenant,wallet)).fetchone():
                raise Conflict("return settlement review required")
            if conn.execute("SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND wallet_id=%s AND resolved_at IS NULL LIMIT 1",
                            (tenant,wallet)).fetchone():
                raise Conflict("wallet requires reconciliation review")
            funds=self.ledger._snapshot(conn,tenant,wallet)
            if points>spend_limit(order.snapshot.eligible_minor,funds["available"],
                                  Rules(context["earn_bps"],context["spend_bps"])):
                raise Conflict("insufficient balance or order limit")
            rid=str(uuid4())
            marker="bonuspro:v1:"+rid
            marker_id=str(binding["marker_id"])
            plan["update_state"]["attributes"]=[{"id":marker_id,"value":marker}]
            qhash=fingerprint(dict(tenant=tenant,order=order.snapshot.order_id,points=points,marker=marker,plan=plan))
            # Reserve and plan persist in ONE transaction; no orphan and no overspend window.
            conn.execute("INSERT INTO bonuspro.reservations "
                "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,actor_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant,rid,wallet,"widget/"+request_id,body_hash,order.snapshot.order_id,qhash,points,actor))
            conn.execute("UPDATE bonuspro.reservations SET state='verifying' WHERE tenant_id=%s AND id=%s",(tenant,rid))
            conn.execute("INSERT INTO bonuspro.redemption_attempts "
                "(tenant_id,id,order_id,request_id,request_hash,actor_id,source_hash,marker_id,marker,plan) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant,rid,order.snapshot.order_id,request_id,body_hash,actor,expected_source,marker_id,marker,Jsonb(plan)))
            return dict(id=rid,order_id=order.snapshot.order_id,state="verifying",points=points,
                        update_state=plan["update_state"],replayed=False,recoverable=True)

    @staticmethod
    def resumable(row,order: SavedOrder):
        # Recovery can replay only the absolute plan for the unchanged original
        # financial document. A fresh Save may advance revision without changing it.
        return (row["state"]=="verifying" and not order.marker
                and not order.discount_blocked_reason
                and not order.snapshot.cancelled
                and not order.snapshot.paid_minor and not order.snapshot.shipped_minor
                and order.financial()==row["plan"]["before"])

    def resume(self,tenant,order: SavedOrder,*,attempt_id,expected_source,authorize=None):
        current=self.existing(tenant,order.snapshot.order_id)
        if current and current.get("personal_application"):
            from bonuspro.adapters.postgres.personal_applications import PersonalApplications
            return PersonalApplications(self.ledger).resume(tenant,order,attempt_id=attempt_id,
                expected_source=expected_source,authorize=authorize)
        tenant,attempt_id=map(uuid_value,(tenant,attempt_id))
        digest_value(expected_source)
        if tenant!=order.snapshot.tenant_id:
            raise Conflict("wrong tenant")
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            # Serialize against confirmation, void and installation revocation.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+order.snapshot.order_id)
            row=conn.execute("SELECT a.*,r.state,r.points FROM bonuspro.redemption_attempts a "
                "JOIN bonuspro.reservations r ON r.tenant_id=a.tenant_id AND r.id=a.id "
                "WHERE a.tenant_id=%s AND a.order_id=%s AND a.id=%s",
                (tenant,order.snapshot.order_id,attempt_id)).fetchone()
            if not row or not self.resumable(row,order) or order.source_hash!=expected_source:
                raise Conflict("saved order changed; verify before recovery")
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated" or not binding or str(binding["marker_id"])!=str(row["marker_id"]):
                raise Conflict("widget setup or sales activation required")
            if authorize:authorize(conn)
            # No reservation or debit is created. Values come from the immutable
            # stored plan, never from the caller or a newly calculated discount.
            return dict(**self.public(row),update_state=row["plan"]["update_state"],replayed=True)

    def confirm_saved(self,tenant,order: SavedOrder,*,authorize=None):
        row=self.existing(tenant,order.snapshot.order_id)
        if not row:
            raise NotFound("redemption not found")
        if row.get("personal_application"):
            from bonuspro.adapters.postgres.personal_applications import PersonalApplications
            return PersonalApplications(self.ledger).confirm(tenant,order,authorize=authorize)
        if tenant!=order.snapshot.tenant_id:
            raise Conflict("wrong tenant")
        # Even a retry of a committed attempt reports a mismatch if the document changed.
        exact=(order.marker==row["marker"] and order.financial()==row["plan"]["after"]
               and not order.snapshot.cancelled
               and (row["state"]=="committed" or
                    (not order.snapshot.paid_minor and not order.snapshot.shipped_minor)))
        if not exact:
            result=dict(**self.public(row),verified=False,reason="saved_order_mismatch")
            result["recoverable"]=self.resumable(row,order)
            if result["recoverable"]:
                result["resume_source_hash"]=order.source_hash
            return result
        with self.ledger.transaction(tenant) as conn:
            reservation=conn.execute("SELECT quote_hash FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s",
                                     (tenant,row["id"])).fetchone()
        proof=VerifiedDiscount(order.snapshot.order_id,reservation["quote_hash"],row["points"]*100,order.snapshot.revision)
        # Same advisory key as lifecycle.change: revocation cannot overtake this debit.
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated":
                raise Conflict("installation is not activated")
            transaction=self.ledger.confirm_discount_in(conn,tenant,str(row["id"]),proof=proof)
        return dict(id=str(row["id"]),order_id=order.snapshot.order_id,state="committed",
                    points=row["points"],verified=True,transaction_id=transaction)
