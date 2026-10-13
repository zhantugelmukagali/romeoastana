CREATE SCHEMA bonuspro;
REVOKE ALL ON SCHEMA bonuspro FROM PUBLIC;

-- A group role: deployment provisions a separate LOGIN role and grants membership.
GRANT USAGE ON SCHEMA bonuspro TO bonuspro_app;

CREATE FUNCTION bonuspro.current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('bonuspro.tenant_id', true), '')::uuid
$$;
REVOKE ALL ON FUNCTION bonuspro.current_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bonuspro.current_tenant() TO bonuspro_app;

CREATE TABLE bonuspro.tenants (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bonuspro.wallets (
  tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
  id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE TABLE bonuspro.reservations (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  wallet_id uuid NOT NULL,
  operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
  request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  order_id uuid NOT NULL,
  quote_hash text NOT NULL CHECK (quote_hash ~ '^[0-9a-f]{64}$'),
  points bigint NOT NULL CHECK (points > 0 AND points <= 90000000000000),
  state text NOT NULL DEFAULT 'held' CHECK (state IN ('held','verifying','committed','released')),
  actor_id uuid NOT NULL,
  proof_revision text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, operation_key),
  UNIQUE (tenant_id, id, wallet_id),
  FOREIGN KEY (tenant_id, wallet_id) REFERENCES bonuspro.wallets(tenant_id, id),
  CHECK ((state = 'committed') = (proof_revision IS NOT NULL)),
  CHECK (proof_revision IS NULL OR length(proof_revision) BETWEEN 1 AND 256)
);
CREATE UNIQUE INDEX one_active_reservation_per_order
ON bonuspro.reservations(tenant_id, order_id)
WHERE state IN ('held','verifying','committed');

CREATE TABLE bonuspro.ledger_transactions (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  wallet_id uuid NOT NULL,
  operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 256),
  request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  kind text NOT NULL CHECK (kind IN ('credit','redemption')),
  delta bigint NOT NULL CHECK (delta <> 0 AND abs(delta) <= 90000000000000),
  reservation_id uuid,
  actor_id uuid NOT NULL,
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, operation_key),
  UNIQUE (tenant_id, id, wallet_id),
  UNIQUE (tenant_id, reservation_id),
  FOREIGN KEY (tenant_id, wallet_id) REFERENCES bonuspro.wallets(tenant_id, id),
  FOREIGN KEY (tenant_id, reservation_id, wallet_id)
    REFERENCES bonuspro.reservations(tenant_id, id, wallet_id),
  CHECK ((kind = 'credit' AND delta > 0 AND reservation_id IS NULL)
      OR (kind = 'redemption' AND delta < 0 AND reservation_id IS NOT NULL))
);
CREATE TABLE bonuspro.ledger_postings (
  tenant_id uuid NOT NULL,
  transaction_id uuid NOT NULL,
  wallet_id uuid NOT NULL,
  account text NOT NULL CHECK (account IN ('customer','system')),
  amount bigint NOT NULL CHECK (amount <> 0 AND abs(amount) <= 90000000000000),
  PRIMARY KEY (tenant_id, transaction_id, account),
  FOREIGN KEY (tenant_id, transaction_id, wallet_id)
    REFERENCES bonuspro.ledger_transactions(tenant_id, id, wallet_id)
);
CREATE INDEX ledger_wallet ON bonuspro.ledger_postings(tenant_id, wallet_id, account);
CREATE INDEX reserved_wallet ON bonuspro.reservations(tenant_id, wallet_id) WHERE state IN ('held','verifying');

CREATE FUNCTION bonuspro.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable record' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER immutable_transactions BEFORE UPDATE OR DELETE ON bonuspro.ledger_transactions
FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
CREATE TRIGGER immutable_postings BEFORE UPDATE OR DELETE ON bonuspro.ledger_postings
FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();

