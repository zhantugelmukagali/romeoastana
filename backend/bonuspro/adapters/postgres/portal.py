"""Read-only loyalty cabinet. Financial values come exclusively from tenant journals."""
from bonuspro.adapters.postgres.ledger import Ledger,NotFound,uuid_value

class Portal:
    def __init__(self,ledger:Ledger):self.ledger=ledger

    def summary(self,tenant):
        tenant=uuid_value(tenant)
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("""
              SELECT
                (SELECT count(*) FROM bonuspro.customer_wallets WHERE tenant_id=%(tenant)s) AS customers,
                (SELECT COALESCE(sum(amount),0) FROM bonuspro.ledger_postings WHERE tenant_id=%(tenant)s AND account='customer') AS balance,
                (SELECT COALESCE(sum(points),0) FROM bonuspro.reservations WHERE tenant_id=%(tenant)s AND state IN ('held','verifying')) AS reserved,
                (SELECT COALESCE(sum(delta),0) FROM bonuspro.ledger_transactions WHERE tenant_id=%(tenant)s AND kind='credit') AS credited,
                (SELECT COALESCE(-sum(delta),0) FROM bonuspro.ledger_transactions WHERE tenant_id=%(tenant)s AND kind='redemption') AS redeemed,
                (SELECT count(*) FROM bonuspro.return_cases WHERE tenant_id=%(tenant)s AND state='pending')+
                (SELECT count(*) FROM bonuspro.settlement_cases WHERE tenant_id=%(tenant)s AND state='pending')+
                (SELECT count(*) FROM bonuspro.reconciliation_issues WHERE tenant_id=%(tenant)s AND resolved_at IS NULL) AS attention,
                (SELECT state FROM bonuspro.installations WHERE tenant_id=%(tenant)s) AS state,
                (SELECT json_build_object('earn_bps',earn_bps,'spend_bps',spend_bps,'version',version)
                 FROM bonuspro.rule_versions WHERE tenant_id=%(tenant)s ORDER BY version DESC LIMIT 1) AS rules,
                (SELECT json_build_object('heartbeat_at',heartbeat_at,'last_error',last_error)
                 FROM bonuspro.sync_state WHERE tenant_id=%(tenant)s) AS processing,
                clock_timestamp() AS generated_at
            """,{"tenant":tenant}).fetchone()
            result={key:str(int(row[key])) for key in ("balance","reserved","credited","redeemed")}
            result.update(customers=int(row["customers"]),attention=int(row["attention"]))
            result.update(state=row["state"],rules=row["rules"],processing=row["processing"],generated_at=row["generated_at"].isoformat())
            return result

    def balances(self,tenant,customers):
        tenant=uuid_value(tenant)
        ids=[uuid_value(value) for value in customers]
        if len(ids)>25 or len(set(ids))!=len(ids):raise ValueError("bounded unique customer list required")
        if not ids:return {}
        with self.ledger.transaction(tenant) as conn:
            rows=conn.execute("""
              SELECT c.counterparty_id,
                COALESCE((SELECT sum(p.amount) FROM bonuspro.ledger_postings p WHERE p.tenant_id=c.tenant_id
                  AND p.wallet_id=c.wallet_id AND p.account='customer'),0) AS balance,
                COALESCE((SELECT sum(r.points) FROM bonuspro.reservations r WHERE r.tenant_id=c.tenant_id
                  AND r.wallet_id=c.wallet_id AND r.state IN ('held','verifying')),0) AS reserved
              FROM bonuspro.customer_wallets c WHERE c.tenant_id=%s AND c.counterparty_id=ANY(%s::uuid[])
            """,(tenant,ids)).fetchall()
            return {str(row["counterparty_id"]):dict(enrolled=True,balance=int(row["balance"]),
                reserved=int(row["reserved"]),available=max(0,int(row["balance"])-int(row["reserved"]))) for row in rows}

    def activity(self,tenant,*,customer=None,before=None):
        tenant=uuid_value(tenant)
        customer=uuid_value(customer) if customer else None
        before=uuid_value(before) if before else None
        with self.ledger.transaction(tenant) as conn:
            cursor=None
            if before:
                cursor=conn.execute("SELECT t.created_at,t.id FROM bonuspro.ledger_transactions t "
                    "LEFT JOIN bonuspro.customer_wallets c ON c.tenant_id=t.tenant_id AND c.wallet_id=t.wallet_id "
                    "WHERE t.tenant_id=%s AND t.id=%s AND (%s::uuid IS NULL OR c.counterparty_id=%s::uuid)",
                    (tenant,before,customer,customer)).fetchone()
                if not cursor:raise NotFound("activity cursor not found")
            rows=conn.execute("""
              SELECT t.id,t.kind,t.delta,t.created_at,c.counterparty_id,
                COALESCE(r.order_id,a.order_id) AS order_id,
                EXISTS(SELECT 1 FROM bonuspro.opening_balances o WHERE o.tenant_id=t.tenant_id AND o.transaction_id=t.id) AS opening
              FROM bonuspro.ledger_transactions t
              LEFT JOIN bonuspro.customer_wallets c ON c.tenant_id=t.tenant_id AND c.wallet_id=t.wallet_id
              LEFT JOIN bonuspro.reservations r ON r.tenant_id=t.tenant_id AND r.id=t.reservation_id
              LEFT JOIN bonuspro.order_accruals a ON a.tenant_id=t.tenant_id AND a.credit_id=t.id
              WHERE t.tenant_id=%s AND (%s::uuid IS NULL OR c.counterparty_id=%s::uuid)
                AND (%s::uuid IS NULL OR (t.created_at,t.id)<(%s::timestamptz,%s::uuid))
              ORDER BY t.created_at DESC,t.id DESC LIMIT 51
            """,(tenant,customer,customer,before,cursor["created_at"] if cursor else None,before)).fetchall()
            items=[dict(id=str(r["id"]),kind="opening" if r["opening"] else r["kind"],points=int(r["delta"]),
                created_at=r["created_at"].isoformat(),
                customer_id=str(r["counterparty_id"]) if r["counterparty_id"] else None,
                order_id=str(r["order_id"]) if r["order_id"] else None) for r in rows[:50]]
            return dict(items=items,next_cursor=items[-1]["id"] if len(rows)>50 else None)
