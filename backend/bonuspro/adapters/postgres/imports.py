from uuid import uuid4
from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import Conflict,NotFound,uuid_value,text_value,fingerprint
from bonuspro.importing import parse_csv,ImportInvalid


class Imports:
    def __init__(self,ledger):
        self.ledger=ledger

    def _installation_in(self,conn,tenant):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
        state=conn.execute("SELECT state,generation FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
        if not state or state["state"]!="SettingsRequired" or conn.execute(
            "SELECT 1 FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone():
            raise Conflict("opening migration is only allowed before program launch")
        return state

    def generation(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            return str(self._installation_in(conn,tenant)["generation"])

    def preview(self,tenant,csv_text,actor,client):
        rows,digest,source_hash=parse_csv(csv_text)
        generation=self.generation(tenant)
        named=[]
        for row in rows:
            doc=client.entity("counterparty",row["counterparty_id"])
            if doc.get("archived"):raise ImportInvalid("В файле есть архивный контрагент. Сначала исправьте сопоставление.")
            named.append({**row,"name":str(doc.get("name",""))[:256]})
        with self.ledger.transaction(tenant) as conn:
            state=self._installation_in(conn,tenant)
            if str(state["generation"])!=generation:raise Conflict("installation changed")
            existing=conn.execute("SELECT b.* FROM bonuspro.import_batches b WHERE tenant_id=%s AND manifest_hash=%s AND generation=%s AND (expires_at>clock_timestamp() OR EXISTS(SELECT 1 FROM bonuspro.import_commits c WHERE c.tenant_id=b.tenant_id AND c.batch_id=b.id)) ORDER BY created_at DESC LIMIT 1",
                                 (tenant,digest,generation)).fetchone()
            if existing:return self._public(existing)
            # Preview shows conflicts before asking the administrator to commit.
            for row in rows:
                if conn.execute("SELECT 1 FROM bonuspro.opening_balances WHERE tenant_id=%s AND counterparty_id=%s",
                                (tenant,row["counterparty_id"])).fetchone():
                    raise Conflict("customer opening balance already imported")
            batch=conn.execute("INSERT INTO bonuspro.import_batches "
                "(tenant_id,id,manifest_hash,source_hash,generation,actor_id,rows,total) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant,str(uuid4()),digest,source_hash,generation,uuid_value(actor),Jsonb(named),sum(x["points"] for x in rows))).fetchone()
            return self._public(batch)

    @staticmethod
    def _public(row):
        return dict(id=str(row["id"]),digest=row["manifest_hash"],source_hash=row["source_hash"],
                    total=row["total"],rows=row["rows"],expires_at=row["expires_at"].isoformat())

    def commit(self,tenant,batch_id,*,digest,actor,reason):
        batch_id=uuid_value(batch_id);actor=uuid_value(actor);text_value(reason,"reason",500)
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"import/"+batch_id)
            batch=conn.execute("SELECT *,expires_at>clock_timestamp() AS fresh FROM bonuspro.import_batches WHERE tenant_id=%s AND id=%s",
                               (tenant,batch_id)).fetchone()
            if not batch:raise NotFound("import preview missing")
            if batch["manifest_hash"]!=digest:raise Conflict("preview digest changed")
            done=conn.execute("SELECT * FROM bonuspro.import_commits WHERE tenant_id=%s AND batch_id=%s",(tenant,batch_id)).fetchone()
            if done:
                if str(done["actor_id"])!=actor or done["reason"]!=reason:raise Conflict("already committed by another command")
                return dict(committed=True,total=batch["total"],customers=len(batch["rows"]))
            state=self._installation_in(conn,tenant)
            if state["generation"]!=batch["generation"] or not batch["fresh"]:raise Conflict("preview expired or installation changed")
            self.ledger._key_lock(conn,tenant,"settings")
            conn.execute("INSERT INTO bonuspro.import_commits(tenant_id,batch_id,actor_id,reason) VALUES (%s,%s,%s,%s)",
                         (tenant,batch_id,actor,reason))
            for row in batch["rows"]:
                customer=row["counterparty_id"];points=row["points"]
                if conn.execute("SELECT 1 FROM bonuspro.opening_balances WHERE tenant_id=%s AND counterparty_id=%s",(tenant,customer)).fetchone():
                    raise Conflict("customer already imported")
                link=conn.execute("SELECT wallet_id FROM bonuspro.customer_wallets WHERE tenant_id=%s AND counterparty_id=%s",
                                  (tenant,customer)).fetchone()
                wallet=str(link["wallet_id"]) if link else str(uuid4())
                if not link:
                    conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(tenant,wallet))
                    conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",(tenant,customer,wallet))
                self.ledger._wallet_lock(conn,tenant,wallet)
                if conn.execute("SELECT 1 FROM bonuspro.ledger_transactions WHERE tenant_id=%s AND wallet_id=%s LIMIT 1",(tenant,wallet)).fetchone() or conn.execute(
                    "SELECT 1 FROM bonuspro.reservations WHERE tenant_id=%s AND wallet_id=%s LIMIT 1",(tenant,wallet)).fetchone():
                    raise Conflict("opening import requires a wallet without operations")
                tx=None
                if points:
                    tx=self.ledger._post(conn,tenant,wallet,"opening/"+customer,
                        fingerprint(dict(batch=batch_id,customer=customer,points=points)),
                        points,"credit",actor,reason)
                conn.execute("INSERT INTO bonuspro.opening_balances(tenant_id,counterparty_id,wallet_id,batch_id,points,transaction_id) VALUES (%s,%s,%s,%s,%s,%s)",
                             (tenant,customer,wallet,batch_id,points,tx))
            return dict(committed=True,total=batch["total"],customers=len(batch["rows"]))

    def history(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            rows=conn.execute("SELECT b.id,b.total,b.source_hash,b.created_at,jsonb_array_length(b.rows) AS customers,c.created_at AS committed_at "
                "FROM bonuspro.import_batches b LEFT JOIN bonuspro.import_commits c ON c.tenant_id=b.tenant_id AND c.batch_id=b.id "
                "WHERE b.tenant_id=%s ORDER BY b.created_at DESC LIMIT 100",(tenant,)).fetchall()
            return {"items":[dict(id=str(r["id"]),total=r["total"],customers=r["customers"],source_hash=r["source_hash"],
                created_at=r["created_at"].isoformat(),committed_at=r["committed_at"].isoformat() if r["committed_at"] else None) for r in rows]}
