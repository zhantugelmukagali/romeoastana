"""Lifecycle storage uses a separate least-privileged database login."""
from contextlib import contextmanager
from hashlib import sha256
import json
import secrets
from uuid import UUID, uuid4
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from bonuspro.security import Secrets, AuthenticationFailed
from bonuspro.adapters.postgres.ledger import Ledger, Conflict
from bonuspro.adapters.moysklad.vendor import UserContext


class Installations:
    def __init__(self, app_dsn: str, lifecycle_dsn: str, config: Secrets):
        self.ledger = Ledger(app_dsn)
        self.lifecycle_dsn = lifecycle_dsn
        self.config = config

    @contextmanager
    def lifecycle(self):
        with psycopg.connect(self.lifecycle_dsn, row_factory=dict_row, connect_timeout=5) as conn:
            role = conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            if role["rolsuper"] or role["rolbypassrls"]:
                raise RuntimeError("lifecycle connection must not bypass row security")
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            yield conn

    def change(self, account_id: str, claims: dict, cause: str, credential: str | None = None):
        account = str(UUID(account_id))
        active = cause in ("Install","Resume")
        if cause not in ("Install","Resume","Suspend","Uninstall"):
            raise ValueError("unsupported lifecycle cause")
        if active and not credential:
            raise ValueError("activation credential is required")
        digest = sha256(json.dumps({"cause":cause,"credential":credential},
                                  sort_keys=True,separators=(",",":")).encode()).hexdigest()
        with self.lifecycle() as conn:
            consumed = conn.execute(
                "INSERT INTO bonuspro.vendor_replays(jti,expires_at) VALUES (%s,to_timestamp(%s)) "
                "ON CONFLICT DO NOTHING RETURNING jti", (claims["jti"],claims["exp"])
            ).fetchone()
            if not consumed:
                raise AuthenticationFailed("repeated authentication token")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("installation/"+account,))
            row = conn.execute("SELECT * FROM bonuspro.installations WHERE tenant_id=%s FOR UPDATE",
                               (account,)).fetchone()
            if row and (claims["iat"] < row["last_event_iat"] or
                        (claims["iat"] == row["last_event_iat"] and digest != row["last_event_digest"])):
                raise Conflict("out-of-order lifecycle event requires retry")
            if row and row["last_event_digest"] == digest:
                conn.execute("UPDATE bonuspro.installations SET last_event_iat=%s WHERE tenant_id=%s",
                             (claims["iat"], account))
                return row["state"]
            if not row and not active:
                return None  # Deletion of a missing installation is idempotent.
            if not row:
                conn.execute("INSERT INTO bonuspro.tenants(id) VALUES (%s) ON CONFLICT DO NOTHING", (account,))
            state = "SettingsRequired" if active else ("Suspended" if cause == "Suspend" else "Uninstalled")
            # Configuration activation is deliberately separate from installing credentials.
            # Until configuration is implemented, installation cannot report Activated.
            cipher = self.config.encrypt(account, credential) if active else None
            conn.execute(
                "INSERT INTO bonuspro.installations "
                "(tenant_id,app_id,state,token_ciphertext,generation,last_event_iat,last_event_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id) DO UPDATE SET "
                "state=EXCLUDED.state,token_ciphertext=EXCLUDED.token_ciphertext,generation=EXCLUDED.generation,"
                "last_event_iat=EXCLUDED.last_event_iat,last_event_digest=EXCLUDED.last_event_digest,updated_at=now()",
                (account,self.config.app_id,state,cipher,uuid4(),claims["iat"],digest))
            return state

    def create_session(self, context: UserContext) -> str:
        tenant, user = str(UUID(context.tenant_id)), str(UUID(context.user_id))
        if context.role not in ("admin","cashier","worker","individual"):
            raise AuthenticationFailed("unknown employee role")
        token = tenant + "." + secrets.token_urlsafe(32)
        hashed = sha256(token.encode()).hexdigest()
        with self.ledger.transaction(tenant) as conn:
            row = conn.execute(
                "SELECT generation FROM bonuspro.installations "
                "WHERE tenant_id=%s AND state IN ('SettingsRequired','Activated')", (tenant,)).fetchone()
            if row is None:
                raise AuthenticationFailed("installation is inactive")
            conn.execute(
                "INSERT INTO bonuspro.sessions(tenant_id,token_hash,generation,user_id,role,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,now()+interval '15 minutes')",
                (tenant,hashed,row["generation"],user,context.role))
        return token

    def authenticate(self, token: str) -> UserContext:
        try:
            if not isinstance(token,str) or not 60<=len(token)<=128:
                raise ValueError()
            tenant = str(UUID(token.split(".",1)[0]))
        except (ValueError,TypeError) as exc:
            raise AuthenticationFailed("invalid session") from None
        with self.ledger.transaction(tenant) as conn:
            row = conn.execute(
                "SELECT s.user_id,s.role FROM bonuspro.sessions s "
                "JOIN bonuspro.installations i ON i.tenant_id=s.tenant_id AND i.generation=s.generation "
                "WHERE s.tenant_id=%s AND s.token_hash=%s AND s.expires_at>now() "
                "AND i.state IN ('SettingsRequired','Activated')",
                (tenant,sha256(token.encode()).hexdigest())).fetchone()
            if row is None:
                raise AuthenticationFailed("session expired or revoked")
            return UserContext(tenant,str(row["user_id"]),row["role"])

    def logout(self, token: str):
        context = self.authenticate(token)
        with self.ledger.transaction(context.tenant_id) as conn:
            conn.execute("DELETE FROM bonuspro.sessions WHERE tenant_id=%s AND token_hash=%s",
                         (context.tenant_id,sha256(token.encode()).hexdigest()))

    def state(self, tenant: str) -> dict:
        with self.ledger.transaction(tenant) as conn:
            row = conn.execute("SELECT state,updated_at FROM bonuspro.installations WHERE tenant_id=%s",
                               (tenant,)).fetchone()
            if not row:
                raise AuthenticationFailed("installation is missing")
            return {"state":row["state"],"updated_at":row["updated_at"].isoformat()}
