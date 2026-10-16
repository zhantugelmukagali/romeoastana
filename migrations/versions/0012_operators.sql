CREATE TABLE bonuspro.known_operators (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 user_id uuid NOT NULL,
 login text NOT NULL CHECK(length(login)<=256),
 last_seen timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,user_id)
);
CREATE TABLE bonuspro.operator_permissions (
 tenant_id uuid NOT NULL,
 user_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0),
 enabled boolean NOT NULL,
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 request_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,user_id,version),
 UNIQUE(tenant_id,request_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES bonuspro.known_operators(tenant_id,user_id)
);
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['known_operators','operator_permissions']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
END $$;
GRANT UPDATE(login,last_seen) ON bonuspro.known_operators TO bonuspro_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.operator_permissions
 FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
