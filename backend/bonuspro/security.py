"""Vendor JWT validation and account-bound credential encryption."""
import base64
import binascii
import os
import logging
import math
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


logger = logging.getLogger("bonuspro.vendor_auth")


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
        # Emit fixed reason codes only: never tokens, keys, claims or exception text.
        def reject(reason):
            logger.warning("VENDOR_AUTH_REJECT reason=%s", reason)
            raise AuthenticationFailed("invalid authentication")

        if not isinstance(token, str) or not token or len(token) > 8192:
            reject("token_format")
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm != "HS256":
                # Classify only allowlisted names; never echo arbitrary header data.
                label = {"HS384": "hs384", "HS512": "hs512",
                         "none": "none"}.get(algorithm, "other") if isinstance(algorithm, str) else "invalid"
                reject("header_algorithm_" + label)
            if "crit" in header:
                reject("header_critical")
            token_type = header.get("typ")
            # typ is optional media-type metadata in RFC 7519 section 5.1.
            # Permit its absence and JWT casing variants, while keeping the
            # fixed algorithm, signature, purpose and replay checks mandatory.
            if "typ" in header and (not isinstance(token_type, str) or token_type.upper() != "JWT"):
                reject("header_type")
            claims = jwt.decode(
                token, self.vendor_secret, algorithms=["HS256"],
                options={"require": ["iat", "exp", "jti"], "verify_aud": False,
                         "verify_iat": False, "verify_exp": False, "verify_nbf": False})
        except AuthenticationFailed:
            raise
        except jwt.InvalidSignatureError:
            reject("signature")
        except jwt.MissingRequiredClaimError:
            reject("required_claim")
        except (jwt.PyJWTError, TypeError, ValueError):
            reject("jwt_format")

        # Inbound NumericDate is a JSON number, including fractional seconds.
        # The documented 300-second cap applies to developer -> provider tokens;
        # it is not a documented lifetime limit for provider -> developer tokens.
        for name in ("iat", "exp", "nbf"):
            if name not in claims:
                continue
            value = claims[name]
            if (type(value) not in (int, float) or not 0 <= value <= 253402300799
                    or not math.isfinite(value)):
                reject("numeric_date")
        now = time.time()
        if claims["exp"] <= now:
            reject("expired")
        if claims["iat"] > now or ("nbf" in claims and claims["nbf"] > now):
            reject("not_yet_valid")
        if claims["exp"] <= claims["iat"]:
            reject("time_order")
        if not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 256:
            reject("jti")
        # Our outbound token type has a subject and must never authorize callbacks.
        if "sub" in claims:
            reject("token_purpose")
        # Lifecycle ordering is stored in whole seconds. Normalize only after
        # validating the signature and the original, unrounded time boundaries.
        claims["iat"] = math.floor(claims["iat"])
        if "typ" not in header:
            logger.warning("VENDOR_AUTH_COMPAT typ=absent")
        return claims

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
