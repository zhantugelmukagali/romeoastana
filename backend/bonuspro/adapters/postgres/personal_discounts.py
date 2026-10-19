"""Versioned personal discounts. Settings never create a wallet or move points.

The caller authenticates an administrator and validates the counterparty against
the connected provider before an assignment write. Financial callers resolve
these versions under the tenant settings lock and pin them in an immutable plan.
"""
from bonuspro.adapters.postgres.ledger import (
    Conflict, fingerprint, text_value, uuid_value,
)
from bonuspro.domain.finance import natural

POLICIES = frozenset(("skip", "best", "sequential"))


class PersonalDiscounts:
    def __init__(self, ledger):
        self.ledger = ledger

    @staticmethod
    def _policy(row):
        if row is None:
            return dict(version=0, enabled=False, external_policy="skip",
                        allow_bonus_stack=False)
        return dict(version=row["version"], enabled=row["enabled"],
                    external_policy=row["external_policy"],
                    allow_bonus_stack=row["allow_bonus_stack"],
                    actor_id=str(row["actor_id"]), reason=row["reason"],
                    created_at=row["created_at"].isoformat())

    @staticmethod
    def _assignment(row, counterparty):
        if row is None:
            return dict(counterparty_id=counterparty, version=0, rate_bps=None)
        return dict(counterparty_id=str(row["counterparty_id"]),
                    version=row["version"], rate_bps=row["rate_bps"],
                    actor_id=str(row["actor_id"]), reason=row["reason"],
                    created_at=row["created_at"].isoformat())

    @staticmethod
    def _current_policy(conn, tenant):
        return conn.execute(
            "SELECT * FROM bonuspro.personal_discount_policy_versions "
            "WHERE tenant_id=%s ORDER BY version DESC LIMIT 1", (tenant,),
        ).fetchone()

    @staticmethod
    def _current_assignment(conn, tenant, counterparty):
        return conn.execute(
            "SELECT * FROM bonuspro.customer_personal_discount_versions "
            "WHERE tenant_id=%s AND counterparty_id=%s "
            "ORDER BY version DESC LIMIT 1", (tenant, counterparty),
        ).fetchone()

    @classmethod
    def policy_in(cls, conn, tenant_id):
        tenant = uuid_value(tenant_id)
        return cls._policy(cls._current_policy(conn, tenant))

    @classmethod
    def assignment_in(cls, conn, tenant_id, counterparty_id):
        tenant, customer = map(uuid_value, (tenant_id, counterparty_id))
        return cls._assignment(cls._current_assignment(conn, tenant, customer), customer)

    @classmethod
    def resolve_in(cls, conn, tenant_id, counterparty_id):
        """Read-only resolution; financial callers already hold settings/order."""
        tenant, counterparty = map(uuid_value, (tenant_id, counterparty_id))
        policy = cls._policy(cls._current_policy(conn, tenant))
        assignment = cls._assignment(
            cls._current_assignment(conn, tenant, counterparty), counterparty,
        )
        return dict(policy=policy, assignment=assignment,
                    effective_rate_bps=(assignment["rate_bps"] or 0)
                    if policy["enabled"] else 0)

    def policy(self, tenant_id):
        tenant = uuid_value(tenant_id)
        with self.ledger.transaction(tenant) as conn:
            return self._policy(self._current_policy(conn, tenant))

    def assignment(self, tenant_id, counterparty_id):
        tenant, counterparty = map(uuid_value, (tenant_id, counterparty_id))
        with self.ledger.transaction(tenant) as conn:
            # Keep the policy and assignment from one serialized configuration
            # view, rather than combining two READ COMMITTED snapshots.
            self.ledger._key_lock(conn, tenant, "settings")
            result = self.resolve_in(conn, tenant, counterparty)
            return dict(**result["assignment"],
                        effective_rate_bps=result["effective_rate_bps"],
                        enabled=result["policy"]["enabled"])

    def assignments(self, tenant_id, counterparty_ids):
        tenant = uuid_value(tenant_id)
        if not isinstance(counterparty_ids, (list, tuple)) or len(counterparty_ids) > 100:
            raise ValueError("invalid customer batch")
        customers = tuple(dict.fromkeys(uuid_value(c) for c in counterparty_ids))
        if not customers:
            return {}
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn, tenant, "settings")
            policy = self._policy(self._current_policy(conn, tenant))
            rows = conn.execute(
                "SELECT DISTINCT ON (counterparty_id) * "
                "FROM bonuspro.customer_personal_discount_versions "
                "WHERE tenant_id=%s AND counterparty_id=ANY(%s::uuid[]) "
                "ORDER BY counterparty_id,version DESC", (tenant, list(customers)),
            ).fetchall()
            current = {str(row["counterparty_id"]): row for row in rows}
            result = {}
            for customer in customers:
                item = self._assignment(current.get(customer), customer)
                result[customer] = dict(
                    **item, enabled=policy["enabled"],
                    effective_rate_bps=(item["rate_bps"] or 0)
                    if policy["enabled"] else 0,
                )
            return result

    @staticmethod
    def _command(tenant_id, expected_version, request_id, actor_id, reason):
        tenant, request, actor = map(uuid_value, (tenant_id, request_id, actor_id))
        natural(expected_version, "expected_version", 2147483646)
        reason = text_value(reason, "reason", 500).strip()
        return tenant, request, actor, reason

    def save_policy(self, tenant_id, *, enabled, external_policy, allow_bonus_stack,
                    expected_version, request_id, actor_id, reason):
        tenant, request, actor, reason = self._command(
            tenant_id, expected_version, request_id, actor_id, reason,
        )
        if type(enabled) is not bool or type(allow_bonus_stack) is not bool:
            raise ValueError("invalid personal discount policy")
        if not isinstance(external_policy, str) or external_policy not in POLICIES:
            raise ValueError("invalid external discount policy")
        digest = fingerprint(dict(
            enabled=enabled, external_policy=external_policy,
            allow_bonus_stack=allow_bonus_stack,
            expected_version=expected_version, actor=actor, reason=reason,
        ))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn, tenant, "settings")
            previous = conn.execute(
                "SELECT * FROM bonuspro.personal_discount_policy_versions "
                "WHERE tenant_id=%s AND request_id=%s", (tenant, request),
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise Conflict("request ID reused with different personal discount settings")
                return self._policy(previous)
            current = self._current_policy(conn, tenant)
            version = current["version"] if current else 0
            if version != expected_version:
                raise Conflict("personal discount settings changed; reload before saving")
            row = conn.execute(
                "INSERT INTO bonuspro.personal_discount_policy_versions "
                "(tenant_id,version,enabled,external_policy,allow_bonus_stack,"
                "request_id,request_hash,actor_id,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant, version + 1, enabled, external_policy, allow_bonus_stack,
                 request, digest, actor, reason),
            ).fetchone()
            return self._policy(row)

    def save_assignment(self, tenant_id, counterparty_id, *, rate_bps,
                        expected_version, request_id, actor_id, reason):
        tenant, request, actor, reason = self._command(
            tenant_id, expected_version, request_id, actor_id, reason,
        )
        customer = uuid_value(counterparty_id)
        if rate_bps is not None:
            natural(rate_bps, "rate_bps", 10000)
        digest = fingerprint(dict(
            counterparty=customer, rate_bps=rate_bps,
            expected_version=expected_version, actor=actor, reason=reason,
        ))
        with self.ledger.transaction(tenant) as conn:
            self.ledger._key_lock(conn, tenant, "settings")
            previous = conn.execute(
                "SELECT * FROM bonuspro.customer_personal_discount_versions "
                "WHERE tenant_id=%s AND request_id=%s", (tenant, request),
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise Conflict("request ID reused with a different personal discount")
                return self._assignment(previous, customer)
            current = self._current_assignment(conn, tenant, customer)
            version = current["version"] if current else 0
            if version != expected_version:
                raise Conflict("customer personal discount changed; reload before saving")
            row = conn.execute(
                "INSERT INTO bonuspro.customer_personal_discount_versions "
                "(tenant_id,counterparty_id,version,rate_bps,request_id,"
                "request_hash,actor_id,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (tenant, customer, version + 1, rate_bps, request, digest, actor, reason),
            ).fetchone()
            return self._assignment(row, customer)

    def policy_history(self, tenant_id, before=None):
        tenant = uuid_value(tenant_id)
        if before is not None:
            natural(before, "before", 2147483647)
        with self.ledger.transaction(tenant) as conn:
            rows = conn.execute(
                "SELECT * FROM bonuspro.personal_discount_policy_versions "
                "WHERE tenant_id=%s AND (%s::integer IS NULL OR version<%s) "
                "ORDER BY version DESC LIMIT 51", (tenant, before, before),
            ).fetchall()
            return dict(items=[self._policy(row) for row in rows[:50]],
                        next_cursor=rows[49]["version"] if len(rows) > 50 else None)

    def assignment_history(self, tenant_id, counterparty_id, before=None):
        tenant, customer = map(uuid_value, (tenant_id, counterparty_id))
        if before is not None:
            natural(before, "before", 2147483647)
        with self.ledger.transaction(tenant) as conn:
            rows = conn.execute(
                "SELECT * FROM bonuspro.customer_personal_discount_versions "
                "WHERE tenant_id=%s AND counterparty_id=%s "
                "AND (%s::integer IS NULL OR version<%s) "
                "ORDER BY version DESC LIMIT 51", (tenant, customer, before, before),
            ).fetchall()
            return dict(items=[self._assignment(row, customer) for row in rows[:50]],
                        next_cursor=rows[49]["version"] if len(rows) > 50 else None)
