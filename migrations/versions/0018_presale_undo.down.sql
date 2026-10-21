-- Unpublished pre-traffic rollback only: the helper locks/checks BOTH new
-- tables globally as postgres before selecting the non-bypass migration owner.
DROP TRIGGER presale_undo_complete ON bonuspro.presale_undo_completions;
DROP TRIGGER presale_refund_complete ON bonuspro.ledger_transactions;
DROP FUNCTION bonuspro.validate_presale_undo_complete();
DROP TRIGGER presale_undo_request_guard ON bonuspro.presale_undo_requests;
DROP FUNCTION bonuspro.guard_presale_undo_request();

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

CREATE OR REPLACE FUNCTION bonuspro.validate_committed_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Query final state: a deferred trigger can refer to an earlier row version.
  IF EXISTS (SELECT 1 FROM bonuspro.reservations r
    WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.id AND r.state='committed'
      AND NOT EXISTS (SELECT 1 FROM bonuspro.ledger_transactions t
        WHERE t.tenant_id=r.tenant_id AND t.reservation_id=r.id
          AND t.wallet_id=r.wallet_id AND t.delta=-r.points AND t.kind='redemption'))
  THEN RAISE EXCEPTION 'committed reservation lacks ledger' USING ERRCODE = '23514'; END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION bonuspro.guard_personal_discount_application()
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

ALTER TABLE bonuspro.reservations DROP CONSTRAINT reservation_state_kind;
ALTER TABLE bonuspro.reservations DROP CONSTRAINT reservation_saved_proof;
ALTER TABLE bonuspro.reservations ADD CONSTRAINT reservations_state_check
 CHECK(state IN ('held','verifying','committed','released'));
ALTER TABLE bonuspro.reservations ADD CONSTRAINT reservations_saved_proof
 CHECK((state='committed')=(proof_revision IS NOT NULL));

ALTER TABLE bonuspro.ledger_transactions DROP CONSTRAINT transaction_kind;
ALTER TABLE bonuspro.ledger_transactions ADD CONSTRAINT transaction_kind CHECK (
 (kind='credit' AND delta>0 AND reservation_id IS NULL AND credit_ref_id IS NULL AND settlement_event_id IS NULL) OR
 (kind='redemption' AND delta<0 AND reservation_id IS NOT NULL AND credit_ref_id IS NULL AND settlement_event_id IS NULL) OR
 (kind='reversal' AND delta<0 AND reservation_id IS NULL AND credit_ref_id IS NOT NULL AND settlement_event_id IS NULL) OR
 (kind='settlement' AND reservation_id IS NULL AND credit_ref_id IS NULL AND settlement_event_id IS NOT NULL)
);
ALTER TABLE bonuspro.ledger_transactions DROP CONSTRAINT presale_refund_request;
DROP INDEX bonuspro.presale_refund_once;
ALTER TABLE bonuspro.ledger_transactions DROP COLUMN presale_undo_id;
DROP TABLE bonuspro.presale_undo_completions;
DROP TABLE bonuspro.presale_undo_requests;
