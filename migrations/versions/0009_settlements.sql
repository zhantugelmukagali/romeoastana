CREATE TABLE bonuspro.settlement_events (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 order_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 target_earn bigint NOT NULL CHECK(target_earn BETWEEN 0 AND 90000000000000),
 target_refund bigint NOT NULL CHECK(target_refund BETWEEN 0 AND 90000000000000),
 earn_delta bigint NOT NULL CHECK(abs(earn_delta)<=90000000000000),
 refund_delta bigint NOT NULL CHECK(abs(refund_delta)<=90000000000000),
 waived_earn bigint NOT NULL DEFAULT 0 CHECK(waived_earn BETWEEN 0 AND 90000000000000),
 waived_refund bigint NOT NULL DEFAULT 0 CHECK(waived_refund BETWEEN 0 AND 90000000000000),
 evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 decision text NOT NULL CHECK(decision IN ('automatic','apply','waive')),
 reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,id,wallet_id),
 FOREIGN KEY(tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY(tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id)
);
CREATE INDEX settlement_order ON bonuspro.settlement_events(tenant_id,order_id);
CREATE TABLE bonuspro.settlement_cases (
 order_name text NOT NULL DEFAULT '' CHECK(length(order_name)<=256),
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 order_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
 target_earn bigint NOT NULL CHECK(target_earn BETWEEN 0 AND 90000000000000),
 target_refund bigint NOT NULL CHECK(target_refund BETWEEN 0 AND 90000000000000),
 earn_delta bigint NOT NULL CHECK(abs(earn_delta)<=90000000000000),
 refund_delta bigint NOT NULL CHECK(abs(refund_delta)<=90000000000000),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','waived','superseded')),
 event_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 resolved_at timestamptz,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY(tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id),
 FOREIGN KEY(tenant_id,event_id,wallet_id) REFERENCES bonuspro.settlement_events(tenant_id,id,wallet_id),
 CHECK((state IN ('applied','waived'))=(event_id IS NOT NULL)),
 CHECK((state='pending')=(resolved_at IS NULL))
);
CREATE UNIQUE INDEX one_pending_settlement ON bonuspro.settlement_cases(tenant_id,order_id) WHERE state='pending';
CREATE INDEX pending_settlement_wallet ON bonuspro.settlement_cases(tenant_id,wallet_id) WHERE state='pending';
CREATE FUNCTION bonuspro.guard_settlement_case() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.order_id,NEW.wallet_id,NEW.evidence_hash,NEW.target_earn,NEW.target_refund,NEW.earn_delta,NEW.refund_delta,NEW.created_at,NEW.order_name)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.order_id,OLD.wallet_id,OLD.evidence_hash,OLD.target_earn,OLD.target_refund,OLD.earn_delta,OLD.refund_delta,OLD.created_at,OLD.order_name)
 OR OLD.state<>'pending' OR NEW.state='pending' THEN
  RAISE EXCEPTION 'invalid settlement case transition' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER settlement_case_transition BEFORE UPDATE ON bonuspro.settlement_cases
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_settlement_case();

ALTER TABLE bonuspro.ledger_transactions ADD COLUMN settlement_event_id uuid;
ALTER TABLE bonuspro.ledger_transactions ADD FOREIGN KEY(tenant_id,settlement_event_id,wallet_id)
 REFERENCES bonuspro.settlement_events(tenant_id,id,wallet_id);
