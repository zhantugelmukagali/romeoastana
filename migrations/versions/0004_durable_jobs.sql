CREATE TABLE bonuspro.jobs (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 id uuid NOT NULL,
 operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 1 AND 128),
 request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
 kind text NOT NULL CHECK (kind IN ('reconcile_order')),
 entity_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','done','dead')),
 attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
 available_at timestamptz NOT NULL DEFAULT now(),
 lease_id uuid,
 lease_until timestamptz,
 last_error text CHECK (last_error IN ('provider_timeout','provider_unavailable','conflict','invalid_document','attempts_exhausted')),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,id),
 UNIQUE (tenant_id,operation_key),
 CHECK ((state='running' AND lease_id IS NOT NULL AND lease_until IS NOT NULL)
     OR (state<>'running' AND lease_id IS NULL AND lease_until IS NULL))
);
CREATE INDEX jobs_due ON bonuspro.jobs(tenant_id,available_at) WHERE state='queued';
CREATE INDEX jobs_expired ON bonuspro.jobs(tenant_id,lease_until) WHERE state='running';
ALTER TABLE bonuspro.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bonuspro.jobs
 USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant());
GRANT SELECT,INSERT ON bonuspro.jobs TO bonuspro_app;
GRANT UPDATE(state,attempts,available_at,lease_id,lease_until,last_error,updated_at)
 ON bonuspro.jobs TO bonuspro_app;
CREATE FUNCTION bonuspro.guard_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.operation_key,NEW.request_hash,NEW.kind,NEW.entity_id,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.operation_key,OLD.request_hash,OLD.kind,OLD.entity_id,OLD.created_at)
 OR OLD.state IN ('done','dead') THEN
  RAISE EXCEPTION 'immutable job identity or terminal state' USING ERRCODE='23514';
 END IF;
 NEW.updated_at := now();
 RETURN NEW;
END $$;
CREATE TRIGGER job_guard BEFORE UPDATE ON bonuspro.jobs FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_job();
CREATE TRIGGER job_no_delete BEFORE DELETE ON bonuspro.jobs FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
