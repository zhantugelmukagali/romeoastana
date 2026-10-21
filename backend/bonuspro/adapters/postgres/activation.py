"""Recoverable provider activation: intent, remote acknowledgement, fenced completion."""
from bonuspro.adapters.postgres.ledger import Conflict,NotFound,uuid_value,text_value

class ActivationConflict(Conflict):
    CODES=frozenset(("activation_rules_required","activation_widget_required",
        "activation_configuration_changed","activation_request_changed",
        "activation_installation_changed","activation_already_active"))

    def __init__(self,code):
        if code not in self.CODES:raise ValueError("invalid activation conflict code")
        self.code=code
        super().__init__("program activation conflict")


class ActivationService:
    def __init__(self,installations):
        self.repo=installations;self.ledger=installations.ledger

    @staticmethod
    def lock(conn,tenant):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))

    def prepare(self,tenant,request_id,actor,reason):
        tenant,request_id,actor=map(uuid_value,(tenant,request_id,actor));text_value(reason,"reason",500)
        with self.ledger.transaction(tenant) as conn:
            self.lock(conn,tenant);self.ledger._key_lock(conn,tenant,"settings")
            state=conn.execute("SELECT state,generation FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not state or state["state"] not in ("SettingsRequired","Activated"):raise ActivationConflict("activation_installation_changed")
            row=conn.execute("SELECT * FROM bonuspro.activation_requests WHERE tenant_id=%s AND id=%s",(tenant,request_id)).fetchone()
            if row:
                if row["generation"]!=state["generation"]:
                    raise ActivationConflict("activation_installation_changed")
                if str(row["actor_id"])!=actor or row["reason"]!=reason:
                    raise ActivationConflict("activation_request_changed")
                return dict(row)
            if state["state"]=="Activated":raise ActivationConflict("activation_already_active")
            rule=conn.execute("SELECT version FROM bonuspro.rule_versions WHERE tenant_id=%s ORDER BY version DESC LIMIT 1",(tenant,)).fetchone()
            marker=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not rule:raise ActivationConflict("activation_rules_required")
            if not marker:raise ActivationConflict("activation_widget_required")
            return dict(conn.execute("INSERT INTO bonuspro.activation_requests(tenant_id,id,generation,actor_id,rule_version,marker_id,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant,request_id,state["generation"],actor,rule["version"],marker["marker_id"],reason)).fetchone())

    def complete(self,tenant,request_id):
        # Only the lifecycle login may transition installations. The ordinary app role cannot.
        with self.repo.lifecycle() as conn:
            conn.execute("SELECT set_config('bonuspro.tenant_id',%s,true)",(tenant,))
            self.lock(conn,tenant);self.ledger._key_lock(conn,tenant,"settings")
            state=conn.execute("SELECT state,generation FROM bonuspro.installations WHERE tenant_id=%s FOR UPDATE",(tenant,)).fetchone()
            row=conn.execute("SELECT * FROM bonuspro.activation_requests WHERE tenant_id=%s AND id=%s",(tenant,request_id)).fetchone()
            if not row:raise NotFound("activation intent missing")
            if not state or state["generation"]!=row["generation"] or state["state"] not in ("SettingsRequired","Activated"):
                raise ActivationConflict("activation_installation_changed")
            completed=conn.execute("SELECT 1 FROM bonuspro.activation_completions WHERE tenant_id=%s AND request_id=%s",(tenant,request_id)).fetchone()
            if completed and state["state"]=="Activated":
                cutoff=conn.execute("SELECT starts_at FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone()
                if not cutoff:raise ActivationConflict("activation_configuration_changed")
                return {"state":"Activated","starts_at":cutoff["starts_at"].isoformat()}
            rule=conn.execute("SELECT version FROM bonuspro.rule_versions WHERE tenant_id=%s ORDER BY version DESC LIMIT 1",(tenant,)).fetchone()
            marker=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not rule or not marker or rule["version"]!=row["rule_version"] or marker["marker_id"]!=row["marker_id"]:
                raise ActivationConflict("activation_configuration_changed")
            conn.execute("INSERT INTO bonuspro.activation_completions(tenant_id,request_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",(tenant,request_id))
            conn.execute("UPDATE bonuspro.installations SET state='Activated',updated_at=clock_timestamp() WHERE tenant_id=%s",(tenant,))
            cutoff=conn.execute("SELECT starts_at FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone()
            if not cutoff:
                cutoff=conn.execute("INSERT INTO bonuspro.automation_config(tenant_id,actor_id) VALUES (%s,%s) RETURNING starts_at",
                                    (tenant,row["actor_id"])).fetchone()
                conn.execute("INSERT INTO bonuspro.sync_state(tenant_id,scan_until) VALUES (%s,%s)",(tenant,cutoff["starts_at"]))
            return {"state":"Activated","starts_at":cutoff["starts_at"].isoformat()}

    def activate(self,tenant,request_id,actor,reason,provider):
        row=self.prepare(tenant,request_id,actor,reason)
        with self.ledger.transaction(tenant) as conn:
            completed=conn.execute("SELECT 1 FROM bonuspro.activation_completions WHERE tenant_id=%s AND request_id=%s",
                                   (tenant,request_id)).fetchone()
        if not completed:provider.activate(tenant)
        return self.complete(tenant,request_id)
