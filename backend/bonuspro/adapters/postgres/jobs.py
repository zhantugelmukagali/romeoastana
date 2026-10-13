"""Durable at-least-once queue; remote effects MUST be reconciled idempotently.

The connection-taking enqueue/complete methods make local business changes and
queue acknowledgement one transaction. Never perform network calls while holding
the database lock; collect provider evidence first and revalidate before apply.
A lease prevents stale workers from committing locally, not remote side effects.
"""
from secrets import randbelow
from uuid import uuid4
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, fingerprint, uuid_value, text_value
from bonuspro.domain.finance import natural

ERROR_CODES = frozenset(("provider_timeout","provider_unavailable","conflict","invalid_document"))


class Jobs:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def enqueue_in(conn, tenant_id, *, order_id, key):
        tenant, order = uuid_value(tenant_id), uuid_value(order_id)
        text_value(key,"key",128)
        digest = fingerprint(dict(kind="reconcile_order",order=order))
        Ledger._key_lock(conn,tenant,"job/"+key)
        prior=conn.execute("SELECT id,request_hash FROM bonuspro.jobs WHERE tenant_id=%s AND operation_key=%s",
                           (tenant,key)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                raise Conflict("job idempotency key changed")
            return str(prior["id"])
        job=str(uuid4())
        conn.execute("INSERT INTO bonuspro.jobs(tenant_id,id,operation_key,request_hash,kind,entity_id) "
                     "VALUES (%s,%s,%s,%s,'reconcile_order',%s)",(tenant,job,key,digest,order))
        return job

    def enqueue(self, tenant_id, *, order_id, key):
        with self.ledger.transaction(tenant_id) as conn:
            return self.enqueue_in(conn,tenant_id,order_id=order_id,key=key)

    def claim(self, tenant_id, *, lease_seconds=60):
        tenant=uuid_value(tenant_id)
        natural(lease_seconds,"lease_seconds",300)
        if lease_seconds<10:
            raise ValueError("lease must be between 10 and 300 seconds")
        with self.ledger.transaction(tenant) as conn:
            # Reap exhausted crashed workers without contending with active workers.
            conn.execute(
                "WITH expired AS (SELECT id FROM bonuspro.jobs WHERE tenant_id=%s AND attempts>=8 "
                "AND (state='queued' OR (state='running' AND lease_until<=clock_timestamp())) "
                "ORDER BY available_at LIMIT 100 FOR UPDATE SKIP LOCKED) "
                "UPDATE bonuspro.jobs j SET state='dead',lease_id=NULL,lease_until=NULL,"
                "last_error='attempts_exhausted' FROM expired WHERE j.tenant_id=%s AND j.id=expired.id",
                (tenant,tenant))
            row=conn.execute(
                "SELECT * FROM bonuspro.jobs WHERE tenant_id=%s AND attempts<8 AND "
                "((state='queued' AND available_at<=clock_timestamp()) OR "
                "(state='running' AND lease_until<=clock_timestamp())) "
                "ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",(tenant,)).fetchone()
            if not row:
                return None
            lease=str(uuid4())
            return conn.execute(
                "UPDATE bonuspro.jobs SET state='running',attempts=attempts+1,lease_id=%s,"
                "lease_until=clock_timestamp()+%s*interval '1 second' WHERE tenant_id=%s AND id=%s RETURNING *",
                (lease,lease_seconds,tenant,row["id"])).fetchone()

    @staticmethod
    def complete_in(conn, tenant_id, job_id, lease_id, *, apply=None):
        tenant,job,lease=map(uuid_value,(tenant_id,job_id,lease_id))
        row=conn.execute(
            "SELECT * FROM bonuspro.jobs WHERE tenant_id=%s AND id=%s AND state='running' "
            "AND lease_id=%s AND lease_until>clock_timestamp() FOR UPDATE",(tenant,job,lease)).fetchone()
        if not row:
            raise Conflict("job lease expired or replaced")
        if apply:
            apply(conn,row)
        updated=conn.execute(
            "UPDATE bonuspro.jobs SET state='done',lease_id=NULL,lease_until=NULL,last_error=NULL "
            "WHERE tenant_id=%s AND id=%s AND lease_id=%s AND lease_until>clock_timestamp() RETURNING id",
            (tenant,job,lease)).fetchone()
        if not updated:
            # The whole surrounding transaction must roll back, including apply().
            raise Conflict("job lease expired during local application")

    def complete(self, tenant_id, job_id, lease_id, *, apply=None):
        with self.ledger.transaction(tenant_id) as conn:
            self.complete_in(conn,tenant_id,job_id,lease_id,apply=apply)

    def fail(self, tenant_id, job_id, lease_id, *, error_code, permanent=False, retry_after=0):
        tenant,job,lease=map(uuid_value,(tenant_id,job_id,lease_id))
        natural(retry_after,"retry_after",86400)
        if error_code not in ERROR_CODES or not isinstance(permanent,bool):
            raise ValueError("invalid safe error classification")
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute(
                "SELECT attempts FROM bonuspro.jobs WHERE tenant_id=%s AND id=%s AND state='running' "
                "AND lease_id=%s AND lease_until>clock_timestamp() FOR UPDATE",(tenant,job,lease)).fetchone()
            if not row:
                raise Conflict("job lease expired or replaced")
            terminal=permanent or row["attempts"]>=8
            delay=max(retry_after,min(300,2**row["attempts"])+randbelow(5))
            conn.execute(
                "UPDATE bonuspro.jobs SET state=%s,lease_id=NULL,lease_until=NULL,last_error=%s,"
                "available_at=clock_timestamp()+%s*interval '1 second' WHERE tenant_id=%s AND id=%s",
                ("dead" if terminal else "queued",error_code,delay,tenant,job))
