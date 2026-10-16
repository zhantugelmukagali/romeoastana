CREATE TABLE bonuspro.installations (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.tenants(id),
 app_id uuid NOT NULL,
 state text NOT NULL CHECK (state IN ('SettingsRequired','Activated','Suspended','Uninstalled')),
 token_ciphertext text,
 generation uuid NOT NULL,
 last_event_iat bigint NOT NULL,
 last_event_digest text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((state IN ('SettingsRequired','Activated')) = (token_ciphertext IS NOT NULL))
);
ALTER TABLE bonuspro.installations ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.installations FORCE ROW LEVEL SECURITY;
CREATE POLICY installation_app ON bonuspro.installations TO bonuspro_app
USING (tenant_id=bonuspro.current_tenant());
CREATE POLICY installation_lifecycle ON bonuspro.installations TO bonuspro_lifecycle
USING (true) WITH CHECK (true);

CREATE TABLE bonuspro.vendor_replays (
 jti text PRIMARY KEY CHECK (length(jti) BETWEEN 1 AND 256),
 expires_at timestamptz NOT NULL
);
CREATE TABLE bonuspro.sessions (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 token_hash text NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
 generation uuid NOT NULL,
 user_id uuid NOT NULL,
 role text NOT NULL CHECK (role IN ('admin','cashier','worker','individual')),
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,token_hash)
);
CREATE INDEX session_expiry ON bonuspro.sessions(expires_at);
ALTER TABLE bonuspro.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY session_app ON bonuspro.sessions TO bonuspro_app
USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant());
GRANT SELECT ON bonuspro.installations TO bonuspro_app;
GRANT SELECT,INSERT,DELETE ON bonuspro.sessions TO bonuspro_app;
GRANT USAGE ON SCHEMA bonuspro TO bonuspro_lifecycle;
GRANT SELECT,INSERT ON bonuspro.tenants TO bonuspro_lifecycle;
GRANT SELECT,INSERT,UPDATE ON bonuspro.installations TO bonuspro_lifecycle;
GRANT SELECT,INSERT,DELETE ON bonuspro.vendor_replays TO bonuspro_lifecycle;
