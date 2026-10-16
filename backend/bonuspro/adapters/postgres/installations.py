"""Lifecycle storage uses a separate least-privileged database login."""
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
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

    @staticmethod
    def _consume(conn,claims):
        # Expired signed callbacks cannot authenticate again. Remove only a
        # bounded batch with a safety margin; active replay fences remain.
        conn.execute("DELETE FROM bonuspro.vendor_replays r USING "
                     "(SELECT jti FROM bonuspro.vendor_replays "
                     "WHERE expires_at<clock_timestamp()-interval '10 minutes' "
                     "LIMIT 100) expired WHERE r.jti=expired.jti")
        consumed=conn.execute(
            "INSERT INTO bonuspro.vendor_replays(jti,expires_at) VALUES (%s,to_timestamp(%s)) "
            "ON CONFLICT DO NOTHING RETURNING jti",(claims["jti"],claims["exp"])).fetchone()
        if not consumed:
            logging.getLogger("bonuspro.vendor_auth").warning("VENDOR_AUTH_REJECT reason=replay")
            raise AuthenticationFailed("repeated authentication token")

    def notice(self,account_id,claims,cause):
        """Acknowledge signed non-credential events without financial/state effects."""
        if cause not in ("TariffChanged","Autoprolongation","PermissionsChanged"):
            raise ValueError("unsupported notice")
        account=str(UUID(account_id))
        with self.lifecycle() as conn:
            self._consume(conn,claims)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("installation/"+account,))
            row=conn.execute("SELECT state FROM bonuspro.installations WHERE tenant_id=%s",(account,)).fetchone()
            if cause=="PermissionsChanged":
                return row["state"] if row else None
            if not row or row["state"] not in ("SettingsRequired","Activated"):
                raise Conflict("billing notification cannot activate an inactive installation")
            return row["state"]

    @staticmethod
    def _resume_ready(conn,account):
        # Reuse administrator consent only for the same approved configuration.
        conn.execute("SELECT set_config('bonuspro.tenant_id',%s,true)",(account,))
        return bool(conn.execute("""
            SELECT EXISTS(
                SELECT 1 FROM bonuspro.activation_requests a
                JOIN bonuspro.activation_completions c
                  ON c.tenant_id=a.tenant_id AND c.request_id=a.id
                JOIN bonuspro.automation_config cfg ON cfg.tenant_id=a.tenant_id
                JOIN bonuspro.widget_bindings b ON b.tenant_id=a.tenant_id AND b.marker_id=a.marker_id
                WHERE a.tenant_id=%s AND a.rule_version=(
                    SELECT max(version) FROM bonuspro.rule_versions WHERE tenant_id=%s)
            ) AS ready
        """,(account,account)).fetchone()["ready"])

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
            self._consume(conn,claims)
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
            if cause=="Resume" and row and row["state"]=="Suspended" and self._resume_ready(conn,account):
                state="Activated"
            # New installs require activation; Resume can reuse proven cutover consent.
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
            # Sessions are tenant-scoped, short-lived and stored as hashes.
            # Bounded opportunistic cleanup prevents indefinite accumulation
            # without granting a cross-tenant maintenance login.
            conn.execute(
                "DELETE FROM bonuspro.sessions s USING "
                "(SELECT tenant_id,token_hash FROM bonuspro.sessions "
                "WHERE tenant_id=%s AND expires_at<clock_timestamp() "
                "ORDER BY expires_at,token_hash LIMIT 100) expired "
                "WHERE s.tenant_id=expired.tenant_id AND s.token_hash=expired.token_hash", (tenant,))
            conn.execute("INSERT INTO bonuspro.known_operators(tenant_id,user_id,login) VALUES (%s,%s,%s) "
                         "ON CONFLICT (tenant_id,user_id) DO UPDATE SET login=EXCLUDED.login,last_seen=clock_timestamp()",
                         (tenant,user,context.login))
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

    def remap_token(self,tenant):
        """Internal only. Never serialize this return value in an API response."""
        with self.ledger.transaction(tenant) as conn:
            row=conn.execute("SELECT token_ciphertext FROM bonuspro.installations WHERE tenant_id=%s "
                             "AND state IN ('SettingsRequired','Activated')",(tenant,)).fetchone()
            if not row or not row["token_ciphertext"]:
                raise AuthenticationFailed("installation is inactive")
            return self.config.decrypt(tenant,row["token_ciphertext"])
