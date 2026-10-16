import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from bonuspro.adapters.postgres.ledger import Ledger, Conflict
from bonuspro.adapters.postgres.jobs import Jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tenant=str(uuid4())
        self.ledger=Ledger(os.environ["TEST_APP_DSN"])
        self.jobs=Jobs(self.ledger)
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            self.assertEqual(conn.info.dbname,"bonuspro_ci")
            conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s)",(self.tenant,))

    def enqueue(self, key="event-1", order=None):
        return self.jobs.enqueue(self.tenant,order_id=order or str(uuid4()),key=key)

    def expire(self, job):
        with psycopg.connect(os.environ["TEST_ADMIN_DSN"]) as conn:
            conn.execute("UPDATE bonuspro.jobs SET lease_until=clock_timestamp()-interval '1 second' "
                         "WHERE tenant_id=%s AND id=%s",(self.tenant,job["id"]))

    def test_duplicate_event_and_changed_payload(self):
        order=str(uuid4())
        job=self.enqueue(order=order)
        self.assertEqual(job,self.enqueue(order=order))
        with self.assertRaises(Conflict):
            self.enqueue()

    def test_parallel_claims_deliver_one_lease(self):
        self.enqueue()
        barrier=Barrier(2)
        def claim(_):
            barrier.wait(timeout=10)
            return self.jobs.claim(self.tenant)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,range(2)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_crashed_worker_is_reclaimed_and_old_worker_fenced(self):
        self.enqueue()
        old=self.jobs.claim(self.tenant)
        self.expire(old)
        new=self.jobs.claim(self.tenant)
        self.assertEqual(old["id"],new["id"])
        self.assertNotEqual(old["lease_id"],new["lease_id"])
        self.assertEqual(new["attempts"],2)
        with self.assertRaises(Conflict):
            self.jobs.complete(self.tenant,old["id"],old["lease_id"])
        with self.assertRaises(Conflict):
            self.jobs.fail(self.tenant,old["id"],old["lease_id"],error_code="provider_timeout")
        self.jobs.complete(self.tenant,new["id"],new["lease_id"])
        self.assertIsNone(self.jobs.claim(self.tenant))

    def test_expired_lease_cannot_acknowledge_before_reclaim(self):
        self.enqueue()
        job=self.jobs.claim(self.tenant)
        self.expire(job)
        with self.assertRaises(Conflict):
            self.jobs.complete(self.tenant,job["id"],job["lease_id"])

    def test_enqueue_rolls_back_with_business_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction(self.tenant) as conn:
                self.jobs.enqueue_in(conn,self.tenant,order_id=str(uuid4()),key="rollback")
                raise RuntimeError("Synthetic failure")
        self.assertIsNone(self.jobs.claim(self.tenant))

    def test_local_effect_and_acknowledgement_rollback_together(self):
        self.enqueue()
        job=self.jobs.claim(self.tenant)
        wallet=str(uuid4())
        def broken(conn,row):
            conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(self.tenant,wallet))
            raise RuntimeError("Synthetic interrupted application")
        with self.assertRaises(RuntimeError):
            self.jobs.complete(self.tenant,job["id"],job["lease_id"],apply=broken)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM bonuspro.wallets WHERE id=%s",(wallet,)).fetchone())
            self.assertEqual(conn.execute("SELECT state FROM bonuspro.jobs WHERE id=%s",(job["id"],)).fetchone()["state"],"running")
        def success(conn,row):
            conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(self.tenant,wallet))
        self.jobs.complete(self.tenant,job["id"],job["lease_id"],apply=success)
        with self.ledger.transaction(self.tenant) as conn:
            self.assertIsNotNone(conn.execute("SELECT id FROM bonuspro.wallets WHERE id=%s",(wallet,)).fetchone())

    def test_retry_has_delay_and_safe_error_only(self):
        self.enqueue()
        job=self.jobs.claim(self.tenant)
        with self.assertRaises(ValueError):
            self.jobs.fail(self.tenant,job["id"],job["lease_id"],error_code="secret provider response")
        self.jobs.fail(self.tenant,job["id"],job["lease_id"],error_code="provider_timeout")
        self.assertIsNone(self.jobs.claim(self.tenant))
        with self.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT * FROM bonuspro.jobs WHERE id=%s",(job["id"],)).fetchone()
            self.assertEqual(row["state"],"queued")
            self.assertEqual(row["last_error"],"provider_timeout")
            self.assertIsNone(row["lease_id"])

    def test_exhausted_crashed_worker_becomes_dead(self):
        self.enqueue()
        for i in range(8):
            job=self.jobs.claim(self.tenant)
            self.assertEqual(job["attempts"],i+1)
            self.expire(job)
        self.assertIsNone(self.jobs.claim(self.tenant))
        with self.ledger.transaction(self.tenant) as conn:
            row=conn.execute("SELECT state,last_error FROM bonuspro.jobs").fetchone()
            self.assertEqual((row["state"],row["last_error"]),("dead","attempts_exhausted"))

    def test_permanent_failure_is_not_retried(self):
        self.enqueue()
        job=self.jobs.claim(self.tenant)
        self.jobs.fail(self.tenant,job["id"],job["lease_id"],error_code="invalid_document",permanent=True)
        self.assertIsNone(self.jobs.claim(self.tenant))

    def test_tenant_cannot_claim_or_acknowledge_other_shop(self):
        self.enqueue()
        other=str(uuid4())
        self.assertIsNone(self.jobs.claim(other))
        job=self.jobs.claim(self.tenant)
        with self.assertRaises(Conflict):
            self.jobs.complete(other,job["id"],job["lease_id"])

    def test_terminal_job_cannot_be_rewritten(self):
        self.enqueue()
        job=self.jobs.claim(self.tenant)
        self.jobs.complete(self.tenant,job["id"],job["lease_id"])
        with self.ledger.transaction(self.tenant) as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE bonuspro.jobs SET state='queued' WHERE id=%s",(job["id"],))
