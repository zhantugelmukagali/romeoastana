-- Verified presale cancellation preserves every original posting and plan.
CREATE TABLE bonuspro.presale_undo_requests (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 operation_id uuid NOT NULL,
 order_id uuid NOT NULL,
 wallet_id uuid NOT NULL,
 reservation_id uuid,
 personal_application_id uuid,
 points bigint NOT NULL CHECK(points BETWEEN 0 AND 90000000000000),
 actor_id uuid NOT NULL,
 generation uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 source_hash text NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
 source_revision text NOT NULL CHECK(length(btrim(source_revision)) BETWEEN 1 AND 256),
 marker_id uuid NOT NULL,
 marker text NOT NULL CHECK(length(btrim(marker)) BETWEEN 1 AND 256),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 500),
 plan jsonb NOT NULL CHECK(jsonb_typeof(plan)='object'
   AND plan ?& ARRAY['before','after','update_state']
   AND jsonb_typeof(plan->'before')='object' AND jsonb_typeof(plan->'after')='object'
   AND jsonb_typeof(plan->'update_state')='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,id,wallet_id),
 UNIQUE(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY(tenant_id,wallet_id) REFERENCES bonuspro.wallets(tenant_id,id),
 FOREIGN KEY(tenant_id,reservation_id) REFERENCES bonuspro.reservations(tenant_id,id),
 FOREIGN KEY(tenant_id,personal_application_id)
   REFERENCES bonuspro.personal_discount_applications(tenant_id,id),
 CHECK(reservation_id IS NOT NULL OR personal_application_id IS NOT NULL),
 CHECK((points=0)=(reservation_id IS NULL)),
 CHECK(operation_id=COALESCE(personal_application_id,reservation_id)),
 CHECK(reservation_id IS NULL OR personal_application_id IS NULL
   OR reservation_id=personal_application_id)
);

ALTER TABLE bonuspro.ledger_transactions ADD COLUMN presale_undo_id uuid;
ALTER TABLE bonuspro.ledger_transactions ADD CONSTRAINT presale_refund_request
 FOREIGN KEY(tenant_id,presale_undo_id,wallet_id)
 REFERENCES bonuspro.presale_undo_requests(tenant_id,id,wallet_id);
CREATE UNIQUE INDEX presale_refund_once ON bonuspro.ledger_transactions(tenant_id,presale_undo_id)
 WHERE presale_undo_id IS NOT NULL;
ALTER TABLE bonuspro.ledger_transactions DROP CONSTRAINT transaction_kind;
ALTER TABLE bonuspro.ledger_transactions ADD CONSTRAINT transaction_kind CHECK (
 (kind='credit' AND delta>0 AND reservation_id IS NULL AND credit_ref_id IS NULL
   AND settlement_event_id IS NULL AND presale_undo_id IS NULL) OR
 (kind='redemption' AND delta<0 AND reservation_id IS NOT NULL AND credit_ref_id IS NULL
   AND settlement_event_id IS NULL AND presale_undo_id IS NULL) OR
 (kind='reversal' AND delta<0 AND reservation_id IS NULL AND credit_ref_id IS NOT NULL
   AND settlement_event_id IS NULL AND presale_undo_id IS NULL) OR
 (kind='settlement' AND reservation_id IS NULL AND credit_ref_id IS NULL
   AND settlement_event_id IS NOT NULL AND presale_undo_id IS NULL) OR
 (kind='presale_refund' AND delta>0 AND reservation_id IS NULL AND credit_ref_id IS NULL
   AND settlement_event_id IS NULL AND presale_undo_id IS NOT NULL)
);

CREATE TABLE bonuspro.presale_undo_completions (
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 generation uuid NOT NULL,
 evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
 proof_revision text NOT NULL CHECK(length(btrim(proof_revision)) BETWEEN 1 AND 256),
 transaction_id uuid,
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,request_id),
 UNIQUE(tenant_id,transaction_id),
 FOREIGN KEY(tenant_id,request_id) REFERENCES bonuspro.presale_undo_requests(tenant_id,id),
 FOREIGN KEY(tenant_id,transaction_id) REFERENCES bonuspro.ledger_transactions(tenant_id,id)
);

DO $$
DECLARE item record;
BEGIN
 FOR item IN SELECT conname FROM pg_constraint
  WHERE conrelid='bonuspro.reservations'::regclass AND contype='c'
    AND pg_get_constraintdef(oid) LIKE '%state%'
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.reservations DROP CONSTRAINT %I',item.conname);
 END LOOP;
END $$;
ALTER TABLE bonuspro.reservations ADD CONSTRAINT reservation_state_kind
 CHECK(state IN ('held','verifying','committed','released','reversed'));
