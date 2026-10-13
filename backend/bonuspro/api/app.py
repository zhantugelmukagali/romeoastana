"""MoySklad installation, administration and gated saved-discount verification."""
from uuid import UUID
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

import psycopg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, SecretStr, ConfigDict, field_validator, StrictInt

from bonuspro.settings import Settings
from bonuspro.security import AuthenticationFailed
from bonuspro.adapters.moysklad.vendor import VendorClient, ProviderUnavailable
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, NotFound
from bonuspro.adapters.postgres.returns import Returns
from bonuspro.adapters.postgres.checkout import Checkout
from bonuspro.adapters.postgres.redemptions import Redemptions
from bonuspro.adapters.moysklad.remap import RemapClient, RemapError, RetryLater, AccessDenied, EntityMissing
from bonuspro.adapters.moysklad.order import read_order

REMAP = "https://api.moysklad.ru/api/remap/1.2"


class Access(BaseModel):
    resource: str
    access_token: SecretStr


class Activation(BaseModel):
    appUid: str
    cause: Literal["Install","Resume"]
    access: list[Access] = Field(min_length=1,max_length=20)


class Deactivation(BaseModel):
    appUid: str | None = None
    cause: Literal["Suspend","Uninstall"]


class ContextExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1,max_length=4096)


class ReturnDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["reverse","waive"]
    reason: str = Field(min_length=1,max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls,value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    earn_bps: StrictInt = Field(ge=0,le=10000)
    spend_bps: StrictInt = Field(ge=0,le=10000)
    delivery_policy: Literal["exclude"] = "exclude"
    expected_version: StrictInt = Field(ge=0,le=2147483646)
    request_id: UUID
    reason: str = Field(min_length=1,max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls,value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class RedemptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: StrictInt = Field(gt=0,le=90000000000000)
    request_id: UUID
    source_hash: str = Field(pattern="^[0-9a-f]{64}$")


class BodyLimit:
    def __init__(self, app, maximum=65536):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body",b""))
            if len(body)>self.maximum:
                return await JSONResponse({"error":"request too large"},status_code=413)(scope,receive,send)
            if not message.get("more_body",False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type":"http.request","body":bytes(body),"more_body":False}
            return await receive()
        await self.app(scope,replay,send)


def create_app(settings: Settings | None = None, *, store=None, vendor=None, remap_factory=None):
    config = settings or Settings.from_env()
    repo = store or Installations(config.database_url, config.lifecycle_database_url, config.credentials)
    provider = vendor or VendorClient(config.credentials)
    returns = Returns(Ledger(config.database_url))
    checkout = Checkout(Ledger(config.database_url))
    redemptions = Redemptions(Ledger(config.database_url))
    remap_factory = remap_factory or RemapClient
    app = FastAPI(title="БонусПро",docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlsplit(config.origin).hostname])

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Content-Security-Policy"]="default-src 'self'; frame-ancestors https://online.moysklad.ru; object-src 'none'; base-uri 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # FastAPI's default response may echo a credential from the submitted body.
        return JSONResponse({"error":"invalid request"},status_code=422)

    @app.exception_handler(AuthenticationFailed)
    async def invalid_auth(request, error):
        return JSONResponse({"error":"authentication required"},status_code=401)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"error":"operation conflict"},status_code=409)

    @app.exception_handler(NotFound)
    async def missing(request, error):
        return JSONResponse({"error":"not found"},status_code=404)

    @app.exception_handler(ProviderUnavailable)
    async def provider_down(request, error):
        return JSONResponse({"error":"provider temporarily unavailable"},status_code=503)

    @app.exception_handler(psycopg.Error)
    async def database_down(request, error):
        return JSONResponse({"error":"service temporarily unavailable"},status_code=503)

    @app.exception_handler(RemapError)
    async def remap_error(request, error):
        if isinstance(error,RetryLater):
            return JSONResponse({"error":"moysklad_temporarily_unavailable"},status_code=503,
                                headers={"Retry-After":str(error.delay_seconds)})
        if isinstance(error,AccessDenied):
            return JSONResponse({"error":"moysklad_permissions_required"},status_code=403)
        if isinstance(error,EntityMissing):
            return JSONResponse({"error":"saved_order_not_found"},status_code=404)
        return JSONResponse({"error":"unsupported_saved_order"},status_code=422)

    def bearer(request):
        authorization = request.headers.get("authorization","")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower()!="bearer" or not value or len(value)>8192:
            raise AuthenticationFailed("bearer token required")
        return value

    def same_origin(request):
        # Browser endpoints have a mandatory Origin; Vendor callbacks use signed JWT.
        if request.headers.get("origin") != config.origin:
            raise HTTPException(403,"origin rejected")

    def vendor_claims(request, app_id, body_uid):
        if str(app_id)!=config.credentials.app_id or (body_uid and body_uid!=config.credentials.app_uid):
            raise HTTPException(404,"application not found")
        return config.credentials.verify_vendor(bearer(request))

    @app.get("/health/live")
    def live():
        return {"status":"ok"}

    @app.get("/health/ready")
    def ready():
        # Connectivity and expected schema revision, without disclosing infrastructure details.
        try:
            with psycopg.connect(config.database_url,connect_timeout=3) as conn:
                conn.execute("SET LOCAL statement_timeout='3s'")
                exists = conn.execute("SELECT to_regclass('bonuspro.installations'),to_regclass('bonuspro.ledger_transactions'),to_regclass('bonuspro.return_cases'),to_regclass('bonuspro.jobs'),to_regclass('bonuspro.rule_versions'),to_regclass('bonuspro.order_contexts'),to_regclass('bonuspro.customer_wallets'),to_regclass('bonuspro.redemption_attempts'),to_regclass('bonuspro.widget_bindings')").fetchone()
                revision = conn.execute("SELECT version_num FROM public.alembic_version").fetchall()
                if not all(exists) or revision != [("0007",)]:
                    raise RuntimeError("missing schema")
            return {"status":"ready"}
        except (psycopg.Error,RuntimeError):
            return JSONResponse({"status":"not ready"},status_code=503)

    @app.put("/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
    def install(app_id: UUID, account_id: UUID, body: Activation, request: Request):
        claims = vendor_claims(request,app_id,body.appUid)
        access = [x for x in body.access if x.resource.rstrip("/")==REMAP]
        if len(access)!=1:
            raise HTTPException(422,"REMAP access is required")
        state = repo.change(str(account_id),claims,body.cause,access[0].access_token.get_secret_value())
        return {"status":state}

    @app.delete("/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
    def uninstall(app_id: UUID, account_id: UUID, body: Deactivation, request: Request):
        claims = vendor_claims(request,app_id,body.appUid)
        repo.change(str(account_id),claims,body.cause)
        return Response(status_code=200)

    @app.post("/api/session")
    def session(body: ContextExchange, request: Request):
        same_origin(request)
        identity = provider.exchange(body.token.get_secret_value())
        token = repo.create_session(identity)
        return {"token":token,"expires_in":900}

    @app.delete("/api/session")
    def logout(request: Request):
        same_origin(request)
        repo.logout(bearer(request))
        return Response(status_code=204)

    @app.get("/api/me")
    def me(request: Request):
        identity = repo.authenticate(bearer(request))
        return {"account_id":identity.tenant_id,"user_id":identity.user_id,"role":identity.role}

    @app.get("/api/installation")
    def installation(request: Request):
        identity = repo.authenticate(bearer(request))
        if identity.role!="admin":
            raise HTTPException(403,"administrator required")
        return repo.state(identity.tenant_id)


    @app.get("/api/admin/returns")
    def return_cases(request: Request, state: Literal["pending","reversed","waived"]="pending",
                     before: UUID | None=None):
        identity = repo.authenticate(bearer(request))
        if identity.role!="admin":
            raise HTTPException(403,"administrator required")
        return returns.list_cases(identity.tenant_id,state=state,before=str(before) if before else None)

    @app.post("/api/admin/returns/{case_id}/resolve")
    def resolve_return(case_id: UUID, body: ReturnDecision, request: Request):
        same_origin(request)
        identity = repo.authenticate(bearer(request))
        if identity.role!="admin":
            raise HTTPException(403,"administrator required")
        return returns.resolve(identity.tenant_id,str(case_id),decision=body.decision,
                               actor_id=identity.user_id,role=identity.role,reason=body.reason.strip())

    @app.get("/api/admin/rules")
    def get_rules(request: Request):
        identity = repo.authenticate(bearer(request))
        if identity.role != "admin":
            raise HTTPException(403,"administrator required")
        return {"current":checkout.rules(identity.tenant_id)}

    @app.get("/api/admin/rules/history")
    def get_rules_history(request: Request, before: int | None = None):
        identity = repo.authenticate(bearer(request))
        if identity.role != "admin":
            raise HTTPException(403,"administrator required")
        if before is not None and not 1 <= before <= 2147483647:
            raise HTTPException(422,"invalid cursor")
        return checkout.history(identity.tenant_id,before=before)

    @app.put("/api/admin/rules")
    def update_rules(body: RuleUpdate, request: Request):
        same_origin(request)
        identity = repo.authenticate(bearer(request))
        if identity.role != "admin":
            raise HTTPException(403,"administrator required")
        result = checkout.save_rules(identity.tenant_id,earn_bps=body.earn_bps,
                    spend_bps=body.spend_bps,delivery_policy=body.delivery_policy,
                    expected_version=body.expected_version,request_id=str(body.request_id),
                    actor_id=identity.user_id,reason=body.reason)
        return {"current":result}


    def widget_identity(request):
        identity=repo.authenticate(bearer(request))
        # User-context exposes a role, not document-specific write permissions.
        # Until explicit operator authorization exists, do not elevate a worker to an app-token admin.
        if identity.role!="admin":
            raise HTTPException(403,"administrator required")
        return identity

    def document_client(identity):
        return remap_factory(repo.remap_token(identity.tenant_id),identity.tenant_id)

    @app.post("/api/admin/widget/setup")
    def widget_setup(request: Request):
        same_origin(request)
        identity=widget_identity(request)
        marker=document_client(identity).marker_attribute(create=True)
        repo.authenticate(bearer(request))
        redemptions.bind(identity.tenant_id,marker,identity.user_id)
        return {"configured":True}

    @app.get("/api/widget/orders/{order_id}")
    def widget_order(order_id: UUID,request: Request):
        identity=widget_identity(request)
        existing=redemptions.existing(identity.tenant_id,str(order_id))
        if existing:
            return {"redemption":redemptions.public(existing)}
        marker=redemptions.binding(identity.tenant_id)
        saved=read_order(document_client(identity),str(order_id),marker_id=marker)
        rules=redemptions.rules_for_order(identity.tenant_id,str(order_id))
        repo.authenticate(bearer(request))
        value=checkout.preview(identity.tenant_id,saved.snapshot,requested_points=0,
                               actor_id=identity.user_id,eligibility_version=rules["version"])
        # API token and full provider document never leave the backend.
        return {"quote":value,"saved_source_hash":saved.source_hash,"name":saved.name,
                "eligible_minor":saved.snapshot.eligible_minor,"total_minor":saved.snapshot.total_minor,
                "can_prepare":bool(marker) and repo.state(identity.tenant_id)["state"]=="Activated"}

    @app.post("/api/widget/orders/{order_id}/prepare")
    def widget_prepare(order_id: UUID,body: RedemptionRequest,request: Request):
        same_origin(request)
        identity=widget_identity(request)
        marker=redemptions.binding(identity.tenant_id)
        if not marker or repo.state(identity.tenant_id)["state"]!="Activated":
            raise Conflict("widget setup or sales activation required")
        saved=read_order(document_client(identity),str(order_id),marker_id=marker)
        repo.authenticate(bearer(request))
        return redemptions.prepare(identity.tenant_id,saved,points=body.points,
                    request_id=str(body.request_id),actor_id=identity.user_id,expected_source=body.source_hash)

    @app.post("/api/widget/orders/{order_id}/verify")
    def widget_verify(order_id: UUID,request: Request):
        same_origin(request)
        identity=widget_identity(request)
        marker=redemptions.binding(identity.tenant_id)
        if not marker:
            raise Conflict("widget setup required")
        saved=read_order(document_client(identity),str(order_id),marker_id=marker)
        repo.authenticate(bearer(request))
        return redemptions.confirm_saved(identity.tenant_id,saved)

    @app.get("/widget")
    def widget_page():
        return FileResponse(web / "widget.html")

    @app.get("/settings")
    def settings_page():
        return FileResponse(web / "settings.html")

    web = Path(__file__).resolve().parents[3] / "web"
    app.mount("/assets",StaticFiles(directory=web / "assets"),name="assets")

    @app.get("/")
    @app.get("/admin")
    def admin_page():
        return FileResponse(web / "index.html")

    return app
