-- Personal discounts are opt-in. No rows are backfilled, and no existing
-- order, wallet, reservation, rule version or posting is rewritten.
CREATE TABLE bonuspro.personal_discount_policy_versions (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 version integer NOT NULL CHECK(version>0),
 enabled boolean NOT NULL,
 external_policy text NOT NULL CHECK(external_policy IN ('skip','best','sequential')),
 allow_bonus_stack boolean NOT NULL,
 request_id uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,version),
 UNIQUE(tenant_id,request_id)
);
CREATE TABLE bonuspro.customer_personal_discount_versions (
 tenant_id uuid NOT NULL REFERENCES bonuspro.tenants(id),
 counterparty_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0),
 -- NULL removes an assignment; zero is an explicit zero-rate assignment.
 rate_bps integer CHECK(rate_bps BETWEEN 0 AND 10000),
 request_id uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,counterparty_id,version),
 UNIQUE(tenant_id,request_id)
);
CREATE INDEX personal_discount_assignments_current
 ON bonuspro.customer_personal_discount_versions(tenant_id,counterparty_id,version DESC);

CREATE TABLE bonuspro.personal_discount_applications (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 order_id uuid NOT NULL,
 counterparty_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 request_id uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
 actor_id uuid NOT NULL,
 marker_id uuid NOT NULL,
 marker text NOT NULL CHECK(length(btrim(marker)) BETWEEN 1 AND 256),
 policy_version integer NOT NULL,
 assignment_version integer NOT NULL,
 rate_bps integer NOT NULL CHECK(rate_bps BETWEEN 1 AND 10000),
 points bigint NOT NULL CHECK(points BETWEEN 0 AND 90000000000000),
 redemption_id uuid,
 plan jsonb NOT NULL CHECK(jsonb_typeof(plan)='object'),
 state text NOT NULL DEFAULT 'verifying' CHECK(state IN ('verifying','confirmed','voided')),
 proof_revision text CHECK(proof_revision IS NULL OR length(btrim(proof_revision)) BETWEEN 1 AND 256),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,request_id),
 FOREIGN KEY(tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY(tenant_id,counterparty_id,wallet_id)
  REFERENCES bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id),
 FOREIGN KEY(tenant_id,policy_version)
  REFERENCES bonuspro.personal_discount_policy_versions(tenant_id,version),
 FOREIGN KEY(tenant_id,counterparty_id,assignment_version)
  REFERENCES bonuspro.customer_personal_discount_versions(tenant_id,counterparty_id,version),
 FOREIGN KEY(tenant_id,redemption_id) REFERENCES bonuspro.reservations(tenant_id,id),
 CHECK((points=0)=(redemption_id IS NULL)),
 CHECK(redemption_id IS NULL OR id=redemption_id),
 CHECK((state='confirmed' AND proof_revision IS NOT NULL)
       OR (state='verifying' AND proof_revision IS NULL) OR state='voided')
);
CREATE UNIQUE INDEX personal_discount_application_active_order
 ON bonuspro.personal_discount_applications(tenant_id,order_id)
 WHERE state IN ('verifying','confirmed');

CREATE TABLE bonuspro.personal_discount_voids (
 tenant_id uuid NOT NULL,
 application_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
 proof_revision text NOT NULL CHECK(length(btrim(proof_revision)) BETWEEN 1 AND 256),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,application_id),
 FOREIGN KEY(tenant_id,application_id)
  REFERENCES bonuspro.personal_discount_applications(tenant_id,id)
);

DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['personal_discount_policy_versions',
  'customer_personal_discount_versions','personal_discount_applications','personal_discount_voids']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
 END LOOP;
 FOREACH tbl IN ARRAY ARRAY['personal_discount_policy_versions',
  'customer_personal_discount_versions','personal_discount_voids']
 LOOP
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
 END LOOP;
END $$;
GRANT UPDATE(state,proof_revision,updated_at)
 ON bonuspro.personal_discount_applications TO bonuspro_app;