ALTER TABLE bonuspro.reservations ADD CONSTRAINT reservation_saved_proof
 CHECK((state IN ('committed','reversed'))=(proof_revision IS NOT NULL));

DO $$
DECLARE tbl text;
BEGIN
 FOREACH tbl IN ARRAY ARRAY['presale_undo_requests','presale_undo_completions']
 LOOP
  EXECUTE format('ALTER TABLE bonuspro.%I ENABLE ROW LEVEL SECURITY',tbl);
  EXECUTE format('ALTER TABLE bonuspro.%I FORCE ROW LEVEL SECURITY',tbl);
  EXECUTE format('CREATE POLICY tenant_isolation ON bonuspro.%I USING(tenant_id=bonuspro.current_tenant()) WITH CHECK(tenant_id=bonuspro.current_tenant())',tbl);
  EXECUTE format('GRANT SELECT,INSERT ON bonuspro.%I TO bonuspro_app',tbl);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.%I FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation()',tbl);
 END LOOP;
END $$;

CREATE FUNCTION bonuspro.guard_presale_undo_request()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
DECLARE
 installed bonuspro.installations;
 context bonuspro.order_contexts;
 personal bonuspro.personal_discount_applications;
 reserved bonuspro.reservations;
 attempt bonuspro.redemption_attempts;
 expected jsonb;
BEGIN
 IF NEW.tenant_id IS DISTINCT FROM bonuspro.current_tenant() THEN
  RAISE EXCEPTION 'tenant context does not match presale undo' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('installation/'||NEW.tenant_id::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/settings',0));
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/order/'||NEW.order_id::text,0));
 SELECT * INTO installed FROM bonuspro.installations WHERE tenant_id=NEW.tenant_id;
 IF NOT FOUND OR installed.state<>'Activated' OR installed.generation<>NEW.generation THEN
  RAISE EXCEPTION 'presale undo installation changed' USING ERRCODE='23514';
 END IF;
 SELECT * INTO context FROM bonuspro.order_contexts
  WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id;
 IF NOT FOUND OR context.wallet_id<>NEW.wallet_id THEN
  RAISE EXCEPTION 'presale undo belongs to another order wallet' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id) THEN
  RAISE EXCEPTION 'sale or reconciliation history prevents presale undo' USING ERRCODE='23514';
 END IF;
 IF NEW.personal_application_id IS NOT NULL THEN
  SELECT * INTO personal FROM bonuspro.personal_discount_applications
   WHERE tenant_id=NEW.tenant_id AND id=NEW.personal_application_id;
  IF NOT FOUND OR personal.state<>'confirmed' OR personal.order_id<>NEW.order_id
   OR personal.wallet_id<>NEW.wallet_id OR personal.points<>NEW.points
   OR personal.redemption_id IS DISTINCT FROM NEW.reservation_id
   OR personal.marker_id<>NEW.marker_id OR personal.marker<>NEW.marker THEN
   RAISE EXCEPTION 'confirmed personal application required for undo' USING ERRCODE='23514';
  END IF;
  expected:=personal.plan;
 ELSE
  SELECT * INTO attempt FROM bonuspro.redemption_attempts
   WHERE tenant_id=NEW.tenant_id AND id=NEW.reservation_id;
  IF NOT FOUND OR attempt.order_id<>NEW.order_id OR attempt.marker_id<>NEW.marker_id
   OR attempt.marker<>NEW.marker OR EXISTS(
    SELECT 1 FROM bonuspro.personal_discount_applications
     WHERE tenant_id=NEW.tenant_id AND redemption_id=NEW.reservation_id) THEN
   RAISE EXCEPTION 'bonus application does not match presale undo' USING ERRCODE='23514';
  END IF;
  expected:=attempt.plan;
 END IF;
 IF NEW.reservation_id IS NOT NULL THEN
  SELECT * INTO reserved FROM bonuspro.reservations
   WHERE tenant_id=NEW.tenant_id AND id=NEW.reservation_id;
  IF NOT FOUND OR reserved.state<>'committed' OR reserved.wallet_id<>NEW.wallet_id
   OR reserved.order_id<>NEW.order_id OR reserved.points<>NEW.points THEN
   RAISE EXCEPTION 'committed bonus application required for undo' USING ERRCODE='23514';
  END IF;
 END IF;
 IF NEW.plan->'before' IS DISTINCT FROM expected->'after'
 OR NEW.plan->'after' IS DISTINCT FROM expected->'before' THEN
  RAISE EXCEPTION 'presale undo must invert the frozen application' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_presale_undo_request() FROM PUBLIC;
CREATE TRIGGER presale_undo_request_guard BEFORE INSERT ON bonuspro.presale_undo_requests
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_presale_undo_request();

