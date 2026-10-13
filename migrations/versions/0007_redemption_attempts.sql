CREATE TABLE bonuspro.widget_bindings (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.tenants(id),
 marker_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bonuspro.redemption_attempts (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 order_id uuid NOT NULL,
 request_id uuid NOT NULL,
 request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 source_hash text NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
 marker_id uuid NOT NULL,
 marker text NOT NULL,
 plan jsonb NOT NULL CHECK (jsonb_typeof(plan)='object'),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,id),
 UNIQUE (tenant_id,request_id),
 FOREIGN KEY (tenant_id,id) REFERENCES bonuspro.reservations(tenant_id,id),
 FOREIGN KEY (tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id)
);
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['widget_bindings','redemption_attempts']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
END $$;
