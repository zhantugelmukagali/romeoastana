"""Verified presale reversal of a saved bonus/personal application.

The browser only transports a frozen inverse. Refunds require a fresh, stable
provider graph proving the complete original basket and removal of our marker.
"""
from collections import Counter
from decimal import Decimal
import json
import time

from psycopg.types.json import Jsonb
from bonuspro.adapters.postgres.ledger import (
    Conflict, NotFound, MAX_POINTS, uuid_value, digest_value, fingerprint,
)
from bonuspro.adapters.postgres.automation import Automation


REASONS=frozenset((
    "undo_restore_original","undo_sale_locked","undo_operation_changed",
    "undo_not_confirmed","undo_generation_changed","undo_not_representable",
    "undo_balance_limit",
))


class PresaleUndoConflict(Conflict):
    def __init__(self,reason):
        self.reason=reason if reason in REASONS else "undo_operation_changed"
        super().__init__(self.reason)


def original_financial_matches(actual,original):
    """Retain row multiplicity; a newly saved row ID is not financial evidence."""
    if not isinstance(actual,dict) or not isinstance(original,dict):
        return False
    if {k:v for k,v in actual.items() if k!="lines"}!={k:v for k,v in original.items() if k!="lines"}:
        return False
    def records(financial):
        rows=financial.get("lines")
        if not isinstance(rows,list) or not rows or len(rows)>1000:
            return None
        ids=[row.get("id") for row in rows if isinstance(row,dict)]
        if len(ids)!=len(rows) or len(set(ids))!=len(rows):
            return None
        return Counter(json.dumps({k:v for k,v in row.items() if k!="id"},
                     sort_keys=True,separators=(",",":")) for row in rows)
    left,right=records(actual),records(original)
    return left is not None and left==right


def _wire(value):
    value=Decimal(str(value))
    if not value.is_finite():
        raise PresaleUndoConflict("undo_not_representable")
    if value==value.to_integral_value():
        return int(value)
    result=float(value)
    if Decimal(str(result))!=value:
        raise PresaleUndoConflict("undo_not_representable")
    return result


def _can_recreate(plan,row):
    manifest=plan.get("reconstruction")
    if not isinstance(manifest,dict) or type(manifest.get("version")) is not int or manifest["version"]!=1:
        return False
    rows=manifest.get("rows")
    if not isinstance(rows,list) or len(rows)>1000:
        return False
    matches=[entry for entry in rows if isinstance(entry,dict) and entry.get("id")==row["id"]]
    return (len(matches)==1 and matches[0].get("recreatable") is True
            and matches[0].get("fields")=={key:value for key,value in row.items() if key!="id"})


def inverse_state(saved,operation,*,restore_original):
    """Absolute original rows; replacing current edits requires explicit consent."""
    original=operation["plan"]["before"]
    applied=operation["plan"]["after"]
    if original_financial_matches(saved.financial(),original):
        # Staff may have manually restored deleted/unsupported rows with new
        # IDs. Preserve those rows and all unfrozen fields; clear only our marker.
        return {"positions":[{"id":line.id} for line in saved.lines],
                "attributes":[{"id":str(operation["marker_id"]),"value":None}]}
    if not restore_original and saved.financial()!=applied:
        raise PresaleUndoConflict("undo_restore_original")
    current={line.id:line for line in saved.lines}
    positions=[]
    for row in original["lines"]:
        line=current.get(row["id"])
        if line is None:
            # A deleted packed/bundle row cannot be recreated from this plan:
            # package ratios and bundle-specific details were not frozen.
            if not restore_original or row["pack"] or row["kind"]=="bundle" or not _can_recreate(operation["plan"],row):
                raise PresaleUndoConflict("undo_not_representable")
            update={"assortment":{"meta":{
                "href":"https://api.moysklad.ru/api/remap/1.2/entity/"+row["kind"]+"/"+row["assortment"],
                "type":row["kind"],"mediaType":"application/json"}}}
        else:
            if (line.kind,line.assortment,line.pack)!=(row["kind"],row["assortment"],row["pack"]):
                raise PresaleUndoConflict("undo_not_representable")
            update={"id":row["id"]}
        update.update(quantity=_wire(row["quantity"]),price=_wire(row["price"]),
                      discount=_wire(Decimal(row["discount_bps"])/100),
                      vat=_wire(row["vat"]),vatEnabled=row["vat_enabled"])
        positions.append(update)
    return {"positions":positions,"attributes":[{"id":str(operation["marker_id"]),"value":None}]}


