"""Read-only operational evidence, never a substitute for financial acceptance."""
from bonuspro.adapters.postgres.ledger import uuid_value,NotFound

COUNT_LIMIT=1001
ATTENTION_FIELDS=("provider_errors","documents_to_review","unresolved_issues",
                  "stale_redemptions","stale_personal_discounts","delayed_jobs")

def evaluate(row):
    now=row["checked_at"];heartbeat=row["heartbeat_at"];cursor=row.get("scan_until")
    fresh=bool(heartbeat and 0<=(now-heartbeat).total_seconds()<600)
    lag=(now-cursor).total_seconds() if cursor else None
    caught_up=lag is not None and 0<=lag<=600
    attention={field:min(COUNT_LIMIT,max(0,int(row.get(field) or 0))) for field in ATTENTION_FIELDS}
    status="ready";label="Заказы обрабатываются автоматически"
    if row["state"]!="Activated":
        status="inactive"
    elif not heartbeat:
        status="not_started"
    elif not fresh:
        status="stale_cycle";label="Нет свежего подтверждения автоматической обработки"
    elif not caught_up:
        status="discovery_delayed";label="Автоматическая обработка догоняет новые заказы"
    elif row["last_error"]:
        status="provider_unavailable";label="Не удалось завершить проверку МойСклад"
    elif attention["provider_errors"]:
        count="более 1000" if attention["provider_errors"]==COUNT_LIMIT else str(attention["provider_errors"])
        status="provider_documents";label="Не удалось проверить заказы: "+count+". Откройте заказ и проверьте виджет."
    elif attention["documents_to_review"] or attention["unresolved_issues"]:
        status="documents_review";label="Проверьте документы в разделе «Возвраты и сверка»"
    elif attention["stale_redemptions"] or attention["stale_personal_discounts"]:
        status="unverified_redemptions";label="Проверьте неподтверждённые скидки и списания в разделе «Возвраты и сверка»"
    elif attention["delayed_jobs"]:
        status="queue_delayed";label="Есть заказы, обработка которых задержалась"
    checks=[
        dict(id="rules",label="Правила бонусов сохранены",complete=bool(row["has_rules"]),target="rules-form"),
        dict(id="widget",label="Настройка виджета сохранена",complete=bool(row["has_widget"]),target="widget-settings"),
        dict(id="activation",label="Программа запущена",complete=row["state"]=="Activated" and bool(row["has_cutoff"]),target="launch-settings"),
        dict(id="processing",label=label,complete=status=="ready",target="processing-settings")]
    return dict(ready=all(x["complete"] for x in checks),checks=checks,checked_at=now.isoformat(),
                last_processing_at=heartbeat.isoformat() if heartbeat else None,
                processing_status=status,scan_lag_seconds=max(0,int(lag)) if lag is not None else None,
                attention=attention,attention_counts_limited=any(x==COUNT_LIMIT for x in attention.values()))

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
                  s.scan_until,s.heartbeat_at,s.last_error,clock_timestamp() AS checked_at,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.order_monitors m
                    WHERE m.tenant_id=i.tenant_id AND outcome='provider_error' LIMIT 1001) q) AS provider_errors,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.order_monitors m
                    WHERE m.tenant_id=i.tenant_id AND outcome='document_review' LIMIT 1001) q) AS documents_to_review,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.reconciliation_issues r
                    WHERE r.tenant_id=i.tenant_id AND resolved_at IS NULL LIMIT 1001) q) AS unresolved_issues,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.reservations r
                    WHERE r.tenant_id=i.tenant_id AND state='verifying'
                      AND created_at<clock_timestamp()-interval '15 minutes' LIMIT 1001) q) AS stale_redemptions,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.personal_discount_applications a
                    WHERE a.tenant_id=i.tenant_id AND a.state='verifying'
                      AND a.created_at<clock_timestamp()-interval '15 minutes'
                      AND NOT EXISTS(SELECT 1 FROM bonuspro.reservations r
                        WHERE r.tenant_id=a.tenant_id AND r.id=a.redemption_id AND r.state='verifying'
                          AND r.created_at<clock_timestamp()-interval '15 minutes')
                    LIMIT 1001) q) AS stale_personal_discounts,
                  (SELECT count(*) FROM (SELECT 1 FROM bonuspro.jobs j
                    WHERE j.tenant_id=i.tenant_id AND state IN ('queued','running')
                      AND created_at<clock_timestamp()-interval '15 minutes' LIMIT 1001) q) AS delayed_jobs
                FROM bonuspro.installations i LEFT JOIN bonuspro.sync_state s ON s.tenant_id=i.tenant_id
                WHERE i.tenant_id=%s
            """,(tenant,)).fetchone()
            if not row:raise NotFound("installation missing")
            return evaluate(row)
