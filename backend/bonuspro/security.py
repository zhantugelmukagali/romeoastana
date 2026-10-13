"""Vendor JWT validation and account-bound credential encryption."""
import base64
import binascii
import os
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AuthenticationFailed(ValueError):
    pass


@dataclass(frozen=True)
class Secrets:
    app_id: str
    app_uid: str
    vendor_secret: str = field(repr=False)
    encryption_key: bytes = field(repr=False)

    def __post_init__(self):
        UUID(self.app_id)
        if not self.app_uid or len(self.app_uid) > 256:
            raise ValueError("invalid app UID")
        if len(self.vendor_secret.encode()) < 32:
            raise ValueError("vendor secret is missing or too short")
        if len(self.encryption_key) != 32:
            raise ValueError("AES-256 requires exactly 32 bytes")

    def outbound_jwt(self):
        now = int(time.time())
        return jwt.encode({"sub": self.app_uid, "iat": now, "exp": now+120, "jti": str(uuid4())},
                          self.vendor_secret, algorithm="HS256")

    def verify_vendor(self, token: str) -> dict:
        if not isinstance(token, str) or len(token) > 8192:
            raise AuthenticationFailed("invalid authentication")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "HS256" or header.get("typ") != "JWT" or header.get("crit"):
                raise AuthenticationFailed("invalid authentication")
            claims = jwt.decode(token, self.vendor_secret, algorithms=["HS256"],
                                options={"require": ["iat","exp","jti"], "verify_aud": False})
            if any(type(claims[x]) is not int for x in ("iat","exp")):
                raise AuthenticationFailed("invalid authentication")
            if not 0 < claims["exp"]-claims["iat"] <= 300:
                raise AuthenticationFailed("invalid authentication")
            if not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 256:
                raise AuthenticationFailed("invalid authentication")
            # Provider inbound JWT has no specified issuer/audience claims.
            # Reject our outbound token type; its subject belongs only to outbound requests.
            if "sub" in claims:
                raise AuthenticationFailed("wrong token purpose")
            return claims
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise AuthenticationFailed("invalid authentication") from exc

    def encrypt(self, tenant: str, token: str) -> str:
        tenant = str(UUID(tenant))
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError("invalid provider credential")
        nonce = os.urandom(12)
        aad = f"bonuspro/v1/{self.app_id}/{tenant}".encode()
        encrypted = AESGCM(self.encryption_key).encrypt(nonce, token.encode(), aad)
        return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def decrypt(self, tenant: str, envelope: str) -> str:
        tenant = str(UUID(tenant))
        if not envelope.startswith("v1."):
            raise ValueError("unsupported credential key version")
        try:
            raw = base64.b64decode(envelope[3:], altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid credential envelope") from exc
        aad = f"bonuspro/v1/{self.app_id}/{tenant}".encode()
        return AESGCM(self.encryption_key).decrypt(raw[:12], raw[12:], aad).decode()
