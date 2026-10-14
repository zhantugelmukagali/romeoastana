"""Durable bounded notification inbox and related-document index."""
from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import Conflict,fingerprint,uuid_value
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.events import KINDS,affected_orders
from bonuspro.adapters.moysklad.remap import EntityMissing,InvalidDocument
from bonuspro.security import AuthenticationFailed

class Signals:
    def __init__(self,ledger):
        self.ledger=ledger

    def generation(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT generation,state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not row or row["state"] not in ("SettingsRequired","Activated"):
                raise AuthenticationFailed("inactive installation")
            return str(row["generation"])

    def record_setup(self,tenant,generation,count):
        with self.ledger.transaction(tenant) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            row=conn.execute("SELECT generation,state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not row or str(row["generation"])!=generation or row["state"] not in ("SettingsRequired","Activated"):
                raise Conflict("installation changed during setup")
            conn.execute("INSERT INTO bonuspro.webhook_setup(tenant_id,generation,hook_count) VALUES (%s,%s,%s) "
                "ON CONFLICT(tenant_id) DO UPDATE SET generation=EXCLUDED.generation,hook_count=EXCLUDED.hook_count,checked_at=now()",
                (tenant,generation,count))

    def ingest(self,tenant,generation,request_id,events):
        tenant,generation,request_id=map(uuid_value,(tenant,generation,request_id))
        digest=fingerprint(events)
        with self.ledger.transaction(tenant) as conn:
            # Provider expects a quick ACK. A contested/slow DB returns failure for retry/fallback.
            conn.execute("SET LOCAL lock_timeout='200ms'")
            conn.execute("SET LOCAL statement_timeout='700ms'")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("inbox/"+tenant,))
            row=conn.execute("SELECT generation,state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not row or str(row["generation"])!=generation or row["state"] not in ("SettingsRequired","Activated"):
                raise AuthenticationFailed("inactive webhook generation")
            prior=conn.execute("SELECT payload_hash FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND generation=%s AND request_id=%s",
                (tenant,generation,request_id)).fetchone()
            if prior:
                if prior["payload_hash"]!=digest:raise Conflict("request identifier reused for other events")
                return
            count=conn.execute("SELECT count(*) AS n FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND state='queued'",(tenant,)).fetchone()["n"]
            if count>=1000:raise Conflict("notification capacity exhausted")
            conn.execute("INSERT INTO bonuspro.webhook_inbox(tenant_id,generation,request_id,payload_hash,events) VALUES (%s,%s,%s,%s,%s)",
                (tenant,generation,request_id,digest,Jsonb(events)))

    @staticmethod
    def index_in(conn,tenant,order,related):
        conn.execute("INSERT INTO bonuspro.order_monitors(tenant_id,order_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",(tenant,order))
        for kind,identity in related:
            if kind not in KINDS:raise ValueError("unsupported indexed document")
            conn.execute("INSERT INTO bonuspro.related_documents(tenant_id,kind,document_id,order_id) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant,kind,uuid_value(identity),order))

    def known(self,tenant,kind,identity):
        with self.ledger.transaction(tenant) as conn:
            return {str(r["order_id"]) for r in conn.execute("SELECT order_id FROM bonuspro.related_documents "
                "WHERE tenant_id=%s AND kind=%s AND document_id=%s",(tenant,kind,identity)).fetchall()}

    def resolve(self,tenant,client,kind,identity):
        result=self.known(tenant,kind,identity)
        try:result.update(affected_orders(client,kind,identity))
        except EntityMissing:
            pass # Deletion is a hint for every previously indexed relationship.
        return result

    def drain(self,tenant,client,limit=4):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT * FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND state='queued' "
                "ORDER BY received_at,request_id LIMIT 1",(tenant,)).fetchone()
        if not row:return 0
        generation=self.generation(tenant)
        old=str(row["generation"])!=generation
        offset=row["next_event"]
        selected=[] if old else row["events"][offset:offset+limit]
        hints=set()
        for event in selected:
            # An invalid remote reference cannot become proof; leave fallback monitors in place.
            try:hints.update(self.resolve(tenant,client,event["kind"],event["id"]))
            except InvalidDocument:hints.update(self.known(tenant,event["kind"],event["id"]))
        with self.ledger.transaction(tenant) as conn:
            current=conn.execute("SELECT generation,state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            old=old or not current or str(current["generation"])!=str(row["generation"]) or current["state"]!="Activated"
            locked=conn.execute("SELECT next_event,state FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND generation=%s AND request_id=%s FOR UPDATE",
                (tenant,row["generation"],row["request_id"])).fetchone()
            if not locked or locked["state"]!="queued" or locked["next_event"]!=offset:raise Conflict("inbox progress changed")
            if not old:
                for order in sorted(hints):Automation.hint_in(conn,tenant,order)
            next_event=offset if old else offset+len(selected)
            state="discarded" if old else ("done" if next_event==len(row["events"]) else "queued")
            conn.execute("UPDATE bonuspro.webhook_inbox SET next_event=%s,state=%s,completed_at=CASE WHEN %s='queued' THEN NULL ELSE clock_timestamp() END "
                "WHERE tenant_id=%s AND generation=%s AND request_id=%s",
                (next_event,state,state,tenant,row["generation"],row["request_id"]))
        return len(selected)

    def status(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            setup=conn.execute("SELECT s.hook_count,s.checked_at FROM bonuspro.webhook_setup s JOIN bonuspro.installations i "
                "ON i.tenant_id=s.tenant_id AND i.generation=s.generation WHERE s.tenant_id=%s",(tenant,)).fetchone()
            inbox=conn.execute("SELECT count(*) AS pending,min(received_at) AS oldest FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND state='queued'",(tenant,)).fetchone()
            streams=conn.execute("SELECT stream,cursor_at,checked_at,ready_at,window_seconds FROM bonuspro.sync_streams "
                "WHERE tenant_id=%s ORDER BY stream",(tenant,)).fetchall()
        return dict(configured=bool(setup and setup["hook_count"]==21),setup=setup,inbox=inbox,streams=streams)

    def prune(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            for table,column,states in (("jobs","updated_at","('done','dead')"),("webhook_inbox","completed_at","('done','discarded')")):
                # Fixed internal table names only. Financial/audit tables are never pruned.
                keys="id" if table=="jobs" else "generation,request_id"
                sql=("WITH old AS (SELECT "+keys+" FROM bonuspro."+table+
                     " WHERE tenant_id=%s AND state IN "+states+" AND "+column+
                     "<clock_timestamp()-interval '30 days' ORDER BY "+column+
                     " LIMIT 200 FOR UPDATE SKIP LOCKED) DELETE FROM bonuspro."+table+
                     " WHERE tenant_id=%s AND ("+keys+") IN (SELECT "+keys+" FROM old)")
                conn.execute(sql,(tenant,tenant))
