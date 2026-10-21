"""Transactional accrual and reconciliation, fenced by the durable job lease."""
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import time
from psycopg.errors import CheckViolation
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, VerifiedDiscount, fingerprint, uuid_value, MAX_POINTS
from bonuspro.adapters.postgres.jobs import Jobs
from bonuspro.adapters.postgres.settlements import Settlements
from bonuspro.domain.finance import Rules, earned_points


class Automation:
    def __init__(self,ledger: Ledger):
        self.ledger=ledger
        self.jobs=Jobs(ledger)
        self.settlements=Settlements(ledger)

    def configure(self,tenant,actor):
        tenant,actor=map(uuid_value,(tenant,actor))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"automation/setup")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
            if not active or active["state"]!="Activated":
                raise Conflict("activate the accepted installation before setting its cutoff")
            row=conn.execute("SELECT * FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone()
            if not row:
                row=conn.execute("INSERT INTO bonuspro.automation_config(tenant_id,actor_id) VALUES (%s,%s) RETURNING *",
                                 (tenant,actor)).fetchone()
                conn.execute("INSERT INTO bonuspro.sync_state(tenant_id,scan_until) VALUES (%s,%s)",(tenant,row["starts_at"]))
            return {"starts_at":row["starts_at"].isoformat(),"configured":True}

    def state(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT c.starts_at,s.scan_until,s.heartbeat_at,s.cooldown_until,s.last_error "
                "FROM bonuspro.automation_config c JOIN bonuspro.sync_state s USING(tenant_id) WHERE c.tenant_id=%s",(tenant,)).fetchone()
            return row

    @staticmethod
    def hint_in(conn,tenant,order):
        conn.execute("INSERT INTO bonuspro.order_monitors(tenant_id,order_id) VALUES (%s,%s) "
                     "ON CONFLICT (tenant_id,order_id) DO UPDATE SET next_due=LEAST(bonuspro.order_monitors.next_due,now())",
                     (tenant,order))

    def discovery_complete(self,tenant,*,expected_cursor,until,orders):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT scan_until FROM bonuspro.sync_state WHERE tenant_id=%s FOR UPDATE",(tenant,)).fetchone()
            if not row or row["scan_until"]!=expected_cursor or until<expected_cursor:
                raise Conflict("discovery cursor changed")
            for order in orders:
                self.hint_in(conn,tenant,uuid_value(order))
            conn.execute("UPDATE bonuspro.sync_state SET scan_until=%s,heartbeat_at=now(),last_error=NULL,updated_at=now() "
                         "WHERE tenant_id=%s",(until,tenant))

    def schedule(self,tenant,limit=20):
        with self.ledger.transaction(tenant) as conn:
            rows=conn.execute("SELECT * FROM bonuspro.order_monitors WHERE tenant_id=%s AND next_due<=now() "
                             "ORDER BY next_due,order_id LIMIT %s FOR UPDATE SKIP LOCKED",(tenant,limit)).fetchall()
            for row in rows:
                active=conn.execute("SELECT 1 FROM bonuspro.jobs WHERE tenant_id=%s AND entity_id=%s "
                                    "AND state IN ('queued','running') LIMIT 1",(tenant,row["order_id"])).fetchone()
                if not active:
                    self.jobs.enqueue_in(conn,tenant,order_id=str(row["order_id"]),
                                         key="poll/"+str(row["order_id"])+"/"+str(row["generation"]+1))
                conn.execute("UPDATE bonuspro.order_monitors SET generation=generation+1,next_due=now()+interval '60 seconds' "
                             "WHERE tenant_id=%s AND order_id=%s",(tenant,row["order_id"]))
            return len(rows)

    @staticmethod
    def outcome_in(conn,tenant,order,outcome):
        interval=60 if outcome not in ("credited","historical") else 300
        conn.execute("UPDATE bonuspro.order_monitors SET outcome=%s,checked_at=now(),next_due=now()+%s*interval '1 second' "
                     "WHERE tenant_id=%s AND order_id=%s",(outcome,interval,tenant,order))

    @staticmethod
    def issue_in(conn,tenant,order,wallet,reason,evidence_hash):
        conn.execute("INSERT INTO bonuspro.reconciliation_issues(tenant_id,order_id,wallet_id,reason,evidence_hash) "
                     "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",(tenant,order,wallet,reason,evidence_hash))

    def apply_in(self,conn,tenant,job,proof,*,resolution=None,actor=None):
        if time.monotonic()>proof.deadline or str(job["entity_id"])!=proof.saved.snapshot.order_id:
            raise Conflict("stale or mismatched evidence")
        saved=proof.saved;s=saved.snapshot
        if s.tenant_id!=tenant:
            raise Conflict("evidence belongs to another tenant")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
        active=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(tenant,)).fetchone()
        config=conn.execute("SELECT * FROM bonuspro.automation_config WHERE tenant_id=%s",(tenant,)).fetchone()
        if not config or not active or active["state"]!="Activated":
            raise Conflict("automation not active")
        self.ledger._key_lock(conn,tenant,"settings")
        self.ledger._key_lock(conn,tenant,"order/"+s.order_id)
        from bonuspro.adapters.postgres.presale_undo import PresaleUndo
        if PresaleUndo.stale_worker_proof_in(conn,tenant,saved):
            self.outcome_in(conn,tenant,s.order_id,"waiting_refresh")
            return
        if PresaleUndo.older_than_completed_undo_in(conn,tenant,saved):
            raise Conflict("evidence predates completed presale undo")
        context=conn.execute("SELECT * FROM bonuspro.order_contexts WHERE tenant_id=%s AND order_id=%s",(tenant,s.order_id)).fetchone()
        prior=conn.execute("SELECT * FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",(tenant,s.order_id)).fetchone()
        financial_hash=fingerprint(saved.financial())
        if prior and (prior["financial_hash"]!=financial_hash or prior["saved_marker"]!=saved.marker or proof.outcome=="document_review"):
            self.ledger._wallet_lock(conn,tenant,str(prior["wallet_id"]))
            self.issue_in(conn,tenant,s.order_id,str(prior["wallet_id"]),"changed_credited_order",proof.evidence_hash)
            self.outcome_in(conn,tenant,s.order_id,"document_review")
            if resolution:raise Conflict("correct the sale document before resolving its return")
            return
        attempt=conn.execute("SELECT a.*,r.quote_hash,r.points,r.state FROM bonuspro.redemption_attempts a "
            "JOIN bonuspro.reservations r ON r.tenant_id=a.tenant_id AND r.id=a.id WHERE a.tenant_id=%s AND a.order_id=%s "
            "AND r.state IN ('verifying','committed')",
            (tenant,s.order_id)).fetchone()
        from bonuspro.adapters.postgres.personal_applications import PersonalApplications
        personal=PersonalApplications.existing_in(conn,tenant,s.order_id)
        if personal:
            if saved.marker!=personal["marker"] or saved.financial()!=personal["plan"]["after"]:
                self.outcome_in(conn,tenant,s.order_id,"redemption_pending")
                return
            PersonalApplications(self.ledger).confirm_in(conn,tenant,personal,saved,worker=True)
        bonus_minor=0
        if attempt:
            if saved.marker!=attempt["marker"] or saved.financial()!=attempt["plan"]["after"] :
                self.outcome_in(conn,tenant,s.order_id,"redemption_pending")
                return
            self.ledger.confirm_discount_in(conn,tenant,str(attempt["id"]),proof=VerifiedDiscount(
                s.order_id,attempt["quote_hash"],attempt["points"]*100,s.revision))
            bonus_minor=attempt["points"]*100
        elif saved.marker and not personal:
            if context:
                self.ledger._wallet_lock(conn,tenant,str(context["wallet_id"]))
                self.issue_in(conn,tenant,s.order_id,str(context["wallet_id"]),"unrecognized_bonus_marker",proof.evidence_hash)
            self.outcome_in(conn,tenant,s.order_id,"document_review")
            return
        if prior:
            rule=conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s AND version=%s",
                              (tenant,context["rule_version"])).fetchone()
            outcome=self.settlements.apply_in(conn,tenant,proof,context,prior,attempt,
                Rules(rule["earn_bps"],rule["spend_bps"]),actor or str(config["actor_id"]),resolution=resolution)
            self.outcome_in(conn,tenant,s.order_id,outcome)
            return
        if proof.created_at<config["starts_at"]:
            self.outcome_in(conn,tenant,s.order_id,"historical")
            return
        if context:
            try:
                # A preview may have enrolled the previous buyer without any
                # order effects. Roll back a new wallet too if history pins it.
                with conn.transaction():
                    link=conn.execute("SELECT wallet_id FROM bonuspro.customer_wallets WHERE tenant_id=%s AND counterparty_id=%s",
                                      (tenant,s.counterparty_id)).fetchone()
                    if not link:
                        destination=str(uuid4())
                        conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(tenant,destination))
                        conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                                     (tenant,s.counterparty_id,destination))
                    context=conn.execute("SELECT * FROM bonuspro.rebind_order_customer(%s,%s,%s,%s,%s)",
                        (s.order_id,s.counterparty_id,saved.source_hash,s.revision,actor or str(config["actor_id"]))).fetchone()
            except CheckViolation:
                self.outcome_in(conn,tenant,s.order_id,"document_review")
                return
        # New accounts/contexts are enrolled automatically by discovery; employees do not connect orders manually.
        if not context:
            rule=conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s ORDER BY version DESC LIMIT 1",(tenant,)).fetchone()
            if not rule:
                raise Conflict("rules missing")
            link=conn.execute("SELECT wallet_id FROM bonuspro.customer_wallets WHERE tenant_id=%s AND counterparty_id=%s",
                              (tenant,s.counterparty_id)).fetchone()
            wallet=str(link["wallet_id"]) if link else str(uuid4())
            if not link:
                conn.execute("INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s)",(tenant,wallet))
                conn.execute("INSERT INTO bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id) VALUES (%s,%s,%s)",
                             (tenant,s.counterparty_id,wallet))
            conn.execute("INSERT INTO bonuspro.order_contexts "
                "(tenant_id,order_id,counterparty_id,wallet_id,rule_version,initial_source_hash,enrolled_by,binding_revision) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant,s.order_id,s.counterparty_id,wallet,rule["version"],saved.source_hash,config["actor_id"],s.revision))
            context=dict(wallet_id=wallet,rule_version=rule["version"])
        wallet=str(context["wallet_id"])
        complete=(proof.outcome=="eligible" or (proof.full_payment and proof.full_shipment)) and not s.cancelled
        rule=conn.execute("SELECT * FROM bonuspro.rule_versions WHERE tenant_id=%s AND version=%s",
                          (tenant,context["rule_version"])).fetchone()
        base=s.eligible_minor+bonus_minor
        points=earned_points(base,Rules(rule["earn_bps"],rule["spend_bps"])) if complete else 0
        self.ledger._wallet_lock(conn,tenant,wallet)
        if self.ledger._snapshot(conn,tenant,wallet)["balance"]+points>MAX_POINTS:
            raise Conflict("balance limit exceeded")
        tx=None
        if points:
            tx=self.ledger._post(conn,tenant,wallet,"earn/"+s.order_id,
                fingerprint(dict(order=s.order_id,base=base,points=points,rule=rule["version"])),
                points,"credit",str(config["actor_id"]),"Full payment and shipment verified by reconciliation")
        if complete:
            conn.execute("INSERT INTO bonuspro.order_accruals "
                "(tenant_id,order_id,wallet_id,credit_id,points,eligible_minor,saved_marker,financial_hash,evidence_hash,rule_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant,s.order_id,wallet,tx,points,base,saved.marker,financial_hash,proof.evidence_hash,rule["version"]))
            prior=dict(points=points)
        outcome=self.settlements.apply_in(conn,tenant,proof,context,prior,attempt,
            Rules(rule["earn_bps"],rule["spend_bps"]),actor or str(config["actor_id"]),resolution=resolution)
        self.outcome_in(conn,tenant,s.order_id,outcome)
        if time.monotonic()>proof.deadline:
            raise Conflict("evidence expired during transaction")

    def complete(self,tenant,job,proof):
        def apply(conn,row):
            self.apply_in(conn,tenant,row,proof)
            if time.monotonic()>proof.deadline:
                raise Conflict("evidence expired during application")
        self.jobs.complete(tenant,str(job["id"]),str(job["lease_id"]),apply=apply)

    def resolve_settlement(self,tenant,case_id,proof,*,decision,reason,actor):
        case=self.settlements.case(tenant,case_id)
        if case["state"]!="pending":
            if case["decision"]==decision and case["reason"]==reason and str(case["actor_id"])==actor:
                return {"state":case["state"]}
            raise Conflict("case already resolved")
        with self.ledger.transaction(tenant) as conn:
            self.apply_in(conn,tenant,{"entity_id":case["order_id"]},proof,
                          resolution=(case_id,decision,reason),actor=actor)
            if time.monotonic()>proof.deadline:raise Conflict("expired resolution proof")
        return {"state":self.settlements.case(tenant,case_id)["state"]}

    def resolve_issue(self,tenant,identity,proof,*,actor,reason):
        from bonuspro.adapters.postgres.ledger import text_value,NotFound
        text_value(reason,"reason",500)
        with self.ledger.transaction(tenant) as conn:
            issue=conn.execute("SELECT * FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND id=%s",
                              (tenant,uuid_value(identity))).fetchone()
            if not issue:raise NotFound("issue not found")
            if issue["resolved_at"]:
                if str(issue["resolved_by"])==actor and issue["resolution_reason"]==reason:return {"resolved":True}
                raise Conflict("issue already resolved")
            self.apply_in(conn,tenant,{"entity_id":issue["order_id"]},proof)
            issue=conn.execute("SELECT * FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND id=%s FOR UPDATE",
                              (tenant,uuid_value(identity))).fetchone()
            if issue["resolved_at"]:
                if str(issue["resolved_by"])==actor and issue["resolution_reason"]==reason:return {"resolved":True}
                raise Conflict("issue already resolved")
            prior=conn.execute("SELECT * FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",
                               (tenant,issue["order_id"])).fetchone()
            original_ok=bool(prior and prior["financial_hash"]==fingerprint(proof.saved.financial()) and prior["saved_marker"]==proof.saved.marker)
            if not prior and not proof.saved.marker:
                attempts=conn.execute("SELECT a.plan FROM bonuspro.redemption_attempts a JOIN bonuspro.redemption_voids v "
                    "ON v.tenant_id=a.tenant_id AND v.reservation_id=a.id WHERE a.tenant_id=%s AND a.order_id=%s",
                    (tenant,issue["order_id"])).fetchall()
                personal=conn.execute("SELECT a.plan FROM bonuspro.personal_discount_applications a "
                    "JOIN bonuspro.personal_discount_voids v ON v.tenant_id=a.tenant_id AND v.application_id=a.id "
                    "WHERE a.tenant_id=%s AND a.order_id=%s",(tenant,issue["order_id"])).fetchall()
                original_ok=any(row["plan"]["before"]==proof.saved.financial() for row in attempts+personal)
            if not original_ok or proof.outcome=="document_review":
                raise Conflict("document is still inconsistent")
            if time.monotonic()>proof.deadline:raise Conflict("expired correction proof")
            conn.execute("UPDATE bonuspro.reconciliation_issues SET resolved_at=clock_timestamp(),resolved_by=%s,resolution_reason=%s "
                         "WHERE tenant_id=%s AND id=%s AND resolved_at IS NULL",(actor,reason,tenant,identity))
        return {"resolved":True}

    def invalid(self,tenant,order,reason,*,missing_order=False,job=None):
        tenant,order=map(uuid_value,(tenant,order))
        with self.ledger.transaction(tenant) as conn:
            # Match the normal completion lock order and fence the observation
            # before it can alter a monitor on behalf of an expired job owner.
            if job is not None:
                job_id,lease_id=map(uuid_value,(job["id"],job["lease_id"]))
                current=conn.execute("SELECT id FROM bonuspro.jobs WHERE tenant_id=%s AND id=%s "
                    "AND state='running' AND lease_id=%s AND lease_until>clock_timestamp() FOR UPDATE",
                    (tenant,job_id,lease_id)).fetchone()
                if not current:raise Conflict("job lease expired or replaced")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
            self.ledger._key_lock(conn,tenant,"settings")
            self.ledger._key_lock(conn,tenant,"order/"+order)
            if missing_order:
                # Same conservative predicate as the migration's private owner
                # function; all history states count, including released attempts
                # and resolved reviews. No financial history is deleted or released.
                history=conn.execute("""
                    SELECT EXISTS(SELECT 1 FROM bonuspro.reservations WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.redemption_attempts WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.personal_discount_applications WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=%s AND order_id=%s)
                    OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND order_id=%s)
                    AS present
                """,(tenant,order)*7).fetchone()["present"]
                if not history:
                    conn.execute("UPDATE bonuspro.order_monitors SET outcome='cancelled',checked_at=now(),"
                        "next_due=now()+interval '5 minutes' WHERE tenant_id=%s AND order_id=%s",(tenant,order))
                    if job is not None:self.jobs.complete_in(conn,tenant,job_id,lease_id)
                    return True
            prior=conn.execute("SELECT wallet_id FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s",
                               (tenant,order)).fetchone()
            if prior:
                self.ledger._wallet_lock(conn,tenant,str(prior["wallet_id"]))
                self.issue_in(conn,tenant,order,str(prior["wallet_id"]),reason,"unavailable")
            self.outcome_in(conn,tenant,order,"provider_error")
            return False

    def cooldown(self,tenant,seconds,error):
        with self.ledger.transaction(tenant) as conn:
            conn.execute("UPDATE bonuspro.sync_state SET cooldown_until=GREATEST(cooldown_until,now()+%s*interval '1 second'),"
                         "last_error=%s,heartbeat_at=now(),updated_at=now() WHERE tenant_id=%s",(seconds,error,tenant))

    def dashboard(self,tenant):
        with self.ledger.transaction(tenant) as conn:
            counts=conn.execute("SELECT outcome,count(*) AS n FROM bonuspro.order_monitors WHERE tenant_id=%s GROUP BY outcome",(tenant,)).fetchall()
            issues=conn.execute("SELECT id,order_id,reason,created_at FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND resolved_at IS NULL "
                                "ORDER BY created_at DESC LIMIT 50",(tenant,)).fetchall()
            jobs=conn.execute("SELECT state,count(*) AS n FROM bonuspro.jobs WHERE tenant_id=%s GROUP BY state",(tenant,)).fetchall()
            stale=conn.execute("SELECT count(*) AS n FROM bonuspro.reservations WHERE tenant_id=%s AND state='verifying' "
                               "AND created_at<clock_timestamp()-interval '15 minutes'",(tenant,)).fetchone()["n"]
            pending=conn.execute("SELECT count(*) AS n FROM bonuspro.settlement_cases WHERE tenant_id=%s AND state='pending'",(tenant,)).fetchone()["n"]
            queue=conn.execute("SELECT min(created_at) AS oldest FROM bonuspro.jobs WHERE tenant_id=%s AND state IN ('queued','running')",(tenant,)).fetchone()["oldest"]
            unknown=conn.execute("SELECT id,order_id,points,created_at FROM ("
                "SELECT id,order_id,points,created_at FROM bonuspro.reservations WHERE tenant_id=%s AND state='verifying' "
                "UNION ALL SELECT id,order_id,points,created_at FROM bonuspro.personal_discount_applications "
                "WHERE tenant_id=%s AND state='verifying' AND points=0) operations ORDER BY created_at LIMIT 50",
                (tenant,tenant)).fetchall()
        return dict(configuration=self.state(tenant),orders={r["outcome"]:r["n"] for r in counts},
                    jobs={r["state"]:r["n"] for r in jobs},issues=issues,
                    attention=dict(stale_reservations=stale,pending_returns=pending,oldest_job=queue),unverified=unknown)
