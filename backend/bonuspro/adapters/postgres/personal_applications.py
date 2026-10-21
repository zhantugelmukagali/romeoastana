"""Durable personal discounts, optionally paired with one bonus reservation.

Only saved provider financial data plus our immutable marker confirms an operation.
Personal-only operations never create a points reservation or posting.
"""
from uuid import uuid4
import time
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import (
    Conflict, NotFound, VerifiedDiscount, uuid_value, digest_value, fingerprint, text_value,
)
from bonuspro.adapters.postgres.personal_discounts import PersonalDiscounts
from bonuspro.domain.finance import Rules, natural, quote, spend_limit


class PersonalApplications:
    def __init__(self, ledger):
        self.ledger=ledger

    @staticmethod
    def normalized(row):
        if row is None:return None
        result=dict(row)
        result["personal_application"]=True
        result["application_state"]=row["state"]
        result["state"]={"confirmed":"committed","voided":"released"}.get(row["state"],row["state"])
        return result

    @classmethod
    def existing_in(cls,conn,tenant,order):
        return cls.normalized(conn.execute(
            "SELECT * FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND order_id=%s "
            "AND state IN ('verifying','confirmed')",(tenant,order)).fetchone())

    @staticmethod
    def preview_in(conn,tenant,saved,original_quote):
        resolved=PersonalDiscounts.resolve_in(conn,tenant,saved.snapshot.counterparty_id)
        policy,assignment=resolved["policy"],resolved["assignment"]
        rate=resolved["effective_rate_bps"]
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        blocked_reason=None
        try:
            projected,_=saved.personal_discount(rate,policy["external_policy"])
        except InvalidDocument as error:
            projected=saved
            blocked_reason=error.reason
        reduction=saved.snapshot.total_minor-projected.snapshot.total_minor
        row=conn.execute("SELECT earn_bps,spend_bps FROM bonuspro.rule_versions WHERE tenant_id=%s AND version=%s",
                         (tenant,original_quote["rule_version"])).fetchone()
        if not row:raise Conflict("shop rules changed")
        rules=Rules(row["earn_bps"],row["spend_bps"])
        blocked=bool(original_quote.get("blocked_reason"))
        values=quote(total_minor=projected.snapshot.total_minor,eligible_minor=projected.snapshot.eligible_minor,
                     available_points=original_quote["available"],requested_points=0,rules=rules)
        nested=dict(original_quote,spend_points=values.spend_points,payable_minor=values.payable_minor,
                    potential_earn_points=values.potential_earn_points,
                    max_spend_points=(spend_limit(projected.snapshot.eligible_minor,original_quote["available"],rules)
                                      if policy["allow_bonus_stack"] and not blocked else 0))
        nested["quote_hash"]=fingerprint(dict(original=original_quote["quote_hash"],policy=policy["version"],
            assignment=assignment["version"],personal_basis=projected.financial()))
        return dict(enabled=policy["enabled"],policy_version=policy["version"],assignment_version=assignment["version"],
                    rate_bps=rate,external_policy=policy["external_policy"],allow_bonus_stack=policy["allow_bonus_stack"],
                    discount_minor=reduction,total_minor_after=projected.snapshot.total_minor,
                    eligible_minor_after=projected.snapshot.eligible_minor,
                    can_apply=bool(reduction and not blocked and not blocked_reason),blocked_reason=blocked_reason,quote=nested)

    def preview(self,tenant,saved,original_quote):
        tenant=uuid_value(tenant)
        if saved.snapshot.tenant_id!=tenant:raise Conflict("wrong tenant")
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"settings")
            return self.preview_in(conn,tenant,saved,original_quote)

    def prepare(self,tenant,saved,*,points,request_id,actor_id,expected_source,
                expected_policy_version,expected_assignment_version,authorize=None):
        tenant,request,actor=map(uuid_value,(tenant,request_id,actor_id))
        natural(points,"points")
        natural(expected_policy_version,"expected_policy_version",2147483647)
        natural(expected_assignment_version,"expected_assignment_version",2147483647)
        digest_value(expected_source)
        if saved.snapshot.tenant_id!=tenant:raise Conflict("wrong tenant")
        body_hash=fingerprint(dict(order=saved.snapshot.order_id,points=points,actor=actor,source=expected_source,
            apply_personal=True,policy_version=expected_policy_version,assignment_version=expected_assignment_version))
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            self.ledger._key_lock(conn,tenant,"widget/request/"+request)
            previous=conn.execute("SELECT * FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND request_id=%s",
                                  (tenant,request)).fetchone()
            if previous:
                if previous["request_hash"]!=body_hash:raise Conflict("request ID reused")
                from bonuspro.adapters.postgres.redemptions import Redemptions
                return dict(**Redemptions.public(self.normalized(previous)),update_state=None,replayed=True)
            if conn.execute("SELECT 1 FROM bonuspro.redemption_attempts WHERE tenant_id=%s AND request_id=%s",
                            (tenant,request)).fetchone():raise Conflict("request ID reused")
            if saved.source_hash!=expected_source:raise Conflict("order changed; reload")
            saved.snapshot.validate_for_discount()
            if saved.marker or saved.discount_blocked_reason:raise Conflict("saved order is read-only")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+saved.snapshot.order_id)
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated" or not binding:
                raise Conflict("widget setup or sales activation required")
            context=conn.execute("SELECT o.*,r.earn_bps,r.spend_bps FROM bonuspro.order_contexts o "
                "JOIN bonuspro.rule_versions r ON r.tenant_id=o.tenant_id AND r.version=o.rule_version "
                "WHERE o.tenant_id=%s AND o.order_id=%s",(tenant,saved.snapshot.order_id)).fetchone()
            if not context or str(context["counterparty_id"])!=saved.snapshot.counterparty_id:
                raise Conflict("customer or order binding changed")
            # Reject a provider read overtaken by a newer observed document.
            try:
                conn.execute("SELECT bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                    (saved.snapshot.order_id,saved.snapshot.counterparty_id,saved.source_hash,saved.snapshot.revision,actor))
            except CheckViolation:
                raise Conflict("order changed; reload") from None
            resolved=PersonalDiscounts.resolve_in(conn,tenant,saved.snapshot.counterparty_id)
            policy,assignment=resolved["policy"],resolved["assignment"]
            if policy["version"]!=expected_policy_version or assignment["version"]!=expected_assignment_version:
                raise Conflict("personal discount changed; reload")
            if not policy["enabled"] or not resolved["effective_rate_bps"]:
                raise Conflict("personal discount is not assigned")
            if points and not policy["allow_bonus_stack"]:
                raise Conflict("personal discounts and bonuses cannot be combined")
            basis,calculation=saved.personal_discount(resolved["effective_rate_bps"],policy["external_policy"])
            reduction=saved.snapshot.total_minor-basis.snapshot.total_minor
            if not reduction:raise Conflict("personal discount does not reduce this order")
            wallet=str(context["wallet_id"])
            self.ledger._wallet_lock(conn,tenant,wallet)
            if self.existing_in(conn,tenant,saved.snapshot.order_id) or conn.execute(
                "SELECT 1 FROM bonuspro.reservations WHERE tenant_id=%s AND order_id=%s "
                "AND state IN ('held','verifying','committed')",(tenant,saved.snapshot.order_id)).fetchone():
                raise Conflict("order already has a discount operation")
            for table,condition in (("return_cases","state='pending'"),("settlement_cases","state='pending'"),
                                    ("reconciliation_issues","resolved_at IS NULL")):
                if conn.execute("SELECT 1 FROM bonuspro."+table+" WHERE tenant_id=%s AND wallet_id=%s AND "+condition+" LIMIT 1",
                                (tenant,wallet)).fetchone():raise Conflict("wallet requires return or reconciliation review")
            funds=self.ledger._snapshot(conn,tenant,wallet)
            if points>spend_limit(basis.snapshot.eligible_minor,funds["available"],Rules(context["earn_bps"],context["spend_bps"])):
                raise Conflict("insufficient balance or order limit")
            if points:
                plan=basis.plan(points)
            else:
                plan=dict(after=basis.financial(),update_state={"positions":[]})
            # Rebuild the one absolute host patch against the ORIGINAL document.
            after_by_id={line["id"]:line for line in plan["after"]["lines"]}
            positions=[]
            for line in saved.lines:
                changed=after_by_id[line.id]
                update={"id":line.id}
                if changed["discount_bps"]!=line.discount_bps:update["discount"]=changed["discount_bps"]/100
                if changed["price"]!=str(line.price):
                    from decimal import Decimal
                    value=Decimal(changed["price"])
                    if value!=value.to_integral_value() or not 0<=value<=line.price:
                        raise Conflict("personal discount price cannot be represented")
                    update["price"]=int(value)
                positions.append(update)
            identity=str(uuid4());marker="bonuspro:v1:"+identity;marker_id=str(binding["marker_id"])
            plan.update(before=saved.financial(),bonus_before=basis.financial(),personal_basis=basis.financial(),
                        source_hash=saved.source_hash,reconstruction=saved.reconstruction_manifest(),personal_discount_minor=reduction,
                        personal_rate_bps=resolved["effective_rate_bps"],personal_calculation=calculation,
                        personal_policy=dict(version=policy["version"],external_policy=policy["external_policy"],
                                             allow_bonus_stack=policy["allow_bonus_stack"]),
                        personal_assignment_version=assignment["version"])
            plan["update_state"]={"positions":positions,"attributes":[{"id":marker_id,"value":marker}]}
            if points:
                qhash=fingerprint(dict(tenant=tenant,order=saved.snapshot.order_id,points=points,marker=marker,plan=plan))
                conn.execute("INSERT INTO bonuspro.reservations "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,actor_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tenant,identity,wallet,"widget/"+request,body_hash,saved.snapshot.order_id,qhash,points,actor))
                conn.execute("UPDATE bonuspro.reservations SET state='verifying' WHERE tenant_id=%s AND id=%s",(tenant,identity))
                conn.execute("INSERT INTO bonuspro.redemption_attempts "
                    "(tenant_id,id,order_id,request_id,request_hash,actor_id,source_hash,marker_id,marker,plan) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tenant,identity,saved.snapshot.order_id,request,body_hash,actor,expected_source,marker_id,marker,Jsonb(plan)))
            row=conn.execute("INSERT INTO bonuspro.personal_discount_applications "
                "(tenant_id,id,order_id,counterparty_id,wallet_id,request_id,request_hash,source_hash,actor_id,"
                "marker_id,marker,policy_version,assignment_version,rate_bps,points,redemption_id,plan) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant,identity,saved.snapshot.order_id,saved.snapshot.counterparty_id,wallet,request,body_hash,
                 expected_source,actor,marker_id,marker,policy["version"],assignment["version"],
                 resolved["effective_rate_bps"],points,identity if points else None,Jsonb(plan))).fetchone()
            from bonuspro.adapters.postgres.redemptions import Redemptions
            return dict(**Redemptions.public(self.normalized(row)),update_state=plan["update_state"],replayed=False)

    def confirm_in(self,conn,tenant,row,saved,*,worker=False):
        self.ledger._key_lock(conn,tenant,"order/"+saved.snapshot.order_id)
        self.ledger._wallet_lock(conn,tenant,str(row["wallet_id"]))
        current=conn.execute("SELECT * FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND id=%s FOR UPDATE",
                             (tenant,row["id"])).fetchone()
        if not current or current["state"] not in ("verifying","confirmed"):
            raise Conflict("personal discount is not active")
        if saved.marker!=current["marker"] or saved.financial()!=current["plan"]["after"]:
            raise Conflict("saved personal discount does not match")
        if not worker and (saved.snapshot.cancelled or (current["state"]=="verifying" and
            (saved.snapshot.paid_minor or saved.snapshot.shipped_minor))):
            raise Conflict("saved personal discount cannot be confirmed")
        tx=None
        if current["points"]:
            reservation=conn.execute("SELECT quote_hash FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s",
                                     (tenant,current["redemption_id"])).fetchone()
            tx=self.ledger.confirm_discount_in(conn,tenant,str(current["redemption_id"]),proof=VerifiedDiscount(
                saved.snapshot.order_id,reservation["quote_hash"],current["points"]*100,saved.snapshot.revision))
        if current["state"]=="verifying":
            conn.execute("UPDATE bonuspro.personal_discount_applications SET state='confirmed',proof_revision=%s,updated_at=clock_timestamp() "
                         "WHERE tenant_id=%s AND id=%s",(saved.snapshot.revision,tenant,current["id"]))
        return tx

    def confirm(self,tenant,saved,*,authorize=None):
        tenant=uuid_value(tenant)
        if saved.snapshot.tenant_id!=tenant:raise Conflict("wrong tenant")
        with self.ledger.transaction(tenant) as conn:
            row=self.existing_in(conn,tenant,saved.snapshot.order_id)
            if not row:raise NotFound("personal discount not found")
            from bonuspro.adapters.postgres.redemptions import Redemptions
            exact=(saved.marker==row["marker"] and saved.financial()==row["plan"]["after"]
                   and not saved.snapshot.cancelled and (row["state"]=="committed" or
                       (not saved.snapshot.paid_minor and not saved.snapshot.shipped_minor)))
            if not exact:
                result=dict(**Redemptions.public(row),verified=False,reason="saved_order_mismatch")
                result["recoverable"]=Redemptions.resumable(row,saved)
                if result["recoverable"]:result["resume_source_hash"]=saved.source_hash
                return result
            if authorize:authorize(conn)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated":raise Conflict("installation is not activated")
            tx=self.confirm_in(conn,tenant,row,saved)
            return dict(**Redemptions.public(dict(row,state="committed")),verified=True,transaction_id=tx)

    def resume(self,tenant,saved,*,attempt_id,expected_source,authorize=None):
        tenant,identity=map(uuid_value,(tenant,attempt_id));digest_value(expected_source)
        if saved.snapshot.tenant_id!=tenant:raise Conflict("wrong tenant")
        with self.ledger.transaction(tenant) as conn:
            if authorize:authorize(conn)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+saved.snapshot.order_id)
            row=self.existing_in(conn,tenant,saved.snapshot.order_id)
            from bonuspro.adapters.postgres.redemptions import Redemptions
            if not row or str(row["id"])!=identity or not Redemptions.resumable(row,saved) or saved.source_hash!=expected_source:
                raise Conflict("saved order changed; verify before recovery")
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated" or not binding or str(binding["marker_id"])!=str(row["marker_id"]):
                raise Conflict("widget setup or sales activation required")
            if authorize:authorize(conn)
            return dict(**Redemptions.public(row),update_state=row["plan"]["update_state"],replayed=True)

    def release(self,tenant,identity,proof,*,actor,reason):
        tenant,identity,actor=map(uuid_value,(tenant,identity,actor))
        text_value(reason,"reason",500)
        with self.ledger.transaction(tenant) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated":raise Conflict("installation inactive")
            row=conn.execute("SELECT * FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND id=%s",
                             (tenant,identity)).fetchone()
            if not row:raise NotFound("personal discount not found")
            self.ledger._key_lock(conn,tenant,"order/"+str(row["order_id"]))
            self.ledger._wallet_lock(conn,tenant,str(row["wallet_id"]))
            prior=conn.execute("SELECT actor_id,reason FROM bonuspro.personal_discount_voids WHERE tenant_id=%s AND application_id=%s",
                               (tenant,identity)).fetchone()
            if prior:
                if str(prior["actor_id"])!=actor or prior["reason"]!=reason:raise Conflict("void command changed")
                return {"state":"released"}
            row=conn.execute("SELECT *,created_at<clock_timestamp()-interval '15 minutes' AS old "
                "FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND id=%s FOR UPDATE",(tenant,identity)).fetchone()
            if row["state"]!="verifying" or not row["old"]:raise Conflict("operation confirmed or still in flight")
            saved=proof.saved
            if (saved.snapshot.tenant_id!=tenant or saved.snapshot.order_id!=str(row["order_id"]) or saved.marker
                or saved.financial()!=row["plan"]["before"]):raise Conflict("original undiscounted document required")
            if not proof.cancel_safe or not saved.snapshot.cancelled or proof.returned or proof.paid_minor or proof.shipped_minor:
                raise Conflict("cancelled unpaid unshipped document required")
            if time.monotonic()>proof.deadline:raise Conflict("expired proof")
            if row["points"]:
                reservation=conn.execute("SELECT state,created_at<clock_timestamp()-interval '15 minutes' AS old "
                    "FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s FOR UPDATE",(tenant,row["redemption_id"])).fetchone()
                if reservation["state"]!="verifying" or not reservation["old"]:
                    raise Conflict("bonus operation committed or still in flight")
                conn.execute("INSERT INTO bonuspro.redemption_voids(tenant_id,reservation_id,actor_id,reason,evidence_hash) "
                    "VALUES (%s,%s,%s,%s,%s)",(tenant,row["redemption_id"],actor,reason,proof.evidence_hash))
                conn.execute("UPDATE bonuspro.reservations SET state='released' WHERE tenant_id=%s AND id=%s",
                             (tenant,row["redemption_id"]))
            conn.execute("INSERT INTO bonuspro.personal_discount_voids "
                "(tenant_id,application_id,actor_id,reason,evidence_hash,proof_revision) VALUES (%s,%s,%s,%s,%s,%s)",
                (tenant,identity,actor,reason,proof.evidence_hash,saved.snapshot.revision))
            conn.execute("UPDATE bonuspro.personal_discount_applications SET state='voided',updated_at=clock_timestamp() "
                         "WHERE tenant_id=%s AND id=%s",(tenant,identity))
            if time.monotonic()>proof.deadline:raise Conflict("expired proof")
            return {"state":"released"}