CREATE OR REPLACE FUNCTION bonuspro.guard_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.tenant_id,NEW.id,NEW.wallet_id,NEW.operation_key,NEW.request_hash,
        NEW.order_id,NEW.quote_hash,NEW.points,NEW.actor_id,NEW.created_at)
 IS DISTINCT FROM
    ROW(OLD.tenant_id,OLD.id,OLD.wallet_id,OLD.operation_key,OLD.request_hash,
        OLD.order_id,OLD.quote_hash,OLD.points,OLD.actor_id,OLD.created_at) THEN
  RAISE EXCEPTION 'reservation identity is immutable' USING ERRCODE='23514';
 END IF;
 IF NOT ((OLD.state='held' AND NEW.state IN ('verifying','released'))
  OR (OLD.state='verifying' AND (NEW.state='committed' OR
    (NEW.state='released' AND EXISTS(SELECT 1 FROM bonuspro.redemption_voids v
      WHERE v.tenant_id=NEW.tenant_id AND v.reservation_id=NEW.id))))
  OR (OLD.state='committed' AND NEW.state='reversed'
    AND NEW.proof_revision IS NOT DISTINCT FROM OLD.proof_revision
    AND EXISTS(SELECT 1 FROM bonuspro.presale_undo_requests q
      JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
      WHERE q.tenant_id=NEW.tenant_id AND q.reservation_id=NEW.id
        AND q.order_id=NEW.order_id AND q.wallet_id=NEW.wallet_id AND q.points=NEW.points))) THEN
  RAISE EXCEPTION 'invalid reservation transition' USING ERRCODE='23514';
 END IF;
 NEW.updated_at:=now();
 RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION bonuspro.validate_committed_reservation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_row bonuspro.reservations;
BEGIN
 SELECT * INTO current_row FROM bonuspro.reservations WHERE tenant_id=NEW.tenant_id AND id=NEW.id;
 IF current_row.state IN ('committed','reversed') AND NOT EXISTS(
  SELECT 1 FROM bonuspro.ledger_transactions t
   WHERE t.tenant_id=current_row.tenant_id AND t.reservation_id=current_row.id
    AND t.wallet_id=current_row.wallet_id AND t.delta=-current_row.points AND t.kind='redemption') THEN
  RAISE EXCEPTION 'committed reservation lacks ledger' USING ERRCODE='23514';
 END IF;
 IF current_row.state='reversed' AND NOT EXISTS(
  SELECT 1 FROM bonuspro.presale_undo_requests q
   JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
   WHERE q.tenant_id=current_row.tenant_id AND q.reservation_id=current_row.id
     AND q.wallet_id=current_row.wallet_id AND q.points=current_row.points) THEN
  RAISE EXCEPTION 'reversed reservation lacks presale receipt' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION bonuspro.validate_presale_undo_complete()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
DECLARE
 selected_id uuid;
 q bonuspro.presale_undo_requests;
 c bonuspro.presale_undo_completions;
BEGIN
 IF TG_TABLE_NAME='ledger_transactions' THEN
  selected_id:=NEW.presale_undo_id;
 ELSE
  selected_id:=NEW.request_id;
 END IF;
 IF selected_id IS NULL THEN RETURN NULL; END IF;
 SELECT * INTO STRICT q FROM bonuspro.presale_undo_requests
  WHERE tenant_id=NEW.tenant_id AND id=selected_id;
 -- Constraint validation can be forced early with SET CONSTRAINTS. Keep the
 -- lifecycle/settings/order fences until COMMIT, including direct SQL callers.
 PERFORM pg_advisory_xact_lock(hashtextextended('installation/'||q.tenant_id::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended(q.tenant_id::text||'/settings',0));
 PERFORM pg_advisory_xact_lock(hashtextextended(q.tenant_id::text||'/order/'||q.order_id::text,0));
 SELECT * INTO c FROM bonuspro.presale_undo_completions
  WHERE tenant_id=NEW.tenant_id AND request_id=selected_id;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'presale refund lacks completion receipt' USING ERRCODE='23514';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM bonuspro.installations i
  WHERE i.tenant_id=q.tenant_id AND i.state='Activated' AND i.generation=c.generation) THEN
  RAISE EXCEPTION 'presale undo installation changed' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=q.tenant_id AND order_id=q.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=q.tenant_id AND order_id=q.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=q.tenant_id AND order_id=q.order_id)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=q.tenant_id AND order_id=q.order_id) THEN
  RAISE EXCEPTION 'sale or reconciliation history prevents presale refund' USING ERRCODE='23514';
 END IF;
 IF q.points>0 THEN
  IF NOT EXISTS(SELECT 1 FROM bonuspro.reservations r
   WHERE r.tenant_id=q.tenant_id AND r.id=q.reservation_id AND r.order_id=q.order_id
     AND r.wallet_id=q.wallet_id AND r.points=q.points AND r.state='reversed') THEN
   RAISE EXCEPTION 'presale refund requires reversed reservation' USING ERRCODE='23514';
  END IF;
  IF c.transaction_id IS NULL OR NOT EXISTS(SELECT 1 FROM bonuspro.ledger_transactions t
   WHERE t.tenant_id=q.tenant_id AND t.id=c.transaction_id AND t.wallet_id=q.wallet_id
    AND t.kind='presale_refund' AND t.delta=q.points AND t.presale_undo_id=q.id
    AND t.actor_id=c.actor_id) THEN
   RAISE EXCEPTION 'presale receipt lacks matching refund journal' USING ERRCODE='23514';
  END IF;
 ELSE
  IF c.transaction_id IS NOT NULL OR EXISTS(SELECT 1 FROM bonuspro.ledger_transactions t
   WHERE t.tenant_id=q.tenant_id AND t.presale_undo_id=q.id) THEN
   RAISE EXCEPTION 'zero-point presale undo must not post' USING ERRCODE='23514';
  END IF;
 END IF;
 IF q.personal_application_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM bonuspro.personal_discount_applications p
   WHERE p.tenant_id=q.tenant_id AND p.id=q.personal_application_id
     AND p.order_id=q.order_id AND p.wallet_id=q.wallet_id AND p.points=q.points
     AND p.state='voided') THEN
  RAISE EXCEPTION 'presale receipt requires voided personal application' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION bonuspro.validate_presale_undo_complete() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER presale_undo_complete AFTER INSERT ON bonuspro.presale_undo_completions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_presale_undo_complete();