CREATE FUNCTION bonuspro.validate_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE tx bonuspro.ledger_transactions;
DECLARE rid uuid;
DECLARE n integer;
DECLARE customer_amount numeric;
DECLARE system_amount numeric;
BEGIN
  IF TG_TABLE_NAME = 'ledger_transactions' THEN rid := NEW.id;
  ELSE rid := NEW.transaction_id; END IF;
  SELECT * INTO STRICT tx FROM bonuspro.ledger_transactions
    WHERE tenant_id=NEW.tenant_id AND id=rid;
  SELECT count(*), sum(amount) FILTER (WHERE account='customer'),
         sum(amount) FILTER (WHERE account='system')
    INTO n, customer_amount, system_amount
    FROM bonuspro.ledger_postings WHERE tenant_id=tx.tenant_id AND transaction_id=tx.id;
  IF n <> 2 OR customer_amount IS DISTINCT FROM tx.delta::numeric
       OR system_amount IS DISTINCT FROM (-tx.delta)::numeric THEN
    RAISE EXCEPTION 'unbalanced transaction' USING ERRCODE = '23514';
  END IF;
  IF tx.kind='redemption' AND NOT EXISTS (
    SELECT 1 FROM bonuspro.reservations r
    WHERE r.tenant_id=tx.tenant_id AND r.id=tx.reservation_id AND r.wallet_id=tx.wallet_id
      AND r.state='committed' AND r.points=-tx.delta
  ) THEN RAISE EXCEPTION 'unconfirmed redemption' USING ERRCODE = '23514'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER journal_complete AFTER INSERT ON bonuspro.ledger_transactions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_journal();
CREATE CONSTRAINT TRIGGER postings_balanced AFTER INSERT ON bonuspro.ledger_postings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_journal();

CREATE FUNCTION bonuspro.guard_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.tenant_id,NEW.id,NEW.wallet_id,NEW.operation_key,NEW.request_hash,
         NEW.order_id,NEW.quote_hash,NEW.points,NEW.actor_id,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.id,OLD.wallet_id,OLD.operation_key,OLD.request_hash,
         OLD.order_id,OLD.quote_hash,OLD.points,OLD.actor_id,OLD.created_at) THEN
    RAISE EXCEPTION 'reservation identity is immutable' USING ERRCODE = '23514';
  END IF;
  IF NOT ((OLD.state='held' AND NEW.state IN ('verifying','released'))
       OR (OLD.state='verifying' AND NEW.state='committed')) THEN
    RAISE EXCEPTION 'invalid reservation transition' USING ERRCODE = '23514';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER reservation_state BEFORE UPDATE ON bonuspro.reservations
FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_reservation();
CREATE TRIGGER reservation_no_delete BEFORE DELETE ON bonuspro.reservations
FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();

CREATE FUNCTION bonuspro.validate_committed_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Query final state: a deferred trigger can refer to an earlier row version.
  IF EXISTS (SELECT 1 FROM bonuspro.reservations r
    WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.id AND r.state='committed'
      AND NOT EXISTS (SELECT 1 FROM bonuspro.ledger_transactions t
        WHERE t.tenant_id=r.tenant_id AND t.reservation_id=r.id
          AND t.wallet_id=r.wallet_id AND t.delta=-r.points AND t.kind='redemption'))
  THEN RAISE EXCEPTION 'committed reservation lacks ledger' USING ERRCODE = '23514'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER committed_has_ledger AFTER INSERT OR UPDATE ON bonuspro.reservations
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_committed_reservation();

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['wallets','reservations','ledger_transactions','ledger_postings']
  LOOP
    EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY', tbl);
    EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant())', tbl);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON bonuspro.wallets, bonuspro.reservations,
  bonuspro.ledger_transactions, bonuspro.ledger_postings TO bonuspro_app;
GRANT UPDATE (state, proof_revision) ON bonuspro.reservations TO bonuspro_app;
-- No UPDATE/DELETE/TRUNCATE on ledger or tenant provisioning rights for the application.
