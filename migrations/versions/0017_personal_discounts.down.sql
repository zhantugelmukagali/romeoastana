-- Only for the deployment helper's unpublished, pre-traffic rollback.
-- The helper has stopped writers, locked every new table and established that
-- all four are empty as postgres, before selecting the non-bypass owner role.
CREATE OR REPLACE FUNCTION bonuspro.order_has_financial_history(p_tenant uuid,p_order uuid)
RETURNS boolean LANGUAGE sql SET search_path=pg_catalog,bonuspro AS $$
 SELECT EXISTS(SELECT 1 FROM bonuspro.reservations WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.redemption_attempts WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=p_tenant AND order_id=p_order);
$$;
REVOKE ALL ON FUNCTION bonuspro.order_has_financial_history(uuid,uuid) FROM PUBLIC;

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
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_reservation_order_binding() FROM PUBLIC;

DROP TRIGGER personal_discount_policy_version ON bonuspro.personal_discount_policy_versions;
DROP TRIGGER personal_discount_customer_version ON bonuspro.customer_personal_discount_versions;
DROP TRIGGER personal_discount_application_guard ON bonuspro.personal_discount_applications;
DROP TRIGGER personal_discount_void_completed ON bonuspro.personal_discount_voids;
DROP FUNCTION bonuspro.validate_personal_discount_void();
DROP FUNCTION bonuspro.guard_personal_discount_version();
DROP FUNCTION bonuspro.guard_personal_discount_application();
DROP TABLE bonuspro.personal_discount_voids;
DROP TABLE bonuspro.personal_discount_applications;
DROP TABLE bonuspro.customer_personal_discount_versions;
DROP TABLE bonuspro.personal_discount_policy_versions;
