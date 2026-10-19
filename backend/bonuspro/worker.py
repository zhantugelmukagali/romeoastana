"""Bounded polling worker. One configured tenant; no privileged cross-tenant scan."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import signal
from threading import Event
import time

import psycopg
from bonuspro.settings import Settings
from bonuspro.security import AuthenticationFailed
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, uuid_value
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.redemptions import Redemptions
from bonuspro.adapters.postgres.signals import Signals
from bonuspro.adapters.postgres.streams import Streams
from bonuspro.adapters.moysklad.remap import RemapClient, RetryLater, InvalidDocument, EntityMissing, AccessDenied
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader

HEARTBEAT=Path("/tmp/bonuspro-worker-heartbeat")


class Worker:
    def __init__(self,ledger,installations,tenant,*,client_factory=RemapClient):
        self.ledger=ledger
        self.installations=installations
        self.tenant=uuid_value(tenant)
        self.automation=Automation(ledger)
        self.redemptions=Redemptions(ledger)
        self.client_factory=client_factory
        self.signals=Signals(ledger)
        self.streams=Streams(ledger,self.signals)

    def client(self):
        # Draft solutions have 11 requests / 3 s; leave room for interactive reads.
        return self.client_factory(self.installations.remap_token(self.tenant),self.tenant,
                                   budget_seconds=90,minimum_interval=0.35)

    def fail_job(self,job,code,*,permanent=False,delay=0):
        try:
            self.automation.jobs.fail(self.tenant,str(job["id"]),str(job["lease_id"]),
                                      error_code=code,permanent=permanent,retry_after=delay)
        except Conflict:
            pass  # A replacement owner alone can acknowledge an expired lease.

    def cycle(self,*,now=None,max_jobs=4):
        if type(max_jobs) is not int or not 1<=max_jobs<=4:raise ValueError("bounded job batch required")
        # A separate session lock prevents duplicate coordinators for this tenant.
        # No financial database transaction is held during network requests.
        with closing(psycopg.connect(self.ledger.dsn,autocommit=True,connect_timeout=5)) as lock:
            lock.execute("SET statement_timeout='5s'")
            if not lock.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                ("worker/"+self.tenant,)).fetchone()[0]:
                return "busy"
            state=self.automation.state(self.tenant)
            if not state or self.installations.state(self.tenant)["state"]!="Activated":
                return "inactive"
            now=now or datetime.now(timezone.utc)
            if state["cooldown_until"]>now:
                return "cooldown"
            try:
                cursor=state["scan_until"]
                end=min(now-timedelta(seconds=30),cursor+timedelta(minutes=5))
                start=max(state["starts_at"],cursor-timedelta(minutes=2))
                if end>=cursor and end>start:
                    orders=self.client().order_ids_between(start,end)
                    self.automation.discovery_complete(self.tenant,expected_cursor=cursor,until=end,orders=orders)
                try:
                    self.signals.drain(self.tenant,self.client())
                except RetryLater as error:
                    if error.code!="provider_timeout":raise
                    # Keep the inbox offset; its routing budget must not starve ordinary
                    # financial checks, which can populate the missing relationship index.
                    self.automation.cooldown(self.tenant,0,"provider_timeout")
                try:
                    self.streams.scan(self.tenant,self.client(),now)
                except Conflict:
                    pass # Capacity/cursor conflict preserves the window; drain jobs to free it.
                self.signals.prune(self.tenant)
                self.automation.schedule(self.tenant)
                for _ in range(max_jobs):
                    job=self.automation.jobs.claim(self.tenant,lease_seconds=300)
                    if not job:
                        break
                    try:
                        marker=self.redemptions.binding(self.tenant)
                        proof=FulfillmentReader(self.client()).read(str(job["entity_id"]),marker_id=marker)
                        self.automation.complete(self.tenant,job,proof)
                    except RetryLater as error:
                        self.fail_job(job,error.code,delay=error.delay_seconds)
                        raise
                    except (InvalidDocument,EntityMissing) as error:
                        reason="provider_document_missing" if isinstance(error,EntityMissing) else "invalid_credited_document"
                        missing_order=(isinstance(error,EntityMissing) and
                            error.requested_kind=="customerorder" and error.requested_id==str(job["entity_id"]))
                        try:
                            harmless=self.automation.invalid(self.tenant,str(job["entity_id"]),reason,job.get("hint_version"),
                                                             missing_order=missing_order,job=job)
                        except Conflict:
                            self.fail_job(job,"conflict",delay=5)
                        else:
                            if not harmless:self.fail_job(job,"invalid_document",permanent=True)
                    except (AuthenticationFailed,AccessDenied):
                        self.fail_job(job,"provider_unavailable",delay=300)
                        raise
                    except Conflict:
                        self.fail_job(job,"conflict",delay=5)
                    except Exception:
                        self.fail_job(job,"provider_unavailable",delay=30)
                        raise
                self.automation.cooldown(self.tenant,0,None)
                return "processed"
            except RetryLater as error:
                self.automation.cooldown(self.tenant,error.delay_seconds,error.code)
                return error.code
            except (AuthenticationFailed,AccessDenied):
                self.automation.cooldown(self.tenant,300,"provider_unavailable")
                return "access_unavailable"
            except (InvalidDocument,EntityMissing):
                # Discovery failed: do not advance its cursor or lose that window.
                self.automation.cooldown(self.tenant,300,"invalid_document")
                return "invalid_document"
            except Exception:
                # A failed cycle must not keep the previous successful readiness
                # signal. Store only an allowlisted classification, never provider text.
                self.automation.cooldown(self.tenant,30,"provider_unavailable")
                raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--health",action="store_true")
    args=parser.parse_args()
    if args.health:
        try:
            healthy=0<=time.time()-HEARTBEAT.stat().st_mtime<600
        except OSError:
            healthy=False
        raise SystemExit(0 if healthy else 1)
    config=Settings.from_env()
    tenant=uuid_value(os.environ["BONUSPRO_TENANT_ID"])
    worker=Worker(Ledger(config.database_url),
        Installations(config.database_url,config.lifecycle_database_url,config.credentials),tenant)
    stop=Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stop.set())
    while not stop.is_set():
        try:
            result=worker.cycle()
        except Exception:
            # Never log exception text, provider bodies, credentials or customer data.
            print("worker_cycle_failed",flush=True)
            if args.once:
                raise SystemExit(1)
            stop.wait(15)
            continue
        HEARTBEAT.touch()
        if args.once:
            print("worker_cycle:"+result,flush=True)
            return
        stop.wait(15)


if __name__=="__main__":
    main()
