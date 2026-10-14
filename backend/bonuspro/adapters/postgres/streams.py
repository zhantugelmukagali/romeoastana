"""Bounded incremental scans plus a resumable whole-program creation rescan."""
from datetime import timedelta
from uuid import uuid5,NAMESPACE_URL
from math import ceil
from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import fingerprint
from bonuspro.events import KINDS
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.ledger import Conflict
from bonuspro.adapters.moysklad.remap import ScanTooLarge

class Streams:
    def __init__(self,ledger,signals):
        self.ledger=ledger;self.signals=signals

    def next(self,tenant,now):
        with self.ledger.transaction(tenant) as conn:
            config=conn.execute("SELECT starts_at FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone()
            if not config:return None
            installation=conn.execute("SELECT generation FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not installation:return None
            for kind in (*KINDS,"created_rescan"):
                conn.execute("INSERT INTO bonuspro.sync_streams(tenant_id,stream,cursor_at,generation) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT(tenant_id,stream) DO UPDATE SET generation=EXCLUDED.generation,cursor_at=EXCLUDED.cursor_at,round_until=NULL,ready_at=now(),checked_at=NULL "
                    "WHERE bonuspro.sync_streams.generation<>EXCLUDED.generation",
                    (tenant,kind,config["starts_at"],installation["generation"]))
            row=conn.execute("SELECT * FROM bonuspro.sync_streams WHERE tenant_id=%s AND ready_at<=%s "
                "ORDER BY checked_at NULLS FIRST,stream LIMIT 1",(tenant,now)).fetchone()
            if row:row["starts_at"]=config["starts_at"]
            return row

    def scan(self,tenant,client,now):
        row=self.next(tenant,now)
        if not row:return False
        frontier=now-timedelta(seconds=30)
        rescan=row["stream"]=="created_rescan"
        ceiling=(row["round_until"] or frontier) if rescan else frontier
        end=min(ceiling,row["cursor_at"]+timedelta(seconds=row["window_seconds"]))
        if end<row["cursor_at"]:return False
        start=max(row["starts_at"],row["cursor_at"]-timedelta(seconds=min(120,row["window_seconds"]//2)))
        kind="customerorder" if rescan else row["stream"]
        field="created" if rescan else "updated"
        try:
            identities=client.changed_ids_between(kind,field,start,end)

        except ScanTooLarge:
            with self.ledger.transaction(tenant) as conn:
                if row["window_seconds"]<=1:raise
                conn.execute("UPDATE bonuspro.sync_streams SET window_seconds=GREATEST(1,window_seconds/2),checked_at=%s "
                    "WHERE tenant_id=%s AND stream=%s AND cursor_at=%s",(now,tenant,row["stream"],row["cursor_at"]))
            return False
        with self.ledger.transaction(tenant) as conn:
            current=conn.execute("SELECT cursor_at,generation FROM bonuspro.sync_streams WHERE tenant_id=%s AND stream=%s FOR UPDATE",
                (tenant,row["stream"])).fetchone()
            if not current or (current["cursor_at"]!=row["cursor_at"] or current["generation"]!=row["generation"]):raise Conflict("stream cursor changed")
            if kind=="customerorder":
                for order in identities:
                    if rescan:
                        conn.execute("INSERT INTO bonuspro.order_monitors(tenant_id,order_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",(tenant,order))
                    else:Automation.hint_in(conn,tenant,order)
            elif identities:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("inbox/"+tenant,))
                pending=conn.execute("SELECT count(*) AS n FROM bonuspro.webhook_inbox WHERE tenant_id=%s AND state='queued'",(tenant,)).fetchone()["n"]
                if pending+ceil(len(identities)/100)>1000:raise Conflict("notification capacity exhausted")
                generation=conn.execute("SELECT generation FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()["generation"]
                for offset in range(0,len(identities),100):
                    events=[dict(kind=kind,id=i,action="UPDATE") for i in identities[offset:offset+100]]
                    identity=str(uuid5(NAMESPACE_URL,"bonuspro/stream/"+tenant+"/"+str(generation)+"/"+kind+"/"+row["cursor_at"].isoformat()+"/"+end.isoformat()+"/"+str(offset)))
                    conn.execute("INSERT INTO bonuspro.webhook_inbox(tenant_id,generation,request_id,payload_hash,events) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (tenant,generation,identity,fingerprint(events),Jsonb(events)))
            finished=rescan and end>=ceiling
            cursor=row["starts_at"] if finished else end
            ready=now+timedelta(hours=6) if finished else (now+timedelta(seconds=60) if end>=ceiling else now)
            window=min(604800,row["window_seconds"]*2) if len(identities)<100 else row["window_seconds"]
            conn.execute("UPDATE bonuspro.sync_streams SET cursor_at=%s,round_until=%s,ready_at=%s,checked_at=%s,window_seconds=%s "
                "WHERE tenant_id=%s AND stream=%s",
                (cursor,None if finished or not rescan else ceiling,ready,now,window,tenant,row["stream"]))
        return True
