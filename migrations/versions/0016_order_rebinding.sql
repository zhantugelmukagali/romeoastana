-- Existing rows remain unfenced until fresh provider evidence is observed.
ALTER TABLE bonuspro.order_contexts ADD COLUMN binding_revision text
 CHECK (binding_revision IS NULL OR length(btrim(binding_revision)) BETWEEN 1 AND 256);
CREATE TABLE bonuspro.order_customer_rebindings (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 order_id uuid NOT NULL,
 old_counterparty_id uuid NOT NULL,
 new_counterparty_id uuid NOT NULL,
 old_wallet_id uuid NOT NULL,
 new_wallet_id uuid NOT NULL,
 rule_version integer NOT NULL,
 source_hash text NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
 source_revision text NOT NULL CHECK (length(btrim(source_revision)) BETWEEN 1 AND 256),
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (tenant_id,id),
 FOREIGN KEY (tenant_id,order_id) REFERENCES bonuspro.order_contexts(tenant_id,order_id),
 FOREIGN KEY (tenant_id,old_counterparty_id,old_wallet_id)
   REFERENCES bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id),
 FOREIGN KEY (tenant_id,new_counterparty_id,new_wallet_id)
   REFERENCES bonuspro.customer_wallets(tenant_id,counterparty_id,wallet_id),
 FOREIGN KEY (tenant_id,rule_version) REFERENCES bonuspro.rule_versions(tenant_id,version),
 CHECK (old_counterparty_id<>new_counterparty_id AND old_wallet_id<>new_wallet_id)
);
CREATE INDEX customer_rebindings_order ON bonuspro.order_customer_rebindings(tenant_id,order_id,created_at,id);
ALTER TABLE bonuspro.order_customer_rebindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bonuspro.order_customer_rebindings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bonuspro.order_customer_rebindings
 USING (tenant_id=bonuspro.current_tenant()) WITH CHECK (tenant_id=bonuspro.current_tenant());
GRANT SELECT ON bonuspro.order_customer_rebindings TO bonuspro_app;
CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON bonuspro.order_customer_rebindings
 FOR EACH ROW EXECUTE FUNCTION bonuspro.reject_mutation();

-- A reservation is history even if released; zero-point accruals and resolved
-- cases/issues also pin the original financial identity permanently.
CREATE FUNCTION bonuspro.order_has_financial_history(p_tenant uuid,p_order uuid)
RETURNS boolean LANGUAGE sql SET search_path=pg_catalog,bonuspro AS $$
 SELECT EXISTS(SELECT 1 FROM bonuspro.reservations WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.redemption_attempts WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=p_tenant AND order_id=p_order)
 OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=p_tenant AND order_id=p_order);
$$;
REVOKE ALL ON FUNCTION bonuspro.order_has_financial_history(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION bonuspro.guard_order_context_binding()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,bonuspro AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.tenant_id IS DISTINCT FROM bonuspro.current_tenant() THEN
   RAISE EXCEPTION 'tenant context does not match order' USING ERRCODE='42501';
  END IF;
  IF NEW.binding_revision IS NULL THEN
   RAISE EXCEPTION 'new order context requires saved revision' USING ERRCODE='23514';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text||'/order/'||NEW.order_id::text,0));
  IF EXISTS(SELECT 1 FROM bonuspro.reservations
            WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id AND wallet_id<>NEW.wallet_id) THEN
   RAISE EXCEPTION 'order reservation belongs to another wallet' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'order context history is immutable' USING ERRCODE='23514';
 END IF;
 -- Ordinary runtime sessions retain no UPDATE privilege. Only the table owner,
 -- executing the narrow definer function below, may change this one pair.
 IF current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class
                   WHERE oid='bonuspro.order_contexts'::regclass) THEN
  RAISE EXCEPTION 'order context update is not permitted' USING ERRCODE='42501';
 END IF;
 IF ROW(NEW.tenant_id,NEW.order_id,NEW.rule_version,NEW.initial_source_hash,NEW.enrolled_by,NEW.created_at)
 IS DISTINCT FROM
    ROW(OLD.tenant_id,OLD.order_id,OLD.rule_version,OLD.initial_source_hash,OLD.enrolled_by,OLD.created_at) THEN
  RAISE EXCEPTION 'order context enrollment is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(OLD.tenant_id::text||'/order/'||OLD.order_id::text,0));
 IF bonuspro.order_has_financial_history(OLD.tenant_id,OLD.order_id) THEN
  RAISE EXCEPTION 'order customer has financial history' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION bonuspro.guard_order_context_binding() FROM PUBLIC;
DROP TRIGGER immutable_record ON bonuspro.order_contexts;
CREATE TRIGGER immutable_record BEFORE INSERT OR UPDATE OR DELETE ON bonuspro.order_contexts
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_order_context_binding();

