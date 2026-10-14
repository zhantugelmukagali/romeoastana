"""Manual release only for a proven cancelled, unpaid, unshipped and undiscounted order."""
import time
from bonuspro.adapters.postgres.ledger import Conflict,NotFound,uuid_value,text_value

class Voids:
    def __init__(self,ledger):self.ledger=ledger
    def lookup(self,tenant,rid):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT a.order_id,r.state FROM bonuspro.redemption_attempts a JOIN bonuspro.reservations r "
                "ON r.tenant_id=a.tenant_id AND r.id=a.id WHERE a.tenant_id=%s AND a.id=%s",(tenant,uuid_value(rid))).fetchone()
            if not row:raise NotFound("attempt missing")
            return row
    def release(self,tenant,rid,proof,*,actor,reason):
        rid,actor=map(uuid_value,(rid,actor));text_value(reason,"reason",500)
        with self.ledger.transaction(tenant) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            installation=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not installation or installation["state"]!="Activated":raise Conflict("installation inactive")
            prior=conn.execute("SELECT actor_id,reason FROM bonuspro.redemption_voids WHERE tenant_id=%s AND reservation_id=%s",(tenant,rid)).fetchone()
            if prior:
                if str(prior["actor_id"])!=actor or prior["reason"]!=reason:raise Conflict("void command changed")
                return {"state":"released"}
            row=conn.execute("SELECT a.*,r.wallet_id FROM bonuspro.redemption_attempts a JOIN bonuspro.reservations r "
                "ON r.tenant_id=a.tenant_id AND r.id=a.id WHERE a.tenant_id=%s AND a.id=%s",(tenant,rid)).fetchone()
            if not row:raise NotFound("attempt missing")
            self.ledger._key_lock(conn,tenant,"order/"+str(row["order_id"]))
            self.ledger._wallet_lock(conn,tenant,str(row["wallet_id"]))
            r=conn.execute("SELECT state,created_at<clock_timestamp()-interval '15 minutes' AS old FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s FOR UPDATE",(tenant,rid)).fetchone()
            if r["state"]!="verifying" or not r["old"]:raise Conflict("operation committed or still in flight")
            saved=proof.saved
            if saved.snapshot.tenant_id!=tenant or saved.snapshot.order_id!=str(row["order_id"]) or saved.marker or saved.financial()!=row["plan"]["before"]:
                raise Conflict("original undiscounted document required")
            if not proof.cancel_safe or not saved.snapshot.cancelled or proof.returned or proof.paid_minor or proof.shipped_minor:
                raise Conflict("cancelled unpaid unshipped document required")
            if time.monotonic()>proof.deadline:raise Conflict("expired proof")
            conn.execute("INSERT INTO bonuspro.redemption_voids(tenant_id,reservation_id,actor_id,reason,evidence_hash) VALUES (%s,%s,%s,%s,%s)",
                         (tenant,rid,actor,reason,proof.evidence_hash))
            conn.execute("UPDATE bonuspro.reservations SET state='released' WHERE tenant_id=%s AND id=%s",(tenant,rid))
            if time.monotonic()>proof.deadline:raise Conflict("expired proof")
            return {"state":"released"}