CREATE CONSTRAINT TRIGGER presale_refund_complete AFTER INSERT ON bonuspro.ledger_transactions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION bonuspro.validate_presale_undo_complete();

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
 IF OLD.state='confirmed' AND NEW.state='voided' THEN
  IF NEW.proof_revision IS DISTINCT FROM OLD.proof_revision OR NOT EXISTS(
   SELECT 1 FROM bonuspro.presale_undo_requests q
    JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
    WHERE q.tenant_id=NEW.tenant_id AND q.personal_application_id=NEW.id
      AND q.order_id=NEW.order_id AND q.wallet_id=NEW.wallet_id AND q.points=NEW.points) THEN
   RAISE EXCEPTION 'confirmed personal undo requires completion receipt' USING ERRCODE='23514';
  END IF;
  IF NEW.points>0 AND NOT EXISTS(SELECT 1 FROM bonuspro.reservations
   WHERE tenant_id=NEW.tenant_id AND id=NEW.redemption_id AND state='reversed') THEN
   RAISE EXCEPTION 'combined personal undo requires reversed bonus debit' USING ERRCODE='23514';
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

-- Only terminal, completely refunded presale generations stop pinning the buyer.
CREATE OR REPLACE FUNCTION bonuspro.order_has_financial_history(p_tenant uuid,p_order uuid)
RETURNS boolean LANGUAGE sql SET search_path=pg_catalog,bonuspro AS $$
 SELECT EXISTS(SELECT 1 FROM bonuspro.reservations r
  WHERE r.tenant_id=p_tenant AND r.order_id=p_order
    AND NOT (r.state='reversed' AND EXISTS(
      SELECT 1 FROM bonuspro.presale_undo_requests q
       JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
       WHERE q.tenant_id=r.tenant_id AND q.reservation_id=r.id)))
 OR EXISTS(SELECT 1 FROM bonuspro.redemption_attempts a
  WHERE a.tenant_id=p_tenant AND a.order_id=p_order AND NOT EXISTS(
    SELECT 1 FROM bonuspro.presale_undo_requests q
     JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
     JOIN bonuspro.reservations r ON r.tenant_id=q.tenant_id AND r.id=q.reservation_id
     WHERE q.tenant_id=a.tenant_id AND q.reservation_id=a.id AND r.state='reversed'))
 OR EXISTS(SELECT 1 FROM bonuspro.personal_discount_applications p
  WHERE p.tenant_id=p_tenant AND p.order_id=p_order
    AND NOT (p.state='voided' AND EXISTS(
      SELECT 1 FROM bonuspro.presale_undo_requests q
       JOIN bonuspro.presale_undo_completions c ON c.tenant_id=q.tenant_id AND c.request_id=q.id
       WHERE q.tenant_id=p.tenant_id AND q.personal_application_id=p.id)))
 OR EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=p_tenant AND order_id=p_order);
$$;
REVOKE ALL ON FUNCTION bonuspro.order_has_financial_history(uuid,uuid) FROM PUBLIC;
