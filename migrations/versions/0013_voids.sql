CREATE TABLE bonuspro.redemption_voids(
 tenant_id uuid NOT NULL,
 reservation_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,reservation_id),
 FOREIGN KEY(tenant_id,reservation_id) REFERENCES bonuspro.redemption_attempts(tenant_id,id)
);
ALTER TABLE bonuspro.redemption_voids ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.redemption_voids FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bonuspro.redemption_voids USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant());
GRANT SELECT,INSERT ON bonuspro.redemption_voids TO bonuspro_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.redemption_voids FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();
CREATE OR REPLACE FUNCTION bonuspro.guard_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.tenant_id,NEW.id,NEW.wallet_id,NEW.operation_key,NEW.request_hash,
         NEW.order_id,NEW.quote_hash,NEW.points,NEW.actor_id,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.id,OLD.wallet_id,OLD.operation_key,OLD.request_hash,
         OLD.order_id,OLD.quote_hash,OLD.points,OLD.actor_id,OLD.created_at) THEN
    RAISE EXCEPTION 'reservation identity is immutable' USING ERRCODE = '23514';
  END IF;
  IF NOT ((OLD.state='held' AND NEW.state IN ('verifying','released'))
       OR (OLD.state='verifying' AND (NEW.state='committed' OR (NEW.state='released' AND EXISTS(SELECT 1 FROM bonuspro.redemption_voids v WHERE v.tenant_id=NEW.tenant_id AND v.reservation_id=NEW.id))))) THEN
    RAISE EXCEPTION 'invalid reservation transition' USING ERRCODE = '23514';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE FUNCTION bonuspro.validate_void() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM bonuspro.reservations WHERE tenant_id=NEW.tenant_id AND id=NEW.reservation_id AND state='released')
 THEN RAISE EXCEPTION 'void requires released reservation' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER void_complete AFTER INSERT ON bonuspro.redemption_voids DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_void();

ALTER TABLE bonuspro.reconciliation_issues DROP CONSTRAINT reconciliation_issues_reason_check;
ALTER TABLE bonuspro.reconciliation_issues ADD CONSTRAINT reconciliation_issues_reason_check
 CHECK(reason IN ('changed_credited_order','provider_document_missing','invalid_credited_document','unrecognized_bonus_marker'));