-- Serialize even direct reservation inserts with a rebind, and reject a
-- wallet argument captured before the saved customer was corrected.
CREATE FUNCTION bonuspro.guard_reservation_order_binding()
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
CREATE TRIGGER reservation_order_binding BEFORE INSERT ON bonuspro.reservations
 FOR EACH ROW EXECUTE FUNCTION bonuspro.guard_reservation_order_binding();

CREATE FUNCTION bonuspro.rebind_order_customer(
 p_order uuid,p_customer uuid,p_source_hash text,p_revision text,p_actor uuid)
RETURNS bonuspro.order_contexts
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,bonuspro AS $$
DECLARE
 selected_tenant uuid;
 context bonuspro.order_contexts;
 destination_wallet uuid;
 observed timestamp;
 bound timestamp;
 same_customer boolean;
 revision_pattern constant text:='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?$';
BEGIN
 selected_tenant:=bonuspro.current_tenant();
 IF selected_tenant IS NULL THEN
  RAISE EXCEPTION 'tenant context is required' USING ERRCODE='42501';
 END IF;
 IF p_order IS NULL OR p_customer IS NULL OR p_actor IS NULL OR p_source_hash IS NULL
 OR p_source_hash !~ '^[0-9a-f]{64}$' OR p_revision IS NULL
 OR length(btrim(p_revision)) NOT BETWEEN 1 AND 256 THEN
  RAISE EXCEPTION 'invalid customer rebind evidence' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('installation/'||selected_tenant::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended(selected_tenant::text||'/settings',0));
 PERFORM pg_advisory_xact_lock(hashtextextended(selected_tenant::text||'/order/'||p_order::text,0));
 SELECT * INTO context FROM bonuspro.order_contexts
  WHERE tenant_id=selected_tenant AND order_id=p_order FOR UPDATE;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'order context is missing' USING ERRCODE='23503';
 END IF;
 SELECT wallet_id INTO destination_wallet FROM bonuspro.customer_wallets
  WHERE tenant_id=selected_tenant AND counterparty_id=p_customer;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'customer wallet is missing' USING ERRCODE='23503';
 END IF;
 same_customer:=context.counterparty_id=p_customer;
 PERFORM id FROM bonuspro.wallets
  WHERE tenant_id=selected_tenant AND id IN (context.wallet_id,destination_wallet)
  ORDER BY id FOR UPDATE;
 IF bonuspro.order_has_financial_history(selected_tenant,p_order) THEN
  IF same_customer THEN RETURN context; END IF;
  RAISE EXCEPTION 'order customer has financial history' USING ERRCODE='23514';
 END IF;
 -- Opaque internal revisions may be read, but cannot authorize a buyer change
 -- or overwrite a known provider timestamp. Calendar errors always fail closed.
 IF p_revision ~ revision_pattern THEN
  BEGIN
   observed:=p_revision::timestamp;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
   RAISE EXCEPTION 'saved customer revision is invalid' USING ERRCODE='23514';
  END;
 END IF;
 IF context.binding_revision ~ revision_pattern THEN
  BEGIN
   bound:=context.binding_revision::timestamp;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
   RAISE EXCEPTION 'bound customer revision is invalid' USING ERRCODE='23514';
  END;
 END IF;
 IF observed IS NULL THEN
  IF same_customer THEN RETURN context; END IF;
  RAISE EXCEPTION 'saved customer revision cannot be ordered' USING ERRCODE='23514';
 END IF;
 IF NOT same_customer AND context.binding_revision IS NOT NULL AND bound IS NULL THEN
  RAISE EXCEPTION 'bound customer revision cannot be ordered' USING ERRCODE='23514';
 END IF;
 IF bound IS NOT NULL THEN
  IF observed<bound OR (NOT same_customer AND observed=bound) THEN
   RAISE EXCEPTION 'saved customer revision is stale' USING ERRCODE='23514';
  END IF;
  IF same_customer AND observed=bound THEN RETURN context; END IF;
 END IF;
 IF NOT same_customer THEN
  INSERT INTO bonuspro.order_customer_rebindings
   (tenant_id,order_id,old_counterparty_id,new_counterparty_id,old_wallet_id,new_wallet_id,
    rule_version,source_hash,source_revision,actor_id)
  VALUES (selected_tenant,p_order,context.counterparty_id,p_customer,context.wallet_id,destination_wallet,
          context.rule_version,p_source_hash,p_revision,p_actor);
 END IF;
 UPDATE bonuspro.order_contexts
  SET counterparty_id=p_customer,wallet_id=destination_wallet,binding_revision=p_revision
  WHERE tenant_id=selected_tenant AND order_id=p_order RETURNING * INTO context;
 RETURN context;
END $$;
REVOKE ALL ON FUNCTION bonuspro.rebind_order_customer(uuid,uuid,text,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bonuspro.rebind_order_customer(uuid,uuid,text,text,uuid) TO bonuspro_app;
