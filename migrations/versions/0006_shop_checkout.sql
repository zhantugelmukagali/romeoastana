CREATE TABLE bonuspro.rule_versions (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 version integer NOT NULL CHECK (version>0),
 earn_bps integer NOT NULL CHECK (earn_bps BETWEEN 0 AND 10000),
 spend_bps integer NOT NULL CHECK (spend_bps BETWEEN 0 AND 10000),
 delivery_policy text NOT NULL CHECK (delivery_policy='exclude'),
 request_id uuid NOT NULL,
 request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,version),
 UNIQUE (tenant_id,request_id)
);
CREATE TABLE bonuspro.customer_wallets (
 tenant_id uuid NOT NULL,
 counterparty_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,counterparty_id),
 UNIQUE (tenant_id,wallet_id),
 UNIQUE (tenant_id,counterparty_id,wallet_id),
 FOREIGN KEY (tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id)
);
CREATE TABLE bonuspro.order_contexts (
 tenant_id uuid NOT NULL,
 order_id uuid NOT NULL,
 counterparty_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 rule_version integer NOT NULL,
 initial_source_hash text NOT NULL CHECK (initial_source_hash ~ '^[0-9a-f]{64}$'),
 enrolled_by uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,order_id),
 FOREIGN KEY (tenant_id,counterparty_id,wallet_id)
   REFERENCES bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id),
 FOREIGN KEY (tenant_id,rule_version) REFERENCES bonuspro.rule_versions(tenant_id,version)
);
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['rule_versions','customer_wallets','order_contexts']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
END $$;