CREATE FUNCTION bonuspro.guard_personal_discount_version()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
DECLARE current_version integer;
BEGIN
 IF NEW.tenant_id IS DISTINCT FROM bonuspro.current_tenant() THEN
  RAISE EXCEPTION 'tenant context does not match personal discount' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/settings',0));
 IF TG_TABLE_NAME='personal_discount_policy_versions' THEN
  SELECT COALESCE(max(version),0) INTO current_version
   FROM bonuspro.personal_discount_policy_versions WHERE tenant_id=NEW.tenant_id;
 ELSE
  SELECT COALESCE(max(version),0) INTO current_version
   FROM bonuspro.customer_personal_discount_versions
   WHERE tenant_id=NEW.tenant_id AND counterparty_id=NEW.counterparty_id;
 END IF;
 IF NEW.version<>current_version+1 THEN
  RAISE EXCEPTION 'personal discount version is stale' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_personal_discount_version() FROM PUBLIC;
CREATE TRIGGER personal_discount_policy_version BEFORE INSERT
 ON bonuspro.personal_discount_policy_versions
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_personal_discount_version();
CREATE TRIGGER personal_discount_customer_version BEFORE INSERT
 ON bonuspro.customer_personal_discount_versions
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_personal_discount_version();

CREATE FUNCTION bonuspro.guard_personal_discount_application()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
DECLARE
 context bonuspro.order_contexts;
 policy bonuspro.personal_discount_policy_versions;
 assigned_rate integer;
 reserved bonuspro.reservations;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'personal discount history is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.tenant_id IS DISTINCT FROM bonuspro.current_tenant() THEN
  RAISE EXCEPTION 'tenant context does not match personal discount' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/order/'||NEW.order_id::text,0));
 IF TG_OP='INSERT' THEN
  IF NEW.state<>'verifying' OR NEW.proof_revision IS NOT NULL THEN
   RAISE EXCEPTION 'new personal discount must await verification' USING ERRCODE='23514';
  END IF;
  SELECT * INTO context FROM bonuspro.order_contexts
   WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id;
  IF NOT FOUND OR context.counterparty_id<>NEW.counterparty_id OR context.wallet_id<>NEW.wallet_id THEN
   RAISE EXCEPTION 'personal discount belongs to another order customer' USING ERRCODE='23514';
  END IF;
  IF EXISTS(SELECT 1 FROM bonuspro.reservations
   WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id
   AND state IN ('held','verifying','committed')
   AND id IS DISTINCT FROM NEW.redemption_id) THEN
   RAISE EXCEPTION 'order already has another bonus reservation' USING ERRCODE='23514';
  END IF;
  SELECT * INTO policy FROM bonuspro.personal_discount_policy_versions
   WHERE tenant_id=NEW.tenant_id AND version=NEW.policy_version;
  IF NOT FOUND OR NOT policy.enabled THEN
   RAISE EXCEPTION 'personal discount policy is not enabled' USING ERRCODE='23514';
  END IF;
  SELECT rate_bps INTO assigned_rate FROM bonuspro.customer_personal_discount_versions
   WHERE tenant_id=NEW.tenant_id AND counterparty_id=NEW.counterparty_id AND version=NEW.assignment_version;
  IF NOT FOUND OR assigned_rate IS DISTINCT FROM NEW.rate_bps THEN
   RAISE EXCEPTION 'personal discount assignment does not match' USING ERRCODE='23514';
  END IF;
  IF NEW.points>0 THEN
   SELECT * INTO reserved FROM bonuspro.reservations
    WHERE tenant_id=NEW.tenant_id AND id=NEW.redemption_id;
   IF NOT policy.allow_bonus_stack OR NOT FOUND
   OR reserved.wallet_id<>NEW.wallet_id OR reserved.order_id<>NEW.order_id
   OR reserved.points<>NEW.points OR reserved.state<>'verifying' THEN
    RAISE EXCEPTION 'combined personal discount reservation does not match' USING ERRCODE='23514';
   END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF ROW(NEW.tenant_id,NEW.id,NEW.order_id,NEW.counterparty_id,NEW.wallet_id,NEW.request_id,
        NEW.request_hash,NEW.source_hash,NEW.actor_id,NEW.marker_id,NEW.marker,NEW.policy_version,
        NEW.assignment_version,NEW.rate_bps,NEW.points,NEW.redemption_id,NEW.plan,NEW.created_at)
 IS DISTINCT FROM
    ROW(OLD.tenant_id,OLD.id,OLD.order_id,OLD.counterparty_id,OLD.wallet_id,OLD.request_id,
        OLD.request_hash,OLD.source_hash,OLD.actor_id,OLD.marker_id,OLD.marker,OLD.policy_version,
        OLD.assignment_version,OLD.rate_bps,OLD.points,OLD.redemption_id,OLD.plan,OLD.created_at) THEN
  RAISE EXCEPTION 'personal discount plan is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.state=OLD.state THEN
  IF NEW.proof_revision IS DISTINCT FROM OLD.proof_revision THEN
   RAISE EXCEPTION 'personal discount proof is immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF OLD.state<>'verifying' OR NEW.state NOT IN ('confirmed','voided') THEN
  RAISE EXCEPTION 'invalid personal discount transition' USING ERRCODE='23514';
 END IF;
 IF NEW.state='confirmed' AND NEW.proof_revision IS NULL THEN
  RAISE EXCEPTION 'saved personal discount proof is required' USING ERRCODE='23514';
 END IF;
 IF NEW.state='voided' AND NOT EXISTS(
  SELECT 1 FROM bonuspro.personal_discount_voids
   WHERE tenant_id=OLD.tenant_id AND application_id=OLD.id) THEN
  RAISE EXCEPTION 'personal discount void audit is required' USING ERRCODE='23514';
 END IF;
 IF NEW.points>0 AND NEW.state='confirmed' AND NOT EXISTS(
  SELECT 1 FROM bonuspro.reservations
   WHERE tenant_id=NEW.tenant_id AND id=NEW.redemption_id AND state='committed') THEN
  RAISE EXCEPTION 'combined bonus debit is not confirmed' USING ERRCODE='23514';
 END IF;
 IF NEW.points>0 AND NEW.state='voided' AND NOT EXISTS(
  SELECT 1 FROM bonuspro.reservations
   WHERE tenant_id=NEW.tenant_id AND id=NEW.redemption_id AND state='released') THEN
  RAISE EXCEPTION 'combined bonus reservation is not released' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_personal_discount_application() FROM PUBLIC;
