"""MoySklad installation, administration and gated saved-discount verification."""
import logging
from uuid import UUID, uuid4
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

import psycopg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.datastructures import MutableHeaders
from pydantic import BaseModel, Field, SecretStr, ConfigDict, field_validator, StrictInt, StrictBool, model_validator

from bonuspro.settings import Settings
from bonuspro.security import AuthenticationFailed
from bonuspro.adapters.moysklad.vendor import VendorClient, ProviderUnavailable
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.ledger import Ledger, Conflict, NotFound
from bonuspro.adapters.postgres.returns import Returns
from bonuspro.adapters.postgres.checkout import Checkout
from bonuspro.adapters.postgres.portal import Portal
from bonuspro.adapters.postgres.personal_discounts import PersonalDiscounts
from bonuspro.adapters.postgres.readiness import Readiness
from bonuspro.adapters.postgres.redemptions import Redemptions,SetupInProgress,ProgramNotConfigured
from bonuspro.adapters.postgres.automation import Automation
from bonuspro.adapters.postgres.imports import Imports
from bonuspro.adapters.postgres.activation import ActivationService,ActivationConflict
from bonuspro.adapters.postgres.operators import Operators,OperatorDenied
from bonuspro.adapters.postgres.voids import Voids
from bonuspro.importing import ImportInvalid
from bonuspro.adapters.moysklad.remap import RemapClient, RemapError, RetryLater, AccessDenied, EntityMissing, MetadataRejected
from bonuspro.adapters.moysklad.order import read_order
from bonuspro.adapters.moysklad.fulfillment import FulfillmentReader

REMAP = "https://api.moysklad.ru/api/remap/1.2"


class Access(BaseModel):
    resource: str
    access_token: SecretStr


class Activation(BaseModel):
    appUid: str
    cause: Literal["Install","Resume","TariffChanged","Autoprolongation"]
    access: list[Access] = Field(default_factory=list,max_length=20)

    @model_validator(mode="after")
    def access_for_cause(self):
        if self.cause in ("Install","Resume") and not self.access:
            raise ValueError("installation credentials required")
        if self.cause in ("TariffChanged","Autoprolongation") and self.access:
            raise ValueError("billing notifications cannot replace credentials")
        return self


class PermissionAccess(BaseModel):
    resource: str
    permissions: dict = Field(default_factory=dict)


class PermissionEvent(BaseModel):
    appUid: str
    cause: Literal["PermissionsChanged"]
    access: list[PermissionAccess] = Field(min_length=1,max_length=20)


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


class SettlementDecision(ReturnDecision):
    decision: Literal["apply","waive"]


class IssueDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1,max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError("reason required")
        return value.strip()


class OperatorUpdate(IssueDecision):
    request_id: UUID
    enabled: StrictBool
    expected_version: StrictInt = Field(ge=0,le=2147483646)


class ProgramActivation(IssueDecision):
    request_id: UUID
    balances_reviewed: Literal[True]
    previous_program_disabled: Literal[True]


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_text: str = Field(min_length=1,max_length=60000)


class ImportCommit(IssueDecision):
    digest: str = Field(pattern="^[0-9a-f]{64}$")


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


class PersonalDiscountPolicyUpdate(IssueDecision):
    request_id: UUID
    expected_version: StrictInt = Field(ge=0,le=2147483646)
    enabled: StrictBool
    external_policy: Literal["skip","best","sequential"]
    allow_bonus_stack: StrictBool


class CustomerDiscountUpdate(IssueDecision):
    request_id: UUID
    expected_version: StrictInt = Field(ge=0,le=2147483646)
    rate_bps: StrictInt | None = Field(ge=0,le=10000)


class RedemptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: StrictInt = Field(ge=0,le=90000000000000)
    request_id: UUID
    source_hash: str = Field(pattern="^[0-9a-f]{64}$")
    apply_personal: StrictBool = False
    expected_policy_version: StrictInt | None = Field(default=None,ge=0,le=2147483647)
    expected_assignment_version: StrictInt | None = Field(default=None,ge=0,le=2147483647)

    @model_validator(mode="after")
    def requested_benefit(self):
        if not self.points and not self.apply_personal:
            raise ValueError("a discount or positive redemption is required")
        if self.apply_personal and (self.expected_policy_version is None or self.expected_assignment_version is None):
            raise ValueError("personal discount versions are required")
        return self


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    source_hash: str = Field(min_length=64,max_length=64,pattern="^[0-9a-f]{64}$")


