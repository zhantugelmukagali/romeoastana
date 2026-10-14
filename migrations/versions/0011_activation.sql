CREATE TABLE bonuspro.activation_requests (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 id uuid NOT NULL,
 generation uuid NOT NULL,
 actor_id uuid NOT NULL,
 rule_version integer NOT NULL,
 marker_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,rule_version) REFERENCES bonuspro.rule_versions(tenant_id,version)
);
CREATE TABLE bonuspro.activation_completions (
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,request_id),
 FOREIGN KEY(tenant_id,request_id) REFERENCES bonuspro.activation_requests(tenant_id,id)
);
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['activation_requests','activation_completions']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
  EXECUTE format('GRANT SELECT ON bonuspro.%I TO bonuspro_app,bonuspro_lifecycle',tbl);
 END LOOP;
END $$;
GRANT INSERT ON bonuspro.activation_requests TO bonuspro_app;
GRANT INSERT ON bonuspro.activation_completions TO bonuspro_lifecycle;
GRANT SELECT ON bonuspro.rule_versions,bonuspro.widget_bindings,bonuspro.automation_config,bonuspro.sync_state TO bonuspro_lifecycle;
GRANT INSERT ON bonuspro.automation_config,bonuspro.sync_state TO bonuspro_lifecycle;