CREATE TRIGGER personal_discount_application_guard BEFORE INSERT OR UPDATE OR DELETE
 ON bonuspro.personal_discount_applications
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_personal_discount_application();

CREATE OR REPLACE FUNCTION bonuspro.order_has_financial_history(p_tenant uuid,p_order uuid)
RETURNS boolean LANGUAGE sql SET search_path=pg_catalog,bonuspro AS $$
 SELECT EXISTS(SELECT 1 FROM bonuspro.reservations WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.redemption_attempts WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.personal_discount_applications WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=p_tenant AND order_id=p_order);
$$;
REVOKE ALL ON FUNCTION bonuspro.order_has_financial_history(uuid,uuid) FROM PUBLIC;

-- Both discount producers serialize the same order; a combined plan deliberately
-- uses its reservation UUID as its application UUID.
CREATE OR REPLACE FUNCTION bonuspro.guard_reservation_order_binding()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
DECLARE selected_wallet uuid;
BEGIN
 IF NEW.tenant_id IS DISTINCT FROM bonuspro.current_tenant() THEN
  RAISE EXCEPTION 'tenant context does not match reservation' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/order/'||NEW.order_id::text,0));
 SELECT wallet_id INTO selected_wallet FROM bonuspro.order_contexts
  WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id;
 IF FOUND AND selected_wallet<>NEW.wallet_id THEN
  RAISE EXCEPTION 'order reservation belongs to another wallet' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM bonuspro.personal_discount_applications
  WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id
   AND state IN ('verifying','confirmed')
   AND (redemption_id IS DISTINCT FROM NEW.id OR points IS DISTINCT FROM NEW.points)) THEN
  RAISE EXCEPTION 'order already has a personal discount application' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_reservation_order_binding() FROM PUBLIC;

-- An audit record must describe a completed cancellation in this same
-- transaction, rather than reserving the audit UUID for a still-active plan.
CREATE FUNCTION bonuspro.validate_personal_discount_void()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM bonuspro.personal_discount_applications
  WHERE tenant_id=NEW.tenant_id AND id=NEW.application_id AND state='voided') THEN
  RAISE EXCEPTION 'personal discount void is incomplete' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.validate_personal_discount_void() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER personal_discount_void_completed AFTER INSERT
 ON bonuspro.personal_discount_voids DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_personal_discount_void();
