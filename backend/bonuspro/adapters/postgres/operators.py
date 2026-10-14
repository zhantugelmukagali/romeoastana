from bonuspro.adapters.postgres.ledger import Conflict,NotFound,uuid_value,text_value

class OperatorDenied(PermissionError):pass

class Operators:
    def __init__(self,ledger):self.ledger=ledger

    def check_in(self,conn,identity):
        if identity.role=="admin":return
        self.ledger._key_lock(conn,identity.tenant_id,"operator/"+identity.user_id)
        row=conn.execute("SELECT enabled FROM bonuspro.operator_permissions WHERE tenant_id=%s AND user_id=%s ORDER BY version DESC LIMIT 1",
                         (identity.tenant_id,identity.user_id)).fetchone()
        if not row or not row["enabled"]:raise OperatorDenied("operator access required")

    def check(self,identity):
        with self.ledger.transaction(identity.tenant_id) as conn:self.check_in(conn,identity)

    def update(self,tenant,user,*,enabled,expected_version,request_id,actor,reason):
        tenant,user,request_id,actor=map(uuid_value,(tenant,user,request_id,actor));text_value(reason,"reason",500)
        if type(enabled) is not bool or type(expected_version) is not int or expected_version<0:raise ValueError("invalid permission")
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"operator/request/"+request_id)
            self.ledger._key_lock(conn,tenant,"operator/"+user)
            previous=conn.execute("SELECT * FROM bonuspro.operator_permissions WHERE tenant_id=%s AND request_id=%s",(tenant,request_id)).fetchone()
            if previous:
                if (str(previous["user_id"]),previous["enabled"],str(previous["actor_id"]),previous["reason"],previous["version"])!=(user,enabled,actor,reason,expected_version+1):
                    raise Conflict("permission request changed")
                return {"version":previous["version"],"enabled":previous["enabled"]}
            if not conn.execute("SELECT 1 FROM bonuspro.known_operators WHERE tenant_id=%s AND user_id=%s",(tenant,user)).fetchone():
                raise NotFound("employee must open the widget first")
            current=conn.execute("SELECT version FROM bonuspro.operator_permissions WHERE tenant_id=%s AND user_id=%s ORDER BY version DESC LIMIT 1",(tenant,user)).fetchone()
            version=current["version"] if current else 0
            if version!=expected_version:raise Conflict("permissions changed")
            conn.execute("INSERT INTO bonuspro.operator_permissions(tenant_id,user_id,version,enabled,actor_id,reason,request_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (tenant,user,version+1,enabled,actor,reason,request_id))
            return {"version":version+1,"enabled":enabled}

    def listing(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            rows=conn.execute("SELECT u.user_id,u.login,u.last_seen,p.enabled,p.version,p.reason,p.actor_id FROM bonuspro.known_operators u "
                "LEFT JOIN LATERAL(SELECT enabled,version,reason,actor_id FROM bonuspro.operator_permissions "
                "WHERE tenant_id=u.tenant_id AND user_id=u.user_id ORDER BY version DESC LIMIT 1) p ON true "
                "WHERE u.tenant_id=%s ORDER BY u.last_seen DESC LIMIT 500",(tenant,)).fetchall()
            return {"items":[dict(user_id=str(r["user_id"]),login=r["login"],enabled=bool(r["enabled"]),version=r["version"] or 0,
                                 reason=r["reason"],actor_id=str(r["actor_id"]) if r["actor_id"] else None) for r in rows]}