class PresaleUndo:
    def __init__(self,ledger):
        self.ledger=ledger

    @staticmethod
    def public(row,*,completed=False,**extra):
        result=dict(id=str(row["id"]),attempt_id=str(row["operation_id"]),
                    state="completed" if completed else "prepared",
                    verified=completed,recoverable=False)
        result.update(extra)
        return result

    @staticmethod
    def _completion(conn,tenant,identity):
        return conn.execute("SELECT * FROM bonuspro.presale_undo_completions "
                            "WHERE tenant_id=%s AND request_id=%s",(tenant,identity)).fetchone()

    def pending(self,tenant,order):
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT u.* FROM bonuspro.presale_undo_requests u "
                "LEFT JOIN bonuspro.presale_undo_completions c "
                "ON c.tenant_id=u.tenant_id AND c.request_id=u.id "
                "WHERE u.tenant_id=%s AND u.order_id=%s AND c.request_id IS NULL",
                (tenant,order)).fetchone()
            return self.public(row) if row else None

    @staticmethod
    def _operation(conn,tenant,order,identity):
        personal=conn.execute("SELECT * FROM bonuspro.personal_discount_applications "
            "WHERE tenant_id=%s AND id=%s AND order_id=%s",(tenant,identity,order)).fetchone()
        if personal:
            return dict(personal,confirmed=personal["state"]=="confirmed",
                        reservation_id=personal["redemption_id"],personal_application_id=personal["id"])
        row=conn.execute("SELECT a.*,r.wallet_id,r.points,r.state AS reservation_state "
            "FROM bonuspro.redemption_attempts a JOIN bonuspro.reservations r "
            "ON r.tenant_id=a.tenant_id AND r.id=a.id "
            "WHERE a.tenant_id=%s AND a.id=%s AND a.order_id=%s",(tenant,identity,order)).fetchone()
        if not row:
            raise NotFound("application not found")
        return dict(row,confirmed=row["reservation_state"]=="committed",
                    reservation_id=row["id"],personal_application_id=None)

    @staticmethod
    def _no_sale_history(conn,tenant,order):
        return not conn.execute("""
          SELECT 1 WHERE
           EXISTS(SELECT 1 FROM bonuspro.order_accruals WHERE tenant_id=%s AND order_id=%s)
           OR EXISTS(SELECT 1 FROM bonuspro.settlement_events WHERE tenant_id=%s AND order_id=%s)
           OR EXISTS(SELECT 1 FROM bonuspro.settlement_cases WHERE tenant_id=%s AND order_id=%s)
           OR EXISTS(SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND order_id=%s)
        """,(tenant,order,tenant,order,tenant,order,tenant,order)).fetchone()

    @staticmethod
    def _safe(proof,tenant,order):
        saved=proof.saved
        if (saved.snapshot.tenant_id!=tenant or saved.snapshot.order_id!=order):
            raise PresaleUndoConflict("undo_operation_changed")
        if (time.monotonic()>proof.deadline or not proof.presale_safe
            or saved.snapshot.cancelled or saved.snapshot.paid_minor or saved.snapshot.shipped_minor
            or proof.paid_minor or proof.shipped_minor or proof.returned):
            raise PresaleUndoConflict("undo_sale_locked")

    def _lock(self,conn,tenant,order,*,authorize=None,generation=None):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+tenant,))
        self.ledger._key_lock(conn,tenant,"settings")
        self.ledger._key_lock(conn,tenant,"order/"+order)
        active=conn.execute("SELECT state,generation FROM bonuspro.installations WHERE tenant_id=%s",
                            (tenant,)).fetchone()
        if (not active or active["state"]!="Activated"
            or (generation is not None and str(active["generation"])!=str(generation))):
            raise PresaleUndoConflict("undo_generation_changed")
        if authorize:
            authorize(conn)
        return active

    def _capacity(self,conn,tenant,operation):
        wallet=str(operation["wallet_id"])
        self.ledger._wallet_lock(conn,tenant,wallet)
        if self.ledger._snapshot(conn,tenant,wallet)["balance"]+operation["points"]>MAX_POINTS:
            raise PresaleUndoConflict("undo_balance_limit")

    @staticmethod
    def _same_identity(saved,operation):
        before=operation["plan"]["before"]
        actual=saved.financial()
        return all(actual[key]==before[key] for key in (
            "order_id","counterparty_id","organization_id","currency","vat_included","vat_enabled"))

    def hints(self,tenant,saved,result):
        if result.get("state")!="committed":
            return result
        with self.ledger.transaction(tenant) as conn:
            operation=self._operation(conn,tenant,saved.snapshot.order_id,result["id"])
            allowed=(operation["confirmed"] and not saved.snapshot.cancelled
                and not saved.snapshot.paid_minor and not saved.snapshot.shipped_minor
                and saved.discount_blocked_reason in (None,"order_has_marker")
                and self._no_sale_history(conn,tenant,saved.snapshot.order_id))
            if saved.snapshot.counterparty_id!=operation["plan"]["before"]["counterparty_id"]:
                return dict(result,undo_available=False,
                            original_buyer_id=operation["plan"]["before"]["counterparty_id"])
            if not self._same_identity(saved,operation):
                return dict(result,undo_available=False)
            if saved.marker not in (None,operation["marker"]):
                allowed=False
            return dict(result,undo_available=bool(allowed),
                        **({"undo_source_hash":saved.source_hash,
                            "undo_restore_required":not (
                                saved.marker==operation["marker"] and saved.financial()==operation["plan"]["after"])}
                           if allowed else {}))

    def prepare(self,tenant,order,proof,*,attempt_id,request_id,actor,
                expected_source,restore_original=False,authorize=None):
        tenant,order,attempt,identity,actor=map(uuid_value,(tenant,order,attempt_id,request_id,actor))
        digest_value(expected_source)
        if type(restore_original) is not bool:
            raise ValueError("explicit restore selection required")
        request_hash=fingerprint(dict(order=order,operation=attempt,actor=actor,
            source=expected_source,restore_original=restore_original))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn,tenant,"undo/request/"+identity)
            previous=conn.execute("SELECT * FROM bonuspro.presale_undo_requests "
                                 "WHERE tenant_id=%s AND id=%s",(tenant,identity)).fetchone()
            if previous:
                if previous["request_hash"]!=request_hash:
                    raise PresaleUndoConflict("undo_operation_changed")
                if authorize:authorize(conn)
                return dict(undo=self.public(previous,completed=bool(self._completion(conn,tenant,identity))),replayed=True)
            active=self._lock(conn,tenant,order,authorize=authorize)
            self._safe(proof,tenant,order)
            operation=self._operation(conn,tenant,order,attempt)
            if not operation["confirmed"]:
                raise PresaleUndoConflict("undo_not_confirmed")
            existing=conn.execute("SELECT * FROM bonuspro.presale_undo_requests "
                "WHERE tenant_id=%s AND operation_id=%s",(tenant,attempt)).fetchone()
            if existing:
                return dict(undo=self.public(existing,completed=bool(self._completion(conn,tenant,str(existing["id"])))),replayed=True)
            binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
            if not binding or str(binding["marker_id"])!=str(operation["marker_id"]):
                raise PresaleUndoConflict("undo_generation_changed")
            if not self._no_sale_history(conn,tenant,order):
                raise PresaleUndoConflict("undo_sale_locked")
            saved=proof.saved
            if saved.source_hash!=expected_source or not self._same_identity(saved,operation):
                raise PresaleUndoConflict("undo_operation_changed")
            if saved.marker not in (None,operation["marker"]):
                raise PresaleUndoConflict("undo_operation_changed")
            if not restore_original and saved.marker!=operation["marker"]:
                raise PresaleUndoConflict("undo_restore_original")
            self._capacity(conn,tenant,operation)
            update=inverse_state(saved,operation,restore_original=restore_original)
            plan=dict(before=operation["plan"]["after"],after=operation["plan"]["before"],
                      update_state=update,restore_original=restore_original)
            row=conn.execute("""
              INSERT INTO bonuspro.presale_undo_requests
              (tenant_id,id,operation_id,order_id,wallet_id,reservation_id,personal_application_id,
               points,actor_id,generation,request_hash,source_hash,source_revision,marker_id,marker,reason,plan)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """,(tenant,identity,attempt,order,operation["wallet_id"],operation["reservation_id"],
                 operation["personal_application_id"],operation["points"],actor,active["generation"],
                 request_hash,expected_source,saved.snapshot.revision,operation["marker_id"],
                 operation["marker"],"Отмена применения до оплаты и отгрузки",Jsonb(plan))).fetchone()
            self._safe(proof,tenant,order)
            return dict(undo=self.public(row),update_state=update,replayed=False)

    @staticmethod
    def stale_worker_proof_in(conn,tenant,saved):
        if not saved.marker:
            return False
        row=conn.execute("SELECT u.source_hash,c.proof_revision FROM bonuspro.presale_undo_requests u "
            "JOIN bonuspro.presale_undo_completions c ON c.tenant_id=u.tenant_id AND c.request_id=u.id "
            "WHERE u.tenant_id=%s AND u.order_id=%s AND u.marker=%s",
            (tenant,saved.snapshot.order_id,saved.marker)).fetchone()
        if not row:
            return False
        if saved.source_hash==row["source_hash"]:
            return True
        from bonuspro.adapters.moysklad.fulfillment import moment
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        try:
            return moment(saved.snapshot.revision)<=moment(row["proof_revision"])
        except InvalidDocument:
            # Opaque legacy revisions cannot be ordered; do not suppress a
            # newer or foreign marker merely because its text was used before.
            return False

    @staticmethod
    def older_than_completed_undo_in(conn,tenant,saved):
        row=conn.execute("SELECT c.proof_revision FROM bonuspro.presale_undo_requests u "
            "JOIN bonuspro.presale_undo_completions c ON c.tenant_id=u.tenant_id AND c.request_id=u.id "
            "WHERE u.tenant_id=%s AND u.order_id=%s ORDER BY c.completed_at DESC LIMIT 1",
            (tenant,saved.snapshot.order_id)).fetchone()
        if not row:
            return False
        from bonuspro.adapters.moysklad.fulfillment import moment
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        try:
            return moment(saved.snapshot.revision)<moment(row["proof_revision"])
        except InvalidDocument:
            # Opaque legacy proofs cannot authorize a new financial result
            # after a newer, timestamped completed reversal.
            raise Conflict("unorderable evidence after presale undo") from None

    def _request(self,conn,tenant,order,identity):
        row=conn.execute("SELECT * FROM bonuspro.presale_undo_requests "
                        "WHERE tenant_id=%s AND id=%s AND order_id=%s",(tenant,identity,order)).fetchone()
        if not row:
            raise NotFound("undo not found")
        return row

    def _pending_state(self,conn,tenant,order,row,proof):
        self._safe(proof,tenant,order)
        from bonuspro.adapters.moysklad.fulfillment import moment
        from bonuspro.adapters.moysklad.remap import InvalidDocument
        try:
            current=moment(proof.saved.snapshot.revision)
            prepared=moment(row["source_revision"])
        except InvalidDocument:
            raise PresaleUndoConflict("undo_operation_changed") from None
        if current<prepared or (current==prepared and proof.saved.source_hash!=row["source_hash"]):
            raise PresaleUndoConflict("undo_operation_changed")
        binding=conn.execute("SELECT marker_id FROM bonuspro.widget_bindings WHERE tenant_id=%s",(tenant,)).fetchone()
        if not binding or str(binding["marker_id"])!=str(row["marker_id"]):
            raise PresaleUndoConflict("undo_generation_changed")
        operation=self._operation(conn,tenant,order,str(row["operation_id"]))
        if not operation["confirmed"]:
            raise PresaleUndoConflict("undo_not_confirmed")
        if not self._no_sale_history(conn,tenant,order):
            raise PresaleUndoConflict("undo_sale_locked")
        if not self._same_identity(proof.saved,operation) or proof.saved.marker not in (None,row["marker"]):
            return operation,False
        return operation,True

    def verify(self,tenant,order,proof,*,undo_id,actor,authorize=None):
        tenant,order,identity,actor=map(uuid_value,(tenant,order,undo_id,actor))
        with self.ledger.transaction(tenant) as conn:
            row=self._request(conn,tenant,order,identity)
            active=self._lock(conn,tenant,order,authorize=authorize)
            if self._completion(conn,tenant,identity):
                return dict(undo=self.public(row,completed=True),replayed=True)
            operation,same=self._pending_state(conn,tenant,order,row,proof)
            saved=proof.saved
            if not same:
                if saved.snapshot.counterparty_id!=operation["plan"]["before"]["counterparty_id"]:
                    return dict(undo=self.public(row,reason="undo_restore_original",
                                original_buyer_id=operation["plan"]["before"]["counterparty_id"]))
                return dict(undo=self.public(row,reason="undo_operation_changed"))
            if saved.marker is not None or not original_financial_matches(saved.financial(),row["plan"]["after"]):
                try:
                    inverse_state(saved,operation,restore_original=True)
                except PresaleUndoConflict as error:
                    return dict(undo=self.public(row,reason=error.reason))
                exact=(saved.marker==row["marker"] and saved.financial()==row["plan"]["before"])
                return dict(undo=self.public(row,recoverable=True,
                    reason="undo_restore_original" if not exact else "undo_not_confirmed",
                    restore_required=not exact,resume_source_hash=saved.source_hash))
            self._capacity(conn,tenant,row)
            transaction=None
            if row["points"]:
                transaction=self.ledger._post(conn,tenant,str(row["wallet_id"]),
                    "undo/"+identity,fingerprint(dict(undo=identity,evidence=proof.evidence_hash)),
                    row["points"],"presale_refund",actor,"Отмена списания до оплаты и отгрузки",
                    presale_undo=identity)
            conn.execute("INSERT INTO bonuspro.presale_undo_completions "
                "(tenant_id,request_id,actor_id,evidence_hash,proof_revision,transaction_id,generation) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (tenant,identity,actor,proof.evidence_hash,saved.snapshot.revision,transaction,active["generation"]))
            if row["reservation_id"]:
                conn.execute("UPDATE bonuspro.reservations SET state='reversed' WHERE tenant_id=%s AND id=%s",
                             (tenant,row["reservation_id"]))
            if row["personal_application_id"]:
                conn.execute("UPDATE bonuspro.personal_discount_applications SET state='voided',updated_at=clock_timestamp() "
                             "WHERE tenant_id=%s AND id=%s",(tenant,row["personal_application_id"]))
            Automation.hint_in(conn,tenant,order)
            self._safe(proof,tenant,order)
            return dict(undo=self.public(row,completed=True),replayed=False)

    def resume(self,tenant,order,proof,*,undo_id,expected_source,
               restore_original=False,authorize=None):
        tenant,order,identity=map(uuid_value,(tenant,order,undo_id))
        digest_value(expected_source)
        if type(restore_original) is not bool:
            raise ValueError("explicit restore selection required")
        with self.ledger.transaction(tenant) as conn:
            row=self._request(conn,tenant,order,identity)
            active=self._lock(conn,tenant,order,authorize=authorize)
            if self._completion(conn,tenant,identity):
                return dict(undo=self.public(row,completed=True),replayed=True)
            operation,same=self._pending_state(conn,tenant,order,row,proof)
            saved=proof.saved
            if not same or saved.source_hash!=expected_source:
                raise PresaleUndoConflict("undo_operation_changed")
            if saved.marker is None and original_financial_matches(saved.financial(),row["plan"]["after"]):
                # A lost completion ACK is resolved through verify, never another edit.
                raise PresaleUndoConflict("undo_not_confirmed")
            if not restore_original and not original_financial_matches(saved.financial(),row["plan"]["after"]) and (saved.marker!=row["marker"] or saved.financial()!=row["plan"]["before"]):
                raise PresaleUndoConflict("undo_restore_original")
            self._capacity(conn,tenant,row)
            # Restoring a later explicitly approved basket may need new row IDs.
            # It still targets exactly the immutable original financial document.
            update=inverse_state(saved,operation,restore_original=restore_original)
            self._safe(proof,tenant,order)
            return dict(undo=self.public(row),update_state=update,replayed=True)
