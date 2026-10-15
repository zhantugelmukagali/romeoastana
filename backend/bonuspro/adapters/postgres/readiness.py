"""Read-only operational evidence, never a substitute for financial acceptance."""
from bonuspro.adapters.postgres.ledger import uuid_value,NotFound

def evaluate(row):
    now=row["checked_at"];heartbeat=row["heartbeat_at"]
    fresh=bool(heartbeat and 0<=(now-heartbeat).total_seconds()<600)
    checks=[
        dict(id="rules",label="Правила бонусов сохранены",complete=bool(row["has_rules"]),target="rules-form"),
        dict(id="widget",label="Виджет подключён",complete=bool(row["has_widget"]),target="widget-settings"),
        dict(id="activation",label="Программа запущена",complete=row["state"]=="Activated" and bool(row["has_cutoff"]),target="launch-settings"),
        dict(id="processing",label="Заказы обрабатываются автоматически",complete=fresh and not row["last_error"] and row["state"]=="Activated",target="processing-settings")]
    return dict(ready=all(x["complete"] for x in checks),checks=checks,checked_at=now.isoformat(),
                last_processing_at=heartbeat.isoformat() if heartbeat else None)

class Readiness:
    def __init__(self,ledger):self.ledger=ledger
    def report(self,tenant):
        tenant=uuid_value(tenant)
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("""
                SELECT i.state,
                  EXISTS(SELECT 1 FROM bonuspro.rule_versions WHERE tenant_id=i.tenant_id) AS has_rules,
                  EXISTS(SELECT 1 FROM bonuspro.widget_bindings WHERE tenant_id=i.tenant_id) AS has_widget,
                  EXISTS(SELECT 1 FROM bonuspro.automation_config WHERE tenant_id=i.tenant_id) AS has_cutoff,
                  s.heartbeat_at,s.last_error,clock_timestamp() AS checked_at
                FROM bonuspro.installations i LEFT JOIN bonuspro.sync_state s ON s.tenant_id=i.tenant_id
                WHERE i.tenant_id=%s
            """,(tenant,)).fetchone()
            if not row:raise NotFound("installation missing")
            return evaluate(row)
