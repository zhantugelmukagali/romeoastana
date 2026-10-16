CREATE TABLE bonuspro.import_batches (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 id uuid NOT NULL,
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
 generation uuid NOT NULL,
 actor_id uuid NOT NULL,
 rows jsonb NOT NULL CHECK(jsonb_typeof(rows)='array' AND jsonb_array_length(rows) BETWEEN 1 AND 100),
 total bigint NOT NULL CHECK(total BETWEEN 0 AND 90000000000000),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '24 hours',
 PRIMARY KEY(tenant_id,id)
);
CREATE INDEX import_manifest ON bonuspro.import_batches(tenant_id,manifest_hash,generation);
CREATE TABLE bonuspro.import_commits (
 tenant_id uuid NOT NULL,
 batch_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,batch_id),
 FOREIGN KEY(tenant_id,batch_id) REFERENCES bonuspro.import_batches(tenant_id,id)
);
CREATE TABLE bonuspro.opening_balances (
 tenant_id uuid NOT NULL,
 counterparty_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 batch_id uuid NOT NULL,
 points bigint NOT NULL CHECK(points BETWEEN 0 AND 90000000000000),
 transaction_id uuid,
 PRIMARY KEY(tenant_id,counterparty_id),
 UNIQUE(tenant_id,transaction_id),
 FOREIGN KEY(tenant_id,counterparty_id) REFERENCES bonuspro.customer_wallets(tenant_id,counterparty_id),
 FOREIGN KEY(tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id),
 FOREIGN KEY(tenant_id,batch_id) REFERENCES bonuspro.import_commits(tenant_id,batch_id),
 FOREIGN KEY(tenant_id,transaction_id,wallet_id) REFERENCES bonuspro.ledger_transactions(tenant_id,id,wallet_id),
 CHECK((points=0)=(transaction_id IS NULL))
);
CREATE FUNCTION bonuspro.validate_opening() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM bonuspro.customer_wallets WHERE tenant_id=NEW.tenant_id AND counterparty_id=NEW.counterparty_id AND wallet_id=NEW.wallet_id) THEN
  RAISE EXCEPTION 'opening wallet mismatch' USING ERRCODE='23514';
 END IF;
 IF NEW.points>0 AND NOT EXISTS(SELECT 1 FROM bonuspro.ledger_transactions WHERE tenant_id=NEW.tenant_id AND id=NEW.transaction_id
   AND wallet_id=NEW.wallet_id AND kind='credit' AND delta=NEW.points) THEN
  RAISE EXCEPTION 'opening journal mismatch' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER opening_complete AFTER INSERT ON bonuspro.opening_balances
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_opening();
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['import_batches','import_commits','opening_balances']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
 END LOOP;
END $$;
