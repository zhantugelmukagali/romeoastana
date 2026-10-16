-- Preserve immutable financial history: reversals are new balanced transactions.
ALTER TABLE bonuspro.ledger_transactions ADD COLUMN credit_ref_id uuid;
ALTER TABLE bonuspro.ledger_transactions ADD FOREIGN KEY (tenant_id,credit_ref_id,wallet_id)
  REFERENCES bonuspro.ledger_transactions(tenant_id,id,wallet_id);
DO $$
DECLARE item record;
BEGIN
  FOR item IN SELECT conname FROM pg_constraint
    WHERE conrelid='bonuspro.ledger_transactions'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%kind%'
  LOOP
    EXECUTE format('ALTER TABLE bonuspro.ledger_transactions DROP CONSTRAINT %I', item.conname);
  END LOOP;
END $$;
ALTER TABLE bonuspro.ledger_transactions ADD CONSTRAINT transaction_kind CHECK (
  (kind='credit' AND delta>0 AND reservation_id IS NULL AND credit_ref_id IS NULL) OR
  (kind='redemption' AND delta<0 AND reservation_id IS NOT NULL AND credit_ref_id IS NULL) OR
  (kind='reversal' AND delta<0 AND reservation_id IS NULL AND credit_ref_id IS NOT NULL)
);

CREATE TABLE bonuspro.return_cases (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  wallet_id uuid NOT NULL,
  credit_id uuid NOT NULL,
  return_id uuid NOT NULL,
  request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  points bigint NOT NULL CHECK (points>0 AND points<=90000000000000),
  state text NOT NULL CHECK (state IN ('pending','reversed','waived')),
  transaction_id uuid,
  requested_by uuid NOT NULL,
  resolved_by uuid,
  resolution_reason text CHECK (length(resolution_reason) BETWEEN 1 AND 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,credit_id,return_id),
  UNIQUE (tenant_id,transaction_id),
  FOREIGN KEY (tenant_id,credit_id,wallet_id)
    REFERENCES bonuspro.ledger_transactions(tenant_id,id,wallet_id),
  FOREIGN KEY (tenant_id,transaction_id,wallet_id)
    REFERENCES bonuspro.ledger_transactions(tenant_id,id,wallet_id),
  CHECK ((state='reversed') = (transaction_id IS NOT NULL)),
  CHECK ((state='pending' AND resolved_by IS NULL AND resolution_reason IS NULL AND resolved_at IS NULL)
    OR (state<>'pending' AND resolved_by IS NOT NULL AND resolution_reason IS NOT NULL AND resolved_at IS NOT NULL))
);
CREATE INDEX pending_returns ON bonuspro.return_cases(tenant_id,wallet_id) WHERE state='pending';
ALTER TABLE bonuspro.return_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.return_cases FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bonuspro.return_cases
 USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant());
GRANT SELECT,INSERT ON bonuspro.return_cases TO bonuspro_app;
GRANT UPDATE(state,transaction_id,resolved_by,resolution_reason,resolved_at)
 ON bonuspro.return_cases TO bonuspro_app;

CREATE FUNCTION bonuspro.guard_return_case() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.wallet_id,NEW.credit_id,NEW.return_id,NEW.request_hash,
        NEW.points,NEW.requested_by,NEW.created_at) IS DISTINCT FROM
    ROW(OLD.tenant_id,OLD.id,OLD.wallet_id,OLD.credit_id,OLD.return_id,OLD.request_hash,
        OLD.points,OLD.requested_by,OLD.created_at)
 OR OLD.state<>'pending' OR NEW.state NOT IN ('reversed','waived') THEN
   RAISE EXCEPTION 'invalid return transition' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER return_transition BEFORE UPDATE ON bonuspro.return_cases
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_return_case();
CREATE TRIGGER return_no_delete BEFORE DELETE ON bonuspro.return_cases
 FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();

CREATE FUNCTION bonuspro.validate_return_case() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c bonuspro.return_cases;
DECLARE credited bigint;
DECLARE allocated numeric;
BEGIN
 SELECT * INTO STRICT c FROM bonuspro.return_cases WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 PERFORM 1 FROM bonuspro.wallets WHERE tenant_id=c.tenant_id AND id=c.wallet_id FOR UPDATE;
 SELECT delta INTO credited FROM bonuspro.ledger_transactions
   WHERE tenant_id=c.tenant_id AND id=c.credit_id AND kind='credit';
 SELECT sum(points) INTO allocated FROM bonuspro.return_cases
   WHERE tenant_id=c.tenant_id AND credit_id=c.credit_id;
 IF credited IS NULL OR allocated>credited THEN
   RAISE EXCEPTION 'return exceeds original credit' USING ERRCODE='23514';
 END IF;
 IF c.state='reversed' AND NOT EXISTS (
   SELECT 1 FROM bonuspro.ledger_transactions t
   WHERE t.tenant_id=c.tenant_id AND t.id=c.transaction_id AND t.kind='reversal'
     AND t.credit_ref_id=c.credit_id AND t.wallet_id=c.wallet_id AND t.delta=-c.points
 ) THEN RAISE EXCEPTION 'return lacks matching reversal' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER return_consistent AFTER INSERT OR UPDATE ON bonuspro.return_cases
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_return_case();

CREATE FUNCTION bonuspro.validate_wallet_and_reversal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE balance numeric;
BEGIN
 PERFORM 1 FROM bonuspro.wallets WHERE tenant_id=NEW.tenant_id AND id=NEW.wallet_id FOR UPDATE;
 SELECT COALESCE(sum(amount),0) INTO balance FROM bonuspro.ledger_postings
 WHERE tenant_id=NEW.tenant_id AND wallet_id=NEW.wallet_id AND account='customer';
 IF balance<0 OR balance>90000000000000 THEN
   RAISE EXCEPTION 'wallet balance outside allowed range' USING ERRCODE='23514';
 END IF;
 IF NEW.kind='reversal' AND NOT EXISTS (
   SELECT 1 FROM bonuspro.return_cases c WHERE c.tenant_id=NEW.tenant_id
     AND c.transaction_id=NEW.id AND c.credit_id=NEW.credit_ref_id
     AND c.state='reversed' AND c.wallet_id=NEW.wallet_id AND c.points=-NEW.delta
 ) THEN RAISE EXCEPTION 'reversal lacks return case' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER wallet_and_reversal_consistent AFTER INSERT ON bonuspro.ledger_transactions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_wallet_and_reversal();
