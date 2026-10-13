"""Transactional wallet persistence. No HTTP endpoints or provider proof validation.

Only a trusted application layer may call this adapter after authorization and
document reconciliation. VerifiedDiscount is an internal contract, not a public
API request model. The saved-order adapter and fulfillment reconciler produce it after remote reads.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from bonuspro.domain.finance import Rules, natural, spend_limit

MAX_POINTS = 90_000_000_000_000


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


def fingerprint(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text_value(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def uuid_value(value: str) -> str:
    return str(UUID(str(value)))


def digest_value(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid quote digest")
    return value


@dataclass(frozen=True)
class VerifiedDiscount:
    order_id: str
    quote_hash: str
    discount_minor: int
    revision: str


class Ledger:
    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def transaction(self, tenant_id: str):
        tenant = uuid_value(tenant_id)
        with closing(psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)) as conn:
            with conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                role = conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
                if role["rolsuper"] or role["rolbypassrls"]:
                    raise RuntimeError("application connection must not bypass row security")
                conn.execute("SELECT set_config('bonuspro.tenant_id', %s, true)", (tenant,))
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute("SET LOCAL statement_timeout = '10s'")
                yield conn

    @staticmethod
    def _key_lock(conn, tenant: str, key: str):
        # Serializes duplicate commands even when they name different wallets.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant + "/" + key,))

    @staticmethod
    def _wallet_lock(conn, tenant: str, wallet: str):
        row = conn.execute(
            "SELECT id FROM bonuspro.wallets WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant, wallet),
        ).fetchone()
        if row is None:
            raise NotFound("wallet not found")

    @staticmethod
    def _snapshot(conn, tenant: str, wallet: str) -> dict:
        balance = int(conn.execute(
            "SELECT COALESCE(sum(amount),0) AS amount FROM bonuspro.ledger_postings "
            "WHERE tenant_id=%s AND wallet_id=%s AND account='customer'", (tenant, wallet),
        ).fetchone()["amount"])
        reserved = int(conn.execute(
            "SELECT COALESCE(sum(points),0) AS amount FROM bonuspro.reservations "
            "WHERE tenant_id=%s AND wallet_id=%s AND state IN ('held','verifying')", (tenant, wallet),
        ).fetchone()["amount"])
        return {"balance": balance, "reserved": reserved, "available": max(0, balance-reserved)}

    def create_wallet(self, tenant_id: str, wallet_id: str):
        tenant, wallet = uuid_value(tenant_id), uuid_value(wallet_id)
        with self.transaction(tenant) as conn:
            conn.execute(
                "INSERT INTO bonuspro.wallets(tenant_id,id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant, wallet),
            )

    def snapshot(self, tenant_id: str, wallet_id: str) -> dict:
        tenant, wallet = uuid_value(tenant_id), uuid_value(wallet_id)
        with self.transaction(tenant) as conn:
            self._wallet_lock(conn, tenant, wallet)
            return self._snapshot(conn, tenant, wallet)

    @staticmethod
    def _post(conn, tenant, wallet, operation_key, request_hash, delta, kind, actor, reason, reservation=None, credit_ref=None):
        transaction_id = uuid4()
        conn.execute(
            "INSERT INTO bonuspro.ledger_transactions "
            "(tenant_id,id,wallet_id,operation_key,request_hash,kind,delta,reservation_id,actor_id,reason,credit_ref_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant, transaction_id, wallet, operation_key, request_hash, kind, delta, reservation, actor, reason, credit_ref),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO bonuspro.ledger_postings "
                "(tenant_id,transaction_id,wallet_id,account,amount) VALUES (%s,%s,%s,%s,%s)",
                [(tenant, transaction_id, wallet, "customer", delta),
                 (tenant, transaction_id, wallet, "system", -delta)],
            )
        return str(transaction_id)

    def credit(self, tenant_id, wallet_id, *, points, key, actor_id, reason):
        tenant, wallet, actor = map(uuid_value, (tenant_id, wallet_id, actor_id))
        natural(points, "points", MAX_POINTS)
        if points == 0:
            raise ValueError("credit must be positive")
        key = "credit/" + text_value(key, "key", 128)
        text_value(reason, "reason", 500)
        request_hash = fingerprint(dict(wallet=wallet, points=points, actor=actor, reason=reason))
        with self.transaction(tenant) as conn:
            self._key_lock(conn, tenant, key)
            existing = conn.execute(
                "SELECT id,request_hash FROM bonuspro.ledger_transactions WHERE tenant_id=%s AND operation_key=%s",
                (tenant, key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused with different data")
                return str(existing["id"])
            self._wallet_lock(conn, tenant, wallet)
            if self._snapshot(conn, tenant, wallet)["balance"] + points > MAX_POINTS:
                raise Conflict("wallet balance limit exceeded")
            return self._post(conn, tenant, wallet, key, request_hash, points, "credit", actor, reason)

    def reserve(self, tenant_id, wallet_id, *, order_id, points, eligible_minor,
                rules: Rules, quote_hash, key, actor_id):
        tenant, wallet, order, actor = map(uuid_value, (tenant_id, wallet_id, order_id, actor_id))
        natural(points, "points", MAX_POINTS)
        natural(eligible_minor, "eligible_minor")
        if points == 0:
            raise ValueError("reservation must be positive")
        digest_value(quote_hash)
        text_value(key, "key", 128)
        request_hash = fingerprint(dict(
            wallet=wallet, order=order, points=points, eligible_minor=eligible_minor,
            earn_bps=rules.earn_bps, spend_bps=rules.spend_bps, quote_hash=quote_hash, actor=actor,
        ))
        with self.transaction(tenant) as conn:
            self._key_lock(conn, tenant, "reserve/" + key)
            existing = conn.execute(
                "SELECT id,request_hash FROM bonuspro.reservations WHERE tenant_id=%s AND operation_key=%s",
                (tenant, key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused with different data")
                return str(existing["id"])
            self._wallet_lock(conn, tenant, wallet)
            if conn.execute("SELECT 1 FROM bonuspro.return_cases WHERE tenant_id=%s AND wallet_id=%s "
                            "AND state='pending' LIMIT 1", (tenant, wallet)).fetchone():
                raise Conflict("wallet requires administrative return review")
            if conn.execute("SELECT 1 FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND wallet_id=%s LIMIT 1",
                            (tenant,wallet)).fetchone():
                raise Conflict("wallet requires reconciliation review")
            available = self._snapshot(conn, tenant, wallet)["available"]
            if points > spend_limit(eligible_minor, available, rules):
                raise Conflict("insufficient balance or order limit exceeded")
            rid = uuid4()
            try:
                conn.execute(
                    "INSERT INTO bonuspro.reservations "
                    "(tenant_id,id,wallet_id,operation_key,request_hash,order_id,quote_hash,points,actor_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tenant, rid, wallet, key, request_hash, order, quote_hash, points, actor),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise Conflict("order already has a reservation") from exc
            return str(rid)

    def _locked_reservation(self, conn, tenant, rid):
        row = conn.execute(
            "SELECT * FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s", (tenant, rid),
        ).fetchone()
        if row is None:
            raise NotFound("reservation not found")
        # Always wallet before reservation: consistent lock ordering across operations.
        self._wallet_lock(conn, tenant, str(row["wallet_id"]))
        return conn.execute(
            "SELECT * FROM bonuspro.reservations WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant, rid),
        ).fetchone()

    def begin_verification(self, tenant_id, reservation_id):
        tenant, rid = uuid_value(tenant_id), uuid_value(reservation_id)
        with self.transaction(tenant) as conn:
            row = self._locked_reservation(conn, tenant, rid)
            if row["state"] in ("verifying", "committed"):
                return
            if row["state"] != "held":
                raise Conflict("reservation is not active")
            conn.execute("UPDATE bonuspro.reservations SET state='verifying' WHERE tenant_id=%s AND id=%s",
                         (tenant, rid))

    def release_unapplied(self, tenant_id, reservation_id):
        tenant, rid = uuid_value(tenant_id), uuid_value(reservation_id)
        with self.transaction(tenant) as conn:
            row = self._locked_reservation(conn, tenant, rid)
            if row["state"] == "released":
                return
            if row["state"] != "held":
                raise Conflict("external result must be reconciled; reservation cannot be released")
            conn.execute("UPDATE bonuspro.reservations SET state='released' WHERE tenant_id=%s AND id=%s",
                         (tenant, rid))

    def confirm_discount(self, tenant_id, reservation_id, *, proof: VerifiedDiscount):
        with self.transaction(tenant_id) as conn:
            return self.confirm_discount_in(conn,tenant_id,reservation_id,proof=proof)

    def confirm_discount_in(self, conn, tenant_id, reservation_id, *, proof: VerifiedDiscount):
        tenant, rid = uuid_value(tenant_id), uuid_value(reservation_id)
        order = uuid_value(proof.order_id)
        digest_value(proof.quote_hash)
        natural(proof.discount_minor, "discount_minor")
        text_value(proof.revision, "revision", 256)
        row = self._locked_reservation(conn, tenant, rid)
        if (order != str(row["order_id"]) or proof.quote_hash != row["quote_hash"]
                or proof.discount_minor != row["points"] * 100):
            raise Conflict("saved document does not match reservation")
        if row["state"] == "committed":
            tx = conn.execute(
                "SELECT id FROM bonuspro.ledger_transactions WHERE tenant_id=%s AND reservation_id=%s",
                (tenant, rid),
            ).fetchone()
            return str(tx["id"])
        if row["state"] != "verifying":
            raise Conflict("reservation is not awaiting verification")
        conn.execute(
            "UPDATE bonuspro.reservations SET state='committed',proof_revision=%s WHERE tenant_id=%s AND id=%s",
            (proof.revision, tenant, rid),
        )
        return self._post(conn, tenant, str(row["wallet_id"]), "redemption/" + rid,
                          row["request_hash"], -row["points"], "redemption", str(row["actor_id"]),
                          "Saved bonus discount verified by provider adapter", rid)
