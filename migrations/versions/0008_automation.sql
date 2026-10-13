CREATE TABLE bonuspro.automation_config (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.tenants(id),
 starts_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 actor_id uuid NOT NULL
);
CREATE TABLE bonuspro.sync_state (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.automation_config(tenant_id),
 scan_until timestamptz NOT NULL,
 heartbeat_at timestamptz,
 cooldown_until timestamptz NOT NULL DEFAULT now(),
 last_error text CHECK (last_error IN ('provider_timeout','provider_unavailable','conflict','invalid_document')),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bonuspro.order_monitors (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 order_id uuid NOT NULL,
 generation bigint NOT NULL DEFAULT 0 CHECK (generation>=0),
 next_due timestamptz NOT NULL DEFAULT now(),
 checked_at timestamptz,
 outcome text NOT NULL DEFAULT 'queued' CHECK (outcome IN
 ('queued','historical','waiting_payment','waiting_shipment','eligible','credited','cancelled','return_review','document_review','redemption_pending','provider_error')),
 PRIMARY KEY (tenant_id,order_id)
);
CREATE INDEX monitor_due ON bonuspro.order_monitors(tenant_id,next_due,order_id);
CREATE TABLE bonuspro.order_accruals (
 tenant_id uuid NOT NULL,
 order_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 credit_id uuid,
 points bigint NOT NULL CHECK (points BETWEEN 0 AND 90000000000000),
 eligible_minor bigint NOT NULL CHECK (eligible_minor BETWEEN 0 AND 9000000000000000),
 financial_hash text NOT NULL CHECK (financial_hash ~ '^[0-9a-f]{64}$'),
 evidence_hash text NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
 rule_version integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,order_id),
 FOREIGN KEY (tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY (tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id),
 FOREIGN KEY (tenant_id,credit_id,wallet_id) REFERENCES bonuspro.ledger_transactions(tenant_id,id,wallet_id),
 CHECK ((points=0)=(credit_id IS NULL))
);
CREATE TABLE bonuspro.reconciliation_issues (
 tenant_id uuid NOT NULL,
 order_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 reason text NOT NULL CHECK (reason IN ('changed_credited_order','provider_document_missing','invalid_credited_document')),
 evidence_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,order_id,reason),
 FOREIGN KEY (tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id)
);
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['automation_config','sync_state','order_monitors','order_accruals','reconciliation_issues']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
 FOREACH tbl IN ARRAY ARRAY['automation_config','order_accruals','reconciliation_issues']
 LOOP
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
 END LOOP;
END $$;
GRANT UPDATE(scan_until,heartbeat_at,cooldown_until,last_error,updated_at) ON bonuspro.sync_state TO bonuspro_app;
GRANT UPDATE(generation,next_due,checked_at,outcome) ON bonuspro.order_monitors TO bonuspro_app;
