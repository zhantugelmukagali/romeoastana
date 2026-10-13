"""Trusted return application service; provider reconciliation supplies amounts.
No client-supplied refund is accepted by a public endpoint. Each return document
allocates a portion of one immutable credit, including amounts waived by admin.
"""
from uuid import uuid4

from bonuspro.adapters.postgres.ledger import (
    Ledger, Conflict, NotFound, MAX_POINTS, fingerprint, uuid_value, text_value,
)
from bonuspro.domain.finance import natural


class Returns:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def request(self, tenant_id, credit_id, *, return_id, points, actor_id):
        tenant, credit, returned, actor = map(uuid_value, (tenant_id, credit_id, return_id, actor_id))
        natural(points, "points", MAX_POINTS)
        if not points:
            raise ValueError("reversal must be positive")
        digest = fingerprint(dict(credit=credit, returned=returned, points=points))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn, tenant, "return/" + credit + "/" + returned)
            prior = conn.execute(
                "SELECT * FROM bonuspro.return_cases WHERE tenant_id=%s AND credit_id=%s AND return_id=%s",
                (tenant, credit, returned),
            ).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise Conflict("return document changed; reconciliation required")
                return self._result(prior)
            source = conn.execute(
                "SELECT * FROM bonuspro.ledger_transactions WHERE tenant_id=%s AND id=%s AND kind='credit'",
                (tenant, credit),
            ).fetchone()
            if not source:
                raise NotFound("original credit not found")
            wallet = str(source["wallet_id"])
            self.ledger._wallet_lock(conn, tenant, wallet)
            allocated = conn.execute(
                "SELECT COALESCE(sum(points),0) AS n FROM bonuspro.return_cases WHERE tenant_id=%s AND credit_id=%s",
                (tenant, credit),
            ).fetchone()["n"]
            if allocated + points > source["delta"]:
                raise Conflict("return exceeds unallocated original credit")
            # Existing reservations may already be affecting an external document.
            # Never steal their funds or release them on a network timeout.
            available = self.ledger._snapshot(conn, tenant, wallet)["available"]
            blocked = conn.execute(
                "SELECT 1 FROM bonuspro.return_cases WHERE tenant_id=%s AND wallet_id=%s AND state='pending' LIMIT 1",
                (tenant, wallet),
            ).fetchone()
            case = str(uuid4())
            conn.execute(
                "INSERT INTO bonuspro.return_cases "
                "(tenant_id,id,wallet_id,credit_id,return_id,request_hash,points,state,requested_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s)",
                (tenant, case, wallet, credit, returned, digest, points, actor),
            )
            if available >= points and not blocked:
                self._reverse(conn, tenant, case, wallet, credit, points, digest, actor,
                              "Automatic reversal of returned purchase accrual")
            row = conn.execute("SELECT * FROM bonuspro.return_cases WHERE tenant_id=%s AND id=%s",
                               (tenant, case)).fetchone()
            return self._result(row)

    @staticmethod
    def _result(row):
        return dict(id=str(row["id"]), state=row["state"], points=row["points"],
                    transaction_id=str(row["transaction_id"]) if row["transaction_id"] else None)

    def _reverse(self, conn, tenant, case, wallet, credit, points, digest, actor, reason):
        tx = self.ledger._post(conn, tenant, wallet, "return/" + case, digest, -points,
                               "reversal", actor, reason, credit_ref=credit)
        conn.execute(
            "UPDATE bonuspro.return_cases SET state='reversed',transaction_id=%s,resolved_by=%s,"
            "resolution_reason=%s,resolved_at=now() WHERE tenant_id=%s AND id=%s",
            (tx, actor, reason, tenant, case),
        )

    def resolve(self, tenant_id, case_id, *, decision, actor_id, role, reason):
        # role must come from the server-verified MoySklad session, never request JSON.
        if role != "admin":
            raise PermissionError("administrator required")
        tenant, case, actor = map(uuid_value, (tenant_id, case_id, actor_id))
        text_value(reason, "reason", 500)
        if decision not in ("reverse", "waive"):
            raise ValueError("unknown decision")
        wanted = "reversed" if decision == "reverse" else "waived"
        with self.ledger.transaction(tenant) as conn:
            row = conn.execute("SELECT * FROM bonuspro.return_cases WHERE tenant_id=%s AND id=%s",
                               (tenant, case)).fetchone()
            if not row:
                raise NotFound("return case not found")
            wallet = str(row["wallet_id"])
            self.ledger._wallet_lock(conn, tenant, wallet)
            row = conn.execute("SELECT * FROM bonuspro.return_cases WHERE tenant_id=%s AND id=%s FOR UPDATE",
                               (tenant, case)).fetchone()
            if row["state"] != "pending":
                if (row["state"] == wanted and str(row["resolved_by"]) == actor
                        and row["resolution_reason"] == reason):
                    return self._result(row)
                raise Conflict("return case already resolved")
            if decision == "reverse":
                if self.ledger._snapshot(conn, tenant, wallet)["available"] < row["points"]:
                    raise Conflict("insufficient unreserved balance; debt is prohibited")
                self._reverse(conn, tenant, case, wallet, str(row["credit_id"]), row["points"],
                              row["request_hash"], actor, reason)
            else:
                conn.execute(
                    "UPDATE bonuspro.return_cases SET state='waived',resolved_by=%s,resolution_reason=%s,"
                    "resolved_at=now() WHERE tenant_id=%s AND id=%s", (actor, reason, tenant, case),
                )
            return self._result(conn.execute(
                "SELECT * FROM bonuspro.return_cases WHERE tenant_id=%s AND id=%s", (tenant, case),
            ).fetchone())
