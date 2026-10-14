"""Vendor user context exchange. Provider tokens never enter URLs or logs."""
from dataclasses import dataclass
from uuid import UUID
import httpx
from bonuspro.security import Secrets, AuthenticationFailed


class ProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class UserContext:
    tenant_id: str
    user_id: str
    role: str


class VendorClient:
    URL = "https://apps-api.moysklad.ru/api/vendor/1.0/context/user"

    def __init__(self, secrets: Secrets, transport=None):
        self.secrets = secrets
        self.transport = transport

    def exchange(self, token: str) -> UserContext:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise AuthenticationFailed("invalid context")
        try:
            with httpx.Client(timeout=httpx.Timeout(8, connect=3), follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                with client.stream("POST", self.URL,
                    headers={"Authorization": "Bearer "+self.secrets.outbound_jwt(),
                             "Accept":"application/json", "Accept-Encoding":"gzip"},
                    json={"token":token}) as response:
                    if response.status_code in (400,401,403,404):
                        raise AuthenticationFailed("context exchange rejected")
                    if response.status_code != 200:
                        raise ProviderUnavailable("context service unavailable")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>65536:
                            raise ProviderUnavailable("invalid context response")
                    import json
                    data = json.loads(body)
            tenant, user = str(UUID(data["accountId"])), str(UUID(data["userId"]))
            if data["role"] not in ("admin","cashier","worker","individual"):
                raise AuthenticationFailed("unknown role")
            return UserContext(tenant, user, data["role"])
        except httpx.HTTPError as exc:
            # Never interpolate HTTP requests, which can include Authorization.
            raise ProviderUnavailable("context service unavailable") from None
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, AuthenticationFailed):
                raise
            raise ProviderUnavailable("invalid context response") from None

    def activate(self,tenant: str):
        account=str(UUID(tenant));app=str(UUID(self.secrets.app_id))
        url=f"https://apps-api.moysklad.ru/api/vendor/1.0/apps/{app}/{account}/status"
        try:
            with httpx.Client(timeout=httpx.Timeout(8,connect=3),follow_redirects=False,
                              trust_env=False,transport=self.transport) as client:
                with client.stream("PUT",url,headers={"Authorization":"Bearer "+self.secrets.outbound_jwt(),
                    "Accept":"application/json"},json={"status":"Activated"}) as response:
                    if response.status_code!=200:
                        raise ProviderUnavailable("activation status not confirmed")
        except httpx.HTTPError:
            raise ProviderUnavailable("activation status not confirmed") from None