CREATE UNIQUE INDEX settlement_transaction ON bonuspro.ledger_transactions(tenant_id,settlement_event_id) WHERE settlement_event_id IS NOT NULL;
ALTER TABLE bonuspro.ledger_transactions DROP CONSTRAINT transaction_kind;
ALTER TABLE bonuspro.ledger_transactions ADD CONSTRAINT transaction_kind CHECK (
 (kind='credit' AND delta>0 AND reservation_id IS NULL AND credit_ref_id IS NULL AND settlement_event_id IS NULL) OR
 (kind='redemption' AND delta<0 AND reservation_id IS NOT NULL AND credit_ref_id IS NULL AND settlement_event_id IS NULL) OR
 (kind='reversal' AND delta<0 AND reservation_id IS NULL AND credit_ref_id IS NOT NULL AND settlement_event_id IS NULL) OR
 (kind='settlement' AND reservation_id IS NULL AND credit_ref_id IS NULL AND settlement_event_id IS NOT NULL)
);
CREATE FUNCTION bonuspro.validate_settlement() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE e bonuspro.settlement_events;
DECLARE eid uuid;
DECLARE expected bigint;
BEGIN
 IF TG_TABLE_NAME='settlement_events' THEN eid:=NEW.id; ELSE eid:=NEW.settlement_event_id; END IF;
 IF eid IS NULL THEN RETURN NULL; END IF;
 SELECT * INTO STRICT e FROM bonuspro.settlement_events WHERE tenant_id=NEW.tenant_id AND id=eid;
 expected:=e.earn_delta+e.refund_delta;
 IF expected=0 THEN
  IF EXISTS(SELECT 1 FROM bonuspro.ledger_transactions WHERE tenant_id=e.tenant_id AND settlement_event_id=e.id) THEN
   RAISE EXCEPTION 'zero settlement must not post' USING ERRCODE='23514';
  END IF;
 ELSIF NOT EXISTS(SELECT 1 FROM bonuspro.ledger_transactions WHERE tenant_id=e.tenant_id AND
     settlement_event_id=e.id AND kind='settlement' AND wallet_id=e.wallet_id AND delta=expected) THEN
  RAISE EXCEPTION 'settlement lacks matching journal' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER settlement_complete AFTER INSERT ON bonuspro.settlement_events
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_settlement();
CREATE CONSTRAINT TRIGGER settlement_posting_complete AFTER INSERT ON bonuspro.ledger_transactions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_settlement();
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['settlement_events','settlement_cases']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
  EXECUTE format('CREATE TRIGGER no_delete BEFORE DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
 END LOOP;
END $$;
CREATE TRIGGER immutable_settlement BEFORE UPDATE ON bonuspro.settlement_events
 FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
GRANT UPDATE(state,event_id,resolved_at) ON bonuspro.settlement_cases TO bonuspro_app;

-- Reconciliation issues are separate episodes; corrected documents can be checked again.
ALTER TABLE bonuspro.reconciliation_issues DROP CONSTRAINT reconciliation_issues_pkey;
ALTER TABLE bonuspro.reconciliation_issues ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE bonuspro.reconciliation_issues ADD PRIMARY KEY(tenant_id,id);
ALTER TABLE bonuspro.reconciliation_issues ADD COLUMN resolved_at timestamptz;
ALTER TABLE bonuspro.reconciliation_issues ADD COLUMN resolved_by uuid;
ALTER TABLE bonuspro.reconciliation_issues ADD COLUMN resolution_reason text;
ALTER TABLE bonuspro.reconciliation_issues ADD CONSTRAINT complete_issue_resolution CHECK(
 (resolved_at IS NULL AND resolved_by IS NULL AND resolution_reason IS NULL) OR
 (resolved_at IS NOT NULL AND resolved_by IS NOT NULL AND length(resolution_reason) BETWEEN 1 AND 500));
CREATE UNIQUE INDEX one_open_issue ON bonuspro.reconciliation_issues(tenant_id,order_id,reason) WHERE resolved_at IS NULL;
DROP TRIGGER immutable_record ON bonuspro.reconciliation_issues;
CREATE FUNCTION bonuspro.guard_issue() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.order_id,NEW.wallet_id,NEW.reason,NEW.evidence_hash,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.order_id,OLD.wallet_id,OLD.reason,OLD.evidence_hash,OLD.created_at)
 OR OLD.resolved_at IS NOT NULL OR NEW.resolved_at IS NULL THEN
 RAISE EXCEPTION 'invalid issue resolution' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER issue_resolution BEFORE UPDATE ON bonuspro.reconciliation_issues
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_issue();
CREATE TRIGGER issue_no_delete BEFORE DELETE ON bonuspro.reconciliation_issues
 FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
GRANT UPDATE(resolved_at,resolved_by,resolution_reason) ON bonuspro.reconciliation_issues TO bonuspro_app;
