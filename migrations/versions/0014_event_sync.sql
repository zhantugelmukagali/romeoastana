ALTER TABLE bonuspro.order_monitors ADD COLUMN hint_version bigint NOT NULL DEFAULT 0 CHECK(hint_version>=0);
ALTER TABLE bonuspro.order_monitors ADD COLUMN first_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE bonuspro.jobs ADD COLUMN hint_version bigint NOT NULL DEFAULT 0 CHECK(hint_version>=0);
GRANT UPDATE(hint_version) ON bonuspro.order_monitors TO bonuspro_app;

CREATE TABLE bonuspro.sync_streams (
 tenant_id uuid NOT NULL REFERENCES bonuspro.automation_config(tenant_id),
 generation uuid NOT NULL,
 stream text NOT NULL CHECK(stream IN ('customerorder','demand','salesreturn','paymentin','paymentout','cashin','cashout','created_rescan')),
 cursor_at timestamptz NOT NULL,
 round_until timestamptz,
 ready_at timestamptz NOT NULL DEFAULT now(),
 checked_at timestamptz,
 window_seconds integer NOT NULL DEFAULT 86400 CHECK(window_seconds BETWEEN 1 AND 604800),
 PRIMARY KEY(tenant_id,stream)
);
CREATE TABLE bonuspro.related_documents (
 tenant_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('customerorder','demand','salesreturn','paymentin','paymentout','cashin','cashout')),
 document_id uuid NOT NULL,
 order_id uuid NOT NULL,
 PRIMARY KEY(tenant_id,kind,document_id,order_id),
 FOREIGN KEY(tenant_id,order_id) REFERENCES bonuspro.order_monitors(tenant_id,order_id)
);
CREATE TABLE bonuspro.webhook_setup (
 tenant_id uuid PRIMARY KEY REFERENCES bonuspro.tenants(id),
 generation uuid NOT NULL,
 hook_count integer NOT NULL CHECK(hook_count BETWEEN 0 AND 21),
 checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bonuspro.webhook_inbox (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 generation uuid NOT NULL,
 request_id uuid NOT NULL,
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
 events jsonb NOT NULL CHECK(jsonb_typeof(events)='array' AND jsonb_array_length(events) BETWEEN 1 AND 100),
 next_event integer NOT NULL DEFAULT 0 CHECK(next_event>=0),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','done','discarded')),
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz,
 PRIMARY KEY(tenant_id,generation,request_id),
 CHECK(next_event<=jsonb_array_length(events)),
 CHECK((state='queued')=(completed_at IS NULL)),
 CHECK(state<>'done' OR next_event=jsonb_array_length(events))
);
CREATE INDEX webhook_pending ON bonuspro.webhook_inbox(tenant_id,received_at) WHERE state='queued';
DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['sync_streams','related_documents','webhook_setup','webhook_inbox']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
END $$;
GRANT UPDATE(generation,cursor_at,round_until,ready_at,checked_at,window_seconds) ON bonuspro.sync_streams TO bonuspro_app;
GRANT UPDATE(generation,hook_count,checked_at) ON bonuspro.webhook_setup TO bonuspro_app;
GRANT UPDATE(next_event,state,updated_at,completed_at) ON bonuspro.webhook_inbox TO bonuspro_app;
CREATE TRIGGER immutable_relation BEFORE UPDATE OR DELETE ON bonuspro.related_documents
FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();

CREATE FUNCTION bonuspro.guard_inbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
  IF OLD.state='queued' OR OLD.completed_at>clock_timestamp()-interval '30 days' THEN
   RAISE EXCEPTION 'active or recent inbox cannot be removed' USING ERRCODE='23514';
  END IF;
  RETURN OLD;
 END IF;
 IF ROW(NEW.tenant_id,NEW.generation,NEW.request_id,NEW.payload_hash,NEW.events,NEW.received_at)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.generation,OLD.request_id,OLD.payload_hash,OLD.events,OLD.received_at)
 OR OLD.state<>'queued' OR NEW.next_event<OLD.next_event THEN
  RAISE EXCEPTION 'immutable inbox identity or completed state' USING ERRCODE='23514';
 END IF;
 NEW.updated_at:=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER inbox_guard BEFORE UPDATE OR DELETE ON bonuspro.webhook_inbox
FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_inbox();
GRANT DELETE ON bonuspro.webhook_inbox TO bonuspro_app;

CREATE OR REPLACE FUNCTION bonuspro.guard_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.operation_key,NEW.request_hash,NEW.kind,NEW.entity_id,NEW.created_at,NEW.hint_version)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.operation_key,OLD.request_hash,OLD.kind,OLD.entity_id,OLD.created_at,OLD.hint_version)
 OR OLD.state IN ('done','dead') THEN
  RAISE EXCEPTION 'immutable job identity or terminal state' USING ERRCODE='23514';
 END IF;
 NEW.updated_at:=now();
 RETURN NEW;
END $$;
CREATE FUNCTION bonuspro.prune_terminal_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state NOT IN ('done','dead') OR OLD.updated_at>clock_timestamp()-interval '30 days' THEN
  RAISE EXCEPTION 'active or recent job cannot be removed' USING ERRCODE='23514';
 END IF;
 RETURN OLD;
END $$;
DROP TRIGGER job_no_delete ON bonuspro.jobs;
CREATE TRIGGER job_no_delete BEFORE DELETE ON bonuspro.jobs
FOR EACH ROW EXECUTE FUNCTION bonuspro.prune_terminal_job();
GRANT DELETE ON bonuspro.jobs TO bonuspro_app;
CREATE INDEX jobs_terminal_retention ON bonuspro.jobs(tenant_id,updated_at) WHERE state IN ('done','dead');
CREATE INDEX inbox_terminal_retention ON bonuspro.webhook_inbox(tenant_id,completed_at) WHERE state IN ('done','discarded');