class SecurityHeaders:
    """Add headers directly to ASGI response-start; never wrap or consume its body."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", []))
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; frame-ancestors https://online.moysklad.ru; "
                    "object-src 'none'; base-uri 'none'")
            await send(message)

        await self.app(scope, receive, secured_send)


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
    discounts = PersonalDiscounts(Ledger(config.database_url))
    redemptions = Redemptions(Ledger(config.database_url))
    automation = Automation(Ledger(config.database_url))
    imports = Imports(Ledger(config.database_url))
    operators = Operators(Ledger(config.database_url))
    remap_factory = remap_factory or RemapClient
    app = FastAPI(title="БонусПро",docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlsplit(config.origin).hostname])

    app.add_middleware(SecurityHeaders)

    def widget_failure(request,*,reason="saved_order_invalid",error_code="unsupported_saved_order"):
        # Only finite codes and an opaque correlation value are public/logged.
        # No provider bodies, exception text, tokens or customer/order identities.
        if not request.url.path.startswith("/api/widget/orders/"):
            return JSONResponse({"error":error_code},status_code=422)
        allowed=("vat_mode_missing","vat_mode_unsupported","customer_missing","currency_unsupported","exchange_rate_unsupported","positions_missing","positions_limit","position_precision_unsupported","totals_mismatch","linked_documents_present","order_has_marker","discount_not_representable","saved_order_invalid","invalid_request")
        if reason not in allowed:reason="saved_order_invalid"
        path=request.url.path
        stage="prepare" if path.endswith("/prepare") else "verify" if path.endswith("/verify") else "resume" if path.endswith("/resume") else "load"
        trace=uuid4().hex[:12]
        logging.getLogger("bonuspro.widget").warning("WIDGET_REJECT reason=%s stage=%s trace=BP-%s",reason,stage,trace)
        return JSONResponse({"error":error_code,"reason":reason,"stage":stage,"trace_id":trace},status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # FastAPI's default response may echo a credential from the submitted body.
        return widget_failure(request,reason="invalid_request",error_code="invalid request")

    @app.exception_handler(AuthenticationFailed)
    async def invalid_auth(request, error):
        return JSONResponse({"error":"authentication required"},status_code=401)

    @app.exception_handler(OperatorDenied)
    async def operator_denied(request,error):
        return JSONResponse({"error":"operator_access_required"},status_code=403)

    @app.exception_handler(ImportInvalid)
    async def invalid_import(request,error):
        return JSONResponse({"error":"invalid_import","message":str(error)},status_code=422)

    @app.exception_handler(ProgramNotConfigured)
    async def program_not_configured(request,error):
        return JSONResponse({"error":"program_not_configured"},status_code=409)

    @app.exception_handler(ActivationConflict)
    async def activation_conflict(request,error):
        return JSONResponse({"error":error.code},status_code=409)

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
        return widget_failure(request,reason=getattr(error,"reason","saved_order_invalid"))

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
                exists = conn.execute("SELECT to_regclass('bonuspro.installations'),to_regclass('bonuspro.ledger_transactions'),to_regclass('bonuspro.return_cases'),to_regclass('bonuspro.jobs'),to_regclass('bonuspro.rule_versions'),to_regclass('bonuspro.order_contexts'),to_regclass('bonuspro.customer_wallets'),to_regclass('bonuspro.redemption_attempts'),to_regclass('bonuspro.widget_bindings'),to_regclass('bonuspro.automation_config'),to_regclass('bonuspro.order_accruals'),to_regclass('bonuspro.order_monitors'),to_regclass('bonuspro.sync_state'),to_regclass('bonuspro.reconciliation_issues'),to_regclass('bonuspro.settlement_cases'),to_regclass('bonuspro.settlement_events'),to_regclass('bonuspro.import_batches'),to_regclass('bonuspro.opening_balances'),to_regclass('bonuspro.activation_requests'),to_regclass('bonuspro.activation_completions'),to_regclass('bonuspro.operator_permissions'),to_regclass('bonuspro.redemption_voids'),to_regclass('bonuspro.order_customer_rebindings'),to_regprocedure('bonuspro.rebind_order_customer(uuid,uuid,text,text,uuid)'),to_regclass('bonuspro.personal_discount_policy_versions'),to_regclass('bonuspro.customer_personal_discount_versions'),to_regclass('bonuspro.personal_discount_applications'),to_regclass('bonuspro.personal_discount_voids')").fetchone()
                revision = conn.execute("SELECT version_num FROM public.alembic_version").fetchall()
                if not all(exists) or revision != [("0015",)]:
                    raise RuntimeError("missing schema")
            return {"status":"ready"}
        except (psycopg.Error,RuntimeError):
            return JSONResponse({"status":"not ready"},status_code=503)

    @app.put("/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
    def install(app_id: UUID, account_id: UUID, body: Activation, request: Request):
        claims = vendor_claims(request,app_id,body.appUid)
        if body.cause in ("TariffChanged","Autoprolongation"):
            return {"status":repo.notice(str(account_id),claims,body.cause)}
        access = [x for x in body.access if x.resource.rstrip("/")==REMAP]
        if len(access)!=1:
            raise HTTPException(422,"REMAP access is required")
        state = repo.change(str(account_id),claims,body.cause,access[0].access_token.get_secret_value())
        return {"status":state}

    @app.put("/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}/event")
    @app.put("/api/vendor/1.0/apps/{app_id}/{account_id}/event")
    def permission_event(app_id: UUID,account_id: UUID,body: PermissionEvent,request: Request):
        claims=vendor_claims(request,app_id,body.appUid)
        grants=[item for item in body.access if item.resource.rstrip("/")==REMAP]
        if len(grants)!=1:
            raise HTTPException(422,"REMAP access is required")
        # No credential is sent with this event. Provider permissions are enforced
        # on each REMAP request; there is no local cache to overwrite or elevate.
        repo.notice(str(account_id),claims,body.cause)
        return Response(status_code=200)

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


    def admin_identity(request):
        identity=repo.authenticate(bearer(request))
        # Administrative actions always require the provider-confirmed administrator role.
        if identity.role!="admin":
            raise HTTPException(403,"administrator required")
        return identity

    def operator_identity(request):
        identity=repo.authenticate(bearer(request))
        operators.check(identity)
        return identity

    @app.get("/staff")
    def staff_page():
        return FileResponse(web / "staff.html")

    @app.get("/api/admin/personal-discounts/policy")
    def get_personal_discount_policy(request: Request):
        identity=admin_identity(request)
        return {"current":discounts.policy(identity.tenant_id)}

    @app.get("/api/admin/personal-discounts/policy/history")
    def personal_discount_policy_history(request: Request,before: int | None=None):
        identity=admin_identity(request)
        if before is not None and not 1<=before<=2147483647:
            raise HTTPException(422,"invalid cursor")
        return discounts.policy_history(identity.tenant_id,before=before)

    @app.put("/api/admin/personal-discounts/policy")
    def update_personal_discount_policy(body: PersonalDiscountPolicyUpdate,request: Request):
        same_origin(request);identity=admin_identity(request)
        result=discounts.save_policy(identity.tenant_id,enabled=body.enabled,
            external_policy=body.external_policy,allow_bonus_stack=body.allow_bonus_stack,
            expected_version=body.expected_version,request_id=str(body.request_id),
            actor_id=identity.user_id,reason=body.reason)
        return {"current":result}

    @app.get("/api/admin/customers/{customer_id}/personal-discount/history")
    def personal_discount_customer_history(customer_id: UUID,request: Request,before: int | None=None):
        identity=admin_identity(request)
        if before is not None and not 1<=before<=2147483647:
            raise HTTPException(422,"invalid cursor")
        return discounts.assignment_history(identity.tenant_id,str(customer_id),before=before)

    @app.put("/api/admin/customers/{customer_id}/personal-discount")
    def update_customer_personal_discount(customer_id: UUID,body: CustomerDiscountUpdate,request: Request):
        same_origin(request);identity=admin_identity(request)
        customer=document_client(identity).entity("counterparty",str(customer_id))
        admin_identity(request)
        if customer.get("archived") is True:
            return JSONResponse({"error":"customer_archived"},status_code=409)
        result=discounts.save_assignment(identity.tenant_id,str(customer_id),rate_bps=body.rate_bps,
            expected_version=body.expected_version,request_id=str(body.request_id),
            actor_id=identity.user_id,reason=body.reason)
        return {"current":result}

    @app.get("/api/admin/operators")
    def operator_list(request: Request):
        identity=admin_identity(request)
        return operators.listing(identity.tenant_id)

    @app.put("/api/admin/operators/{user_id}")
    def operator_update(user_id: UUID,body: OperatorUpdate,request: Request):
        same_origin(request);identity=admin_identity(request)
        return operators.update(identity.tenant_id,str(user_id),enabled=body.enabled,
            expected_version=body.expected_version,request_id=str(body.request_id),actor=identity.user_id,reason=body.reason)

    def document_client(identity):
        if remap_factory is RemapClient:
            return remap_factory(repo.remap_token(identity.tenant_id),identity.tenant_id,budget_seconds=40)
        return remap_factory(repo.remap_token(identity.tenant_id),identity.tenant_id)

    @app.post("/api/admin/redemptions/{operation_id}/release")
    def release_cancelled(operation_id: UUID,body: IssueDecision,request: Request):
        same_origin(request);identity=admin_identity(request)
        service=Voids(automation.ledger);operation=service.lookup(identity.tenant_id,str(operation_id))
        client=RemapClient(repo.remap_token(identity.tenant_id),identity.tenant_id,budget_seconds=90,minimum_interval=0.35) if remap_factory is RemapClient else document_client(identity)
        proof=FulfillmentReader(client).read(str(operation["order_id"]),marker_id=redemptions.binding(identity.tenant_id))
        repo.authenticate(bearer(request))
        return service.release(identity.tenant_id,str(operation_id),proof,actor=identity.user_id,reason=body.reason)

    @app.post("/api/admin/activate")
    def activate_program(body: ProgramActivation,request: Request):
        same_origin(request);identity=admin_identity(request)
        return ActivationService(repo).activate(identity.tenant_id,str(body.request_id),
            identity.user_id,body.reason,provider)

    @app.get("/migration")
    def migration_page():
        return FileResponse(web / "migration.html")

    @app.get("/api/admin/imports")
    def import_history(request: Request):
        identity=admin_identity(request)
        return imports.history(identity.tenant_id)

    @app.post("/api/admin/imports/preview")
    def import_preview(body: ImportPreview,request: Request):
        same_origin(request);identity=admin_identity(request)
        client=RemapClient(repo.remap_token(identity.tenant_id),identity.tenant_id,budget_seconds=90,minimum_interval=0.35) if remap_factory is RemapClient else document_client(identity)
        result=imports.preview(identity.tenant_id,body.csv_text,identity.user_id,client)
        repo.authenticate(bearer(request))
        return result

    @app.post("/api/admin/imports/{batch_id}/commit")
    def import_commit(batch_id: UUID,body: ImportCommit,request: Request):
        same_origin(request);identity=admin_identity(request)
        return imports.commit(identity.tenant_id,str(batch_id),digest=body.digest,actor=identity.user_id,reason=body.reason)

    @app.get("/api/admin/settlements")
    def settlement_list(request: Request,state: Literal["pending","applied","waived","superseded"]="pending",before: UUID | None=None):
        identity=admin_identity(request)
        return automation.settlements.list_cases(identity.tenant_id,state=state,before=str(before) if before else None)

    @app.post("/api/admin/settlements/{case_id}/resolve")
    def settlement_resolve(case_id: UUID,body: SettlementDecision,request: Request):
        same_origin(request)
        identity=admin_identity(request)
        case=automation.settlements.case(identity.tenant_id,str(case_id))
        if case["state"]!="pending":
            if case["decision"]==body.decision and case["reason"]==body.reason and str(case["actor_id"])==identity.user_id:
                return {"state":case["state"]}
            raise Conflict("case already resolved")
        client=RemapClient(repo.remap_token(identity.tenant_id),identity.tenant_id,budget_seconds=90,minimum_interval=0.35) if remap_factory is RemapClient else document_client(identity)
        proof=FulfillmentReader(client).read(str(case["order_id"]),marker_id=redemptions.binding(identity.tenant_id))
        repo.authenticate(bearer(request))
        return automation.resolve_settlement(identity.tenant_id,str(case_id),proof,
            decision=body.decision,reason=body.reason,actor=identity.user_id)

    @app.post("/api/admin/issues/{issue_id}/resolve")
    def issue_resolve(issue_id: UUID,body: IssueDecision,request: Request):
        same_origin(request);identity=admin_identity(request)
        with automation.ledger.transaction(identity.tenant_id) as conn:
            issue=conn.execute("SELECT order_id FROM bonuspro.reconciliation_issues WHERE tenant_id=%s AND id=%s",
                              (identity.tenant_id,str(issue_id))).fetchone()
            if not issue:raise NotFound("issue missing")
        client=RemapClient(repo.remap_token(identity.tenant_id),identity.tenant_id,budget_seconds=90,minimum_interval=0.35) if remap_factory is RemapClient else document_client(identity)
        proof=FulfillmentReader(client).read(str(issue["order_id"]),marker_id=redemptions.binding(identity.tenant_id))
        repo.authenticate(bearer(request))
        return automation.resolve_issue(identity.tenant_id,str(issue_id),proof,actor=identity.user_id,reason=body.reason)

    @app.get("/reconciliation")
    def reconciliation_page():
        return FileResponse(web / "reconciliation.html")

    @app.get("/api/admin/automation")
    def automation_status(request: Request):
        identity=admin_identity(request)
        return automation.dashboard(identity.tenant_id)

    @app.post("/api/admin/automation")
    async def automation_configure(request: Request):
        same_origin(request)
        identity=admin_identity(request)
        # Server time only; a caller cannot backdate earning or nominate another tenant.
        if await request.body():
            raise HTTPException(422,"no configuration body accepted")
        if not checkout.rules(identity.tenant_id):
            raise Conflict("save rules before configuring automation")
        return await run_in_threadpool(automation.configure,identity.tenant_id,identity.user_id)

    @app.post("/api/admin/widget/setup")
    def widget_setup(request: Request):
        same_origin(request)
        identity=admin_identity(request)
        try:
            redemptions.configure_marker(identity.tenant_id,identity.user_id,document_client(identity),
                                         authorize=lambda:admin_identity(request))
        except SetupInProgress:
            return JSONResponse({"error":"widget_setup_in_progress"},status_code=409)
        except AccessDenied as error:
            code="moysklad_token_rejected" if error.status_code==401 else "moysklad_permissions_required"
            payload={"error":code}
            if error.provider_error_codes:
                payload["provider_error_codes"]=list(error.provider_error_codes)
            if error.provider_operation is not None:
                payload["provider_operation"]=error.provider_operation
            return JSONResponse(payload,status_code=403)
        except MetadataRejected as error:
            return JSONResponse({"error":"widget_metadata_rejected",
                "provider_status":error.provider_status,
                "provider_error_codes":list(error.provider_error_codes),
                "provider_operation":error.provider_operation},status_code=422)
        except RetryLater as error:
            delay=min(86400,max(1,error.delay_seconds)) if type(error.delay_seconds) is int else 30
            reason=("rate_limited" if error.provider_status==429 else
                    "timeout" if error.provider_status==408 or error.code=="provider_timeout" else
                    "conflict" if error.code=="conflict" else "unavailable")
            payload={"error":"widget_metadata_temporarily_unavailable",
                     "retry_reason":reason,"retry_after":delay}
            if error.provider_status is not None:
                payload["provider_status"]=error.provider_status
            if error.provider_error_codes:
                payload["provider_error_codes"]=list(error.provider_error_codes)
            if error.provider_operation is not None:
                payload["provider_operation"]=error.provider_operation
            return JSONResponse(payload,status_code=503,headers={"Retry-After":str(delay)})
        except RemapError:
            return JSONResponse({"error":"widget_metadata_invalid"},status_code=422)
        except Conflict:
            return JSONResponse({"error":"widget_binding_changed"},status_code=409)
        return {"configured":True}

    @app.get("/api/widget/orders/{order_id}")
    def widget_order(order_id: UUID,request: Request):
        identity=operator_identity(request)
        existing=redemptions.existing(identity.tenant_id,str(order_id))
        if existing:
            return {"redemption":redemptions.public(existing)}
        marker=redemptions.binding(identity.tenant_id)
        rules=redemptions.rules_for_order(identity.tenant_id,str(order_id))
        saved=read_order(document_client(identity),str(order_id),marker_id=marker,allow_fulfilled=True)
        operator_identity(request)
        value=checkout.preview(identity.tenant_id,saved.snapshot,requested_points=0,
                               actor_id=identity.user_id,eligibility_version=rules["version"],authorize=lambda conn:operators.check_in(conn,identity),
                               read_only_reason=saved.discount_blocked_reason)
        # API token and full provider document never leave the backend.
        return {"quote":value,"saved_source_hash":saved.source_hash,"name":saved.name,
                "eligible_minor":saved.snapshot.eligible_minor,"total_minor":saved.snapshot.total_minor,
                "can_prepare":bool(marker) and repo.state(identity.tenant_id)["state"]=="Activated",
                "personal_discount":redemptions.personal_preview(identity.tenant_id,saved,value)}

    @app.post("/api/widget/orders/{order_id}/prepare")
    def widget_prepare(order_id: UUID,body: RedemptionRequest,request: Request):
        same_origin(request)
        identity=operator_identity(request)
        marker=redemptions.binding(identity.tenant_id)
        if not marker or repo.state(identity.tenant_id)["state"]!="Activated":
            raise Conflict("widget setup or sales activation required")
        saved=read_order(document_client(identity),str(order_id),marker_id=marker)
        operator_identity(request)
        return redemptions.prepare(identity.tenant_id,saved,points=body.points,
                    request_id=str(body.request_id),actor_id=identity.user_id,expected_source=body.source_hash,
                    apply_personal=body.apply_personal,expected_policy_version=body.expected_policy_version,
                    expected_assignment_version=body.expected_assignment_version,
                    authorize=lambda conn:operators.check_in(conn,identity))

    @app.post("/api/widget/orders/{order_id}/resume")
    def widget_resume(order_id: UUID,body: ResumeRequest,request: Request):
        same_origin(request)
        identity=operator_identity(request)
        marker=redemptions.binding(identity.tenant_id)
        if not marker or repo.state(identity.tenant_id)["state"]!="Activated":
            raise Conflict("widget setup or sales activation required")
        # Strict reading rejects all links, even drafts. The service then checks
        # the current saved revision against the proof returned by verification.
        saved=read_order(document_client(identity),str(order_id),marker_id=marker)
        operator_identity(request)
        return redemptions.resume(identity.tenant_id,saved,attempt_id=str(body.attempt_id),
                    expected_source=body.source_hash,authorize=lambda conn:operators.check_in(conn,identity))

    @app.post("/api/widget/orders/{order_id}/verify")
    def widget_verify(order_id: UUID,request: Request):
        same_origin(request)
        identity=operator_identity(request)
        marker=redemptions.binding(identity.tenant_id)
        if not marker:
            raise Conflict("widget setup required")
        existing=redemptions.existing(identity.tenant_id,str(order_id))
        # A completed debit remains verifiable after later payment/shipment.
        # New debits still require an unpaid, unshipped document without links.
        completed=bool(existing and existing["state"]=="committed")
        saved=read_order(document_client(identity),str(order_id),marker_id=marker,allow_fulfilled=completed)
        operator_identity(request)
        return redemptions.confirm_saved(identity.tenant_id,saved,authorize=lambda conn:operators.check_in(conn,identity))

    @app.get("/api/admin/readiness")
    def operational_readiness(request: Request):
        identity=admin_identity(request)
        return Readiness(Ledger(config.database_url)).report(identity.tenant_id)

    @app.get("/api/admin/overview")
    def loyalty_overview(request: Request):
        identity=admin_identity(request)
        return Portal(Ledger(config.database_url)).summary(identity.tenant_id)

    @app.get("/api/admin/activity")
    def loyalty_activity(request: Request,customer: UUID | None=None,before: UUID | None=None):
        identity=admin_identity(request)
        return Portal(Ledger(config.database_url)).activity(identity.tenant_id,
            customer=str(customer) if customer else None,before=str(before) if before else None)

    @app.get("/api/admin/customers")
    def loyalty_customers(request: Request,q: str="",offset: int=0):
        identity=admin_identity(request)
        if len(q)>100 or (q and len(q.strip())<2) or not 0<=offset<=100000:
            raise HTTPException(422,"invalid customer search")
        result=document_client(identity).counterparties(q,offset)
        admin_identity(request)
        values=Portal(Ledger(config.database_url)).balances(identity.tenant_id,[row["id"] for row in result["items"]])
        personal=discounts.assignments(identity.tenant_id,[row["id"] for row in result["items"]])
        return dict(items=[{**row,"personal_discount":personal[row["id"]],
                            **values.get(row["id"],dict(enrolled=False,balance=0,reserved=0,available=0))}
                           for row in result["items"]],next_offset=result["next_offset"])

    @app.get("/api/admin/customers/{customer_id}")
    def loyalty_customer(customer_id: UUID,request: Request):
        identity=admin_identity(request)
        row=document_client(identity).entity("counterparty",str(customer_id))
        admin_identity(request)
        values=Portal(Ledger(config.database_url)).balances(identity.tenant_id,[str(customer_id)])
        return dict(id=str(customer_id),name=str(row.get("name",""))[:256],phone=str(row.get("phone",""))[:100],
                    archived=row.get("archived") is True,
                    personal_discount=discounts.assignment(identity.tenant_id,str(customer_id)),
                    **values.get(str(customer_id),dict(enrolled=False,balance=0,reserved=0,available=0)))

    @app.get("/returns")
    def previous_returns_page():
        return FileResponse(web / "returns.html")


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
