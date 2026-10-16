"""Cumulative return/cancellation settlement with immutable adjustments and no debt."""
from uuid import uuid4
from bonuspro.adapters.postgres.ledger import Ledger,Conflict,NotFound,MAX_POINTS,uuid_value,text_value
from bonuspro.adapters.moysklad.settlement import targets


class Settlements:
    def __init__(self,ledger):
        self.ledger=ledger

    def apply_in(self,conn,tenant,proof,context,prior,attempt,rules,actor,*,resolution=None):
        order=proof.saved.snapshot.order_id;wallet=str(context["wallet_id"])
        self.ledger._wallet_lock(conn,tenant,wallet)
        target=targets(proof,rules=rules,attempt=attempt)
        gross=prior["points"] if prior else 0
        spent=attempt["points"] if attempt else 0
        wanted_earn=min(gross,target.earn)
        sums=conn.execute("SELECT COALESCE(sum(earn_delta),0) e,COALESCE(sum(refund_delta),0) r,"
            "COALESCE(sum(waived_earn),0) we,COALESCE(sum(waived_refund),0) wr "
            "FROM bonuspro.settlement_events WHERE tenant_id=%s AND order_id=%s",(tenant,order)).fetchone()
        earned=gross+int(sums["e"]);refunded=int(sums["r"])
        de=min(gross,wanted_earn+int(sums["we"]))-earned
        dr=min(spent,target.refund+int(sums["wr"]))-refunded
        case=conn.execute("SELECT * FROM bonuspro.settlement_cases WHERE tenant_id=%s AND order_id=%s AND state='pending'",
                          (tenant,order)).fetchone()
        matching=case and case["evidence_hash"]==target.digest and case["earn_delta"]==de and case["refund_delta"]==dr
        if resolution:
            case_id,decision,reason=resolution
            if not matching or str(case["id"])!=case_id:
                raise Conflict("return changed; refresh before deciding")
            if decision not in ("apply","waive"):
                raise ValueError("invalid settlement decision")
            text_value(reason,"reason",500)
        elif case and not matching:
            conn.execute("UPDATE bonuspro.settlement_cases SET state='superseded',resolved_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                         (tenant,case["id"]))
            case=None
        if case and not resolution:
            return "return_review"
        if not de and not dr and not resolution:
            return "credited" if prior else proof.outcome
        funds=self.ledger._snapshot(conn,tenant,wallet)
        other=conn.execute("SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=%s AND wallet_id=%s AND state='pending' AND order_id<>%s LIMIT 1",
                           (tenant,wallet,order)).fetchone()
        if not resolution and (de+dr < -funds["available"] or (other and de+dr<0)):
            conn.execute("INSERT INTO bonuspro.settlement_cases "
                "(tenant_id,id,order_id,wallet_id,evidence_hash,target_earn,target_refund,earn_delta,refund_delta,order_name) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant,str(uuid4()),order,wallet,target.digest,wanted_earn,target.refund,de,dr,proof.saved.name))
            return "return_review"
        decision=resolution[1] if resolution else "automatic"
        reason=resolution[2] if resolution else "Reconciled returned goods and sale completion"
        we=wr=0
        if decision=="waive":
            # An explicit grant persists as an allowance, capped by the original order.
            # Undoing/repeating a return cannot award the same waived points twice.
            we=max(0,-de);wr=max(0,-dr)
            de+=we;dr+=wr
        delta=de+dr
        if delta < -funds["available"] or funds["balance"]+delta>MAX_POINTS:
            raise Conflict("insufficient free balance or balance limit")
        event=str(uuid4())
        conn.execute("INSERT INTO bonuspro.settlement_events "
            "(tenant_id,id,order_id,wallet_id,target_earn,target_refund,earn_delta,refund_delta,waived_earn,waived_refund,evidence_hash,actor_id,decision,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,event,order,wallet,wanted_earn,target.refund,de,dr,we,wr,target.digest,actor,decision,reason))
        if delta:
            self.ledger._post(conn,tenant,wallet,"settlement/"+event,target.digest,delta,"settlement",actor,reason,settlement_event=event)
        if case:
            conn.execute("UPDATE bonuspro.settlement_cases SET state=%s,event_id=%s,resolved_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                         ("waived" if decision=="waive" else "applied",event,tenant,case["id"]))
        return "credited" if prior else proof.outcome

    def case(self,tenant,identity):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT c.*,e.actor_id,e.decision,e.reason FROM bonuspro.settlement_cases c "
                "LEFT JOIN bonuspro.settlement_events e ON e.tenant_id=c.tenant_id AND e.id=c.event_id "
                "WHERE c.tenant_id=%s AND c.id=%s",(tenant,uuid_value(identity))).fetchone()
            if not row:raise NotFound("settlement case missing")
            return row

    def list_cases(self,tenant,*,state="pending",before=None):
        if state not in ("pending","applied","waived","superseded"):raise ValueError("invalid state")
        cursor=uuid_value(before) if before else None
        with self.ledger.transaction(tenant) as conn:
            rows=conn.execute("SELECT c.*,e.reason FROM bonuspro.settlement_cases c "
                "LEFT JOIN bonuspro.settlement_events e ON e.tenant_id=c.tenant_id AND e.id=c.event_id "
                "WHERE c.tenant_id=%s AND c.state=%s AND (%s::uuid IS NULL OR c.id<%s::uuid) ORDER BY c.id DESC LIMIT 51",
                (tenant,state,cursor,cursor)).fetchall()
            items=[]
            for row in rows[:50]:
                funds=self.ledger._snapshot(conn,tenant,str(row["wallet_id"]))
                items.append(dict(id=str(row["id"]),order_id=str(row["order_id"]),order_name=row["order_name"],
                    wallet_id=str(row["wallet_id"]),earn_delta=row["earn_delta"],refund_delta=row["refund_delta"],
                    total_delta=row["earn_delta"]+row["refund_delta"],state=row["state"],reason=row["reason"],
                    created_at=row["created_at"].isoformat(),**funds))
            return dict(items=items,next_cursor=items[-1]["id"] if len(rows)>50 else None)
