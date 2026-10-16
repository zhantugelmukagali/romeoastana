"""Round-robin coordinator of active account UUIDs. No cross-tenant customer query."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
import signal,time
import psycopg
from bonuspro.settings import Settings
from bonuspro.adapters.postgres.ledger import Ledger,uuid_value
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.worker import Worker

HEARTBEAT=Path("/tmp/bonuspro-pool-heartbeat")

class WorkerPool:
    def __init__(self,ledger,installations,*,factory=Worker):
        self.ledger=ledger;self.installations=installations;self.factory=factory;self.cursor=None
    def accounts(self):
        with closing(psycopg.connect(self.ledger.dsn,autocommit=True,connect_timeout=5)) as conn:
            conn.execute("SET statement_timeout='3s'")
            privileged=conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0]
            if privileged:raise RuntimeError("coordinator must use the limited runtime role")
            if self.cursor:
                rows=conn.execute("SELECT tenant_id FROM bonuspro.worker_accounts WHERE tenant_id>%s ORDER BY tenant_id LIMIT 2",(self.cursor,)).fetchall()
            else:rows=conn.execute("SELECT tenant_id FROM bonuspro.worker_accounts ORDER BY tenant_id LIMIT 2").fetchall()
            if not rows and self.cursor:
                self.cursor=None
                rows=conn.execute("SELECT tenant_id FROM bonuspro.worker_accounts ORDER BY tenant_id LIMIT 2").fetchall()
        values=[uuid_value(row[0]) for row in rows]
        if values:self.cursor=values[-1]
        return values
    def cycle(self):
        accounts=self.accounts()
        def run(tenant):
            try:
                worker=self.factory(self.ledger,self.installations,tenant)
                worker.cycle(max_jobs=1)
                return True
            except Exception:
                # Never log provider errors, customer data or credentials.
                print("tenant_cycle_failed",flush=True)
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(run,accounts))
        return dict(accounts=len(accounts),failed=results.count(False))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--health",action="store_true")
    args=parser.parse_args()
    if args.health:
        try:healthy=0<=time.time()-HEARTBEAT.stat().st_mtime<600
        except OSError:healthy=False
        raise SystemExit(0 if healthy else 1)
    config=Settings.from_env()
    pool=WorkerPool(Ledger(config.database_url),
        Installations(config.database_url,config.lifecycle_database_url,config.credentials))
    stop=Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    while not stop.is_set():
        try:result=pool.cycle()
        except Exception:
            print("pool_cycle_failed",flush=True)
            if args.once:raise SystemExit(1)
            stop.wait(15);continue
        HEARTBEAT.touch()
        if args.once:
            print("pool_cycle:"+str(result["accounts"]))
            raise SystemExit(1 if result["failed"] else 0)
        stop.wait(15)
if __name__=="__main__":main()
