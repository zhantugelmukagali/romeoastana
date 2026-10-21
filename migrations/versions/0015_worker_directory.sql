-- Deliberately global NON-CUSTOMER metadata: active account UUIDs only.
-- Application workers may enumerate this directory, never cross-tenant financial rows.
CREATE TABLE bonuspro.worker_accounts (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.tenants(id)
);
REVOKE ALL ON bonuspro.worker_accounts FROM PUBLIC;
GRANT SELECT ON bonuspro.worker_accounts TO bonuspro_app;
GRANT SELECT,INSERT,DELETE ON bonuspro.worker_accounts TO bonuspro_lifecycle;

CREATE FUNCTION bonuspro.route_worker_account() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.state='Activated' THEN
  INSERT INTO bonuspro.worker_accounts(tenant_id) VALUES(NEW.tenant_id) ON CONFLICT DO NOTHING;
 ELSE
  DELETE FROM bonuspro.worker_accounts WHERE tenant_id=NEW.tenant_id;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.route_worker_account() FROM PUBLIC;
CREATE TRIGGER worker_account_lifecycle AFTER INSERT OR UPDATE OF state ON bonuspro.installations
FOR EACH ROW EXECUTE FUNCTION bonuspro.route_worker_account();

-- Migration owner alone seeds previously installed accounts. The ALTER holds an
-- exclusive lock until commit; no application session observes unforced RLS.
ALTER TABLE bonuspro.installations NO FORCE ROW LEVEL SECURITY;
INSERT INTO bonuspro.worker_accounts(tenant_id)
SELECT tenant_id FROM bonuspro.installations WHERE state='Activated';
ALTER TABLE bonuspro.installations FORCE ROW LEVEL SECURITY;
