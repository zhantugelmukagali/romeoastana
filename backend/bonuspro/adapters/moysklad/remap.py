"""Restricted REMAP boundary (document reads; own marker metadata provisioning only). Returned documents are evidence, not authorization to post.

All destinations are constructed locally; external meta.href/nextHref are never
followed. Decimal JSON parsing preserves provider monetary values without binary
float. Transient failures go back to the durable scheduler, not an HTTP retry loop.
"""
from dataclasses import dataclass
from decimal import Decimal
import json
import time
from uuid import UUID
from urllib.parse import urlsplit, parse_qsl

import httpx

BASE="https://api.moysklad.ru/api/remap/1.2"
ENTITIES=frozenset(("customerorder","demand","salesreturn","paymentin","paymentout",
                    "cashin","cashout","counterparty","organization","currency"))
DOCUMENTS=frozenset(("customerorder","demand","salesreturn","paymentin","paymentout","cashin","cashout"))
POSITION_DOCUMENTS=frozenset(("customerorder","demand","salesreturn"))


class RemapError(RuntimeError):
    pass


class AccessDenied(RemapError):
    def __init__(self,message="MoySklad access denied",*,status_code=None,provider_error_codes=()):
        self.status_code=status_code
        values=provider_error_codes if isinstance(provider_error_codes,(list,tuple)) else ()
        self.provider_error_codes=tuple(sorted({code for code in values[:64]
            if type(code) is int and 0<=code<=999999})[:8])
        super().__init__(message)


class EntityMissing(RemapError):
    def __init__(self,message="MoySklad entity is missing",*,requested_kind=None,requested_id=None):
        self.requested_kind=requested_kind
        self.requested_id=requested_id
        super().__init__(message)


class InvalidDocument(RemapError):
    def __init__(self,message="invalid provider document",*,reason="saved_order_invalid"):
        self.reason=reason
        super().__init__(message)


class RetryLater(RemapError):
    def __init__(self, delay_seconds=30, code="provider_unavailable"):
        self.delay_seconds=delay_seconds
        self.code=code
        super().__init__("provider temporarily unavailable")


def uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError,TypeError,AttributeError):
        raise InvalidDocument("invalid entity identity") from None



def attribute_collection_href(value):
    """Validate a collection URL as data; never follow a returned destination.

    REMAP may echo the pagination query from our fixed request in meta.href.
    Accept only the complete first page of this exact metadata collection.
    """
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<=32 for c in value):
        return False
    try:
        parsed=urlsplit(value)
        expected=urlsplit(BASE+"/entity/customerorder/metadata/attributes")
        if (parsed.scheme,parsed.netloc,parsed.path.rstrip("/")) != (
                expected.scheme,expected.netloc,expected.path) or parsed.fragment:
            return False
        # Only a single optional trailing slash is a resource equivalent.
        if parsed.path not in (expected.path,expected.path+"/"):
            return False
        pairs=parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=2)
    except ValueError:
        return False
    seen=set()
    for name,number in pairs:
        if name in seen or (name,number) not in (("limit","1000"),("offset","0")):
            return False
        seen.add(name)
    return True

def reference(meta, allowed):
    if not isinstance(meta,dict):
        raise InvalidDocument("missing reference")
    kind=meta.get("type")
    if kind not in allowed or not isinstance(meta.get("href"),str):
        raise InvalidDocument("unsupported reference")
    href=meta["href"]
    identity=uuid(href.rsplit("/",1)[-1])
    if href != BASE+"/entity/"+kind+"/"+identity:
        raise InvalidDocument("untrusted entity reference")
    return kind,identity


def exact_minor(value):
    if isinstance(value,bool) or not isinstance(value,(int,Decimal)):
        raise InvalidDocument("money must be an exact JSON number")
    decimal=Decimal(value)
    if not decimal.is_finite() or decimal!=decimal.to_integral_value() or not 0<=decimal<=9_000_000_000_000_000:
        raise InvalidDocument("money must be integral minor units")
    return int(decimal)


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key]=value
    return result


def _invalid_number(value):
    raise ValueError("invalid JSON constant")


def _access_error_codes(response,deadline=None):
    """Keep numeric diagnostics only; an unreadable denial remains a denial."""
    if not response.headers.get("Content-Type","").lower().startswith("application/json"):
        return ()
    body=bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=4096):
            if (deadline is not None and time.monotonic()>=deadline) or len(body)+len(chunk)>64*1024:
                return ()
            body.extend(chunk)
        payload=json.loads(body,parse_float=Decimal,parse_constant=_invalid_number,
                           object_pairs_hook=_unique_object)
    except (httpx.HTTPError,ValueError,UnicodeError,RecursionError):
        return ()
    errors=payload.get("errors") if isinstance(payload,dict) else None
    if not isinstance(errors,list) or len(errors)>64:
        return ()
    return tuple(sorted({item["code"] for item in errors if isinstance(item,dict)
        and type(item.get("code")) is int and 0<=item["code"]<=999999})[:8])


class RemapClient:
    def __init__(self, token, tenant_id, *, transport=None, budget_seconds=None, minimum_interval=0):
        if not isinstance(token,str) or not token.strip() or len(token)>8192:
            raise ValueError("invalid REMAP credential")
        self._token=token
        self.tenant=uuid(tenant_id)
        self.transport=transport
        self.deadline=time.monotonic()+budget_seconds if budget_seconds else None
        self.minimum_interval=minimum_interval
        self.last_request=0

    def _request(self,method,path,params=None,body=None):
        # Private method: callers below construct this path exclusively from enums/UUIDs.
        if not path.startswith("/entity/") or any(x in path for x in ("..","?","#","%","\\","@")):
            raise InvalidDocument("invalid resource path")
        if method not in ("GET","POST") or (method=="POST" and path!="/entity/customerorder/metadata/attributes"):
            raise InvalidDocument("unsupported write resource")
        now=time.monotonic()
        if self.deadline is not None and now+11>=self.deadline:
            raise RetryLater(5,"provider_timeout")
        wait=self.minimum_interval-(now-self.last_request)
        if wait>0:
            time.sleep(min(wait,1))
        self.last_request=time.monotonic()
        headers={"Authorization":"Bearer "+self._token,"Accept":"application/json;charset=utf-8",
                 "Accept-Encoding":"gzip","X-Lognex-Accept-Region":"kz"}
        try:
            with httpx.Client(timeout=httpx.Timeout(10,connect=3),follow_redirects=False,
                              trust_env=False,transport=self.transport) as client:
                with client.stream(method,BASE+path,params=params,headers=headers,json=body) as response:
                    status=response.status_code
                    if status in (401,403):
                        raise AccessDenied("MoySklad access denied",status_code=status,
                                           provider_error_codes=_access_error_codes(response,self.deadline))
                    if status==404:
                        raise EntityMissing("MoySklad entity is missing")
                    if status==429:
                        values=[]
                        for name,divisor in (("X-Lognex-Retry-After",1000),("X-Lognex-Reset",1000),("Retry-After",1)):
                            raw=response.headers.get(name,"")
                            if raw.isascii() and raw.isdecimal() and len(raw)<=12:
                                values.append((int(raw)+divisor-1)//divisor)
                        raise RetryLater(min(86400,max([1,*values])),"provider_unavailable")
                    if status>=500 or status in (408,409,412):
                        raise RetryLater(30,"conflict" if status in (409,412) else "provider_unavailable")
                    if status!=200 or not response.headers.get("Content-Type","").lower().startswith("application/json"):
                        raise InvalidDocument("unexpected provider response")
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        if self.deadline is not None and time.monotonic()>=self.deadline:
                            raise RetryLater(5,"provider_timeout")
                        body.extend(chunk)
                        if len(body)>8*1024*1024:
                            raise InvalidDocument("provider response exceeds size limit")
                    try:
                        payload=json.loads(body,parse_float=Decimal,parse_constant=_invalid_number,
                                           object_pairs_hook=_unique_object)
                    except (ValueError,UnicodeError,RecursionError):
                        raise InvalidDocument("invalid provider JSON") from None
                    if method=="POST" and path=="/entity/customerorder/metadata/attributes" and isinstance(payload,list):
                        if len(payload)!=1 or not isinstance(payload[0],dict):
                            raise InvalidDocument("one created marker required")
                        payload=payload[0]
                    if not isinstance(payload,dict):
                        raise InvalidDocument("provider object required")
                    return payload
        except httpx.TimeoutException:
            raise RetryLater(5,"provider_timeout") from None
        except httpx.HTTPError:
            raise RetryLater() from None

    def _get(self,path,params=None):
        return self._request("GET",path,params)

    def marker_attribute(self, *, create=False):
        path="/entity/customerorder/metadata/attributes"
        # Token scope must include useOwnAttributeMetadata. No generic caller-controlled writes.
        page=self._get(path,{"limit":1000})
        rows=page.get("rows");collection=page.get("meta")
        if not isinstance(collection,dict) or not isinstance(rows,list) or len(rows)>1000 or any(not isinstance(row,dict) for row in rows):
            raise InvalidDocument("invalid attribute metadata")
        # Full attribute collections are documented without size/offset/limit.
        size=collection.get("size")
        if "size" in collection and (type(size) is not int or size!=len(rows)):
            raise InvalidDocument("incomplete attribute metadata")
        if collection.get("nextHref") or ("offset" in collection and (type(collection["offset"]) is not int or collection["offset"]!=0)):
            raise InvalidDocument("incomplete attribute metadata")
        if "href" in collection and not attribute_collection_href(collection["href"]):
            raise InvalidDocument("untrusted attribute collection")
        matches=[row for row in rows if row.get("name")=="БонусПро: операция списания"]
        if len(matches)>1:raise InvalidDocument("ambiguous redemption attribute")
        if not matches:
            if not create:raise InvalidDocument("widget setup required")
            row=self._request("POST",path,body=[{"name":"БонусПро: операция списания","type":"string","required":False}])
        else:row=matches[0]
        identity=uuid(row.get("id"));meta=row.get("meta")
        if (row.get("name")!="БонусПро: операция списания" or row.get("type")!="string" or row.get("required") is True
            or not isinstance(meta,dict) or meta.get("type")!="attributemetadata" or meta.get("href")!=BASE+path+"/"+identity):
            raise InvalidDocument("invalid redemption attribute")
        if "accountId" in row and uuid(row["accountId"])!=self.tenant:
            raise InvalidDocument("marker belongs to another account")
        return identity

    def order_ids_between(self,start,end):
        from bonuspro.adapters.moysklad.fulfillment import moment,MSK
        def stamp(value):
            return value.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if start.tzinfo is None or end.tzinfo is None or end<start:
            raise ValueError("aware chronological window required")
        result=[];seen=set();expected=None
        for offset in range(0,10000,1000):
            page=self._get("/entity/customerorder",{"limit":1000,"offset":offset,
                "filter":"created>="+stamp(start)+";created<="+stamp(end),"order":"created,asc"})
            size=page.get("meta",{}).get("size");rows=page.get("rows")
            if type(size) is not int or not 0<=size<=10000 or not isinstance(rows,list):
                raise InvalidDocument("discovery window exceeds supported size")
            if expected is not None and expected!=size:
                raise RetryLater(1,"conflict")
            expected=size
            if len(rows)>1000 or len(result)+len(rows)>size:
                raise InvalidDocument("invalid discovery page")
            for row in rows:
                identity=uuid(row.get("id"))
                if uuid(row.get("accountId"))!=self.tenant or row.get("meta",{}).get("type")!="customerorder":
                    raise InvalidDocument("discovery account/type mismatch")
                created=moment(row.get("created"))
                # The API accepts millisecond precision; reject rows outside the requested window.
                lower=start.replace(microsecond=(start.microsecond//1000)*1000)
                if not lower<=created<=end or identity in seen:
                    raise RetryLater(1,"conflict")
                seen.add(identity)
                result.append(identity)
            if len(result)==size:
                return result
            if len(rows)!=1000:
                raise RetryLater(1,"conflict")
        raise InvalidDocument("discovery collection too large")

    def counterparties(self,search="",offset=0):
        if not isinstance(search,str) or len(search)>100 or (search and len(search.strip())<2):
            raise InvalidDocument("invalid customer search")
        if type(offset) is not int or not 0<=offset<=100000:
            raise InvalidDocument("invalid customer page")
        params={"limit":25,"offset":offset,"order":"name,asc"}
        if search.strip():params["search"]=search.strip()
        page=self._get("/entity/counterparty",params)
        rows=page.get("rows");size=page.get("meta",{}).get("size")
        if not isinstance(rows,list) or len(rows)>25 or type(size) is not int or size<0:
            raise InvalidDocument("invalid customer collection")
        items=[];seen=set()
        for row in rows:
            if not isinstance(row,dict):raise InvalidDocument("invalid customer")
            identity=uuid(row.get("id"))
            if identity in seen or uuid(row.get("accountId"))!=self.tenant or row.get("meta",{}).get("type")!="counterparty":
                raise InvalidDocument("untrusted customer collection")
            seen.add(identity)
            items.append(dict(id=identity,name=str(row.get("name",""))[:256],
                              phone=str(row.get("phone",""))[:100],archived=row.get("archived") is True))
        return dict(items=items,next_offset=offset+len(rows) if rows and offset+len(rows)<size else None)

    def entity(self,kind,identity):
        if kind not in ENTITIES:
            raise InvalidDocument("unsupported entity")
        identity=uuid(identity)
        try:
            data=self._get("/entity/"+kind+"/"+identity)
        except EntityMissing:
            # Type only the fixed resource requested here; never trust a provider
            # message or treat a related-document/positions 404 as the main order.
            raise EntityMissing(requested_kind=kind,requested_id=identity) from None
        if uuid(data.get("id"))!=identity:
            raise InvalidDocument("entity identity mismatch")
        # Currency responses have no accountId in the official REMAP contract.
        # The account-scoped credential and locally constructed resource select it.
        # If a provider does return accountId, it must still match this tenant.
        if (kind!="currency" or "accountId" in data) and uuid(data.get("accountId"))!=self.tenant:
            raise InvalidDocument("entity does not belong to expected account")
        if data.get("meta",{}).get("type")!=kind:
            raise InvalidDocument("entity type mismatch")
        if kind in DOCUMENTS:
            if not isinstance(data.get("applicable"),bool):
                raise InvalidDocument("posting status is missing")
            exact_minor(data.get("sum"))
        return data

    def positions(self,kind,identity):
        if kind not in POSITION_DOCUMENTS:
            raise InvalidDocument("unsupported positions resource")
        identity=uuid(identity)
        result=[]
        seen=set()
        expected=None
        # Do not return a truncated collection as if complete.
        for offset in range(0,10000,1000):
            page=self._get("/entity/"+kind+"/"+identity+"/positions",{"limit":1000,"offset":offset})
            size=page.get("meta",{}).get("size")
            rows=page.get("rows")
            if isinstance(size,bool) or not isinstance(size,int) or not 0<=size<=10000 or not isinstance(rows,list):
                raise InvalidDocument("invalid position collection")
            if expected is not None and expected!=size:
                raise RetryLater(1,"conflict")
            expected=size
            if len(rows)>1000 or len(result)+len(rows)>size:
                raise InvalidDocument("inconsistent position collection")
            for row in rows:
                if not isinstance(row,dict):
                    raise InvalidDocument("invalid position")
                rid=uuid(row.get("id"))
                if rid in seen:
                    raise RetryLater(1,"conflict")
                if "accountId" in row and uuid(row["accountId"])!=self.tenant:
                    raise InvalidDocument("position account mismatch")
                seen.add(rid)
                result.append(row)
            if len(result)==size:
                return result
            if len(rows)!=1000:
                raise RetryLater(1,"conflict")
        raise InvalidDocument("too many document positions")

    def linked(self,document,field,allowed):
        refs=document.get(field,[])
        if not isinstance(refs,list) or len(refs)>100:
            raise InvalidDocument("unsupported related-document collection")
        result=[]
        seen=set()
        for item in refs:
            if not isinstance(item,dict):
                raise InvalidDocument("invalid related-document reference")
            kind,identity=reference(item.get("meta"),allowed)
            if (kind,identity) in seen:
                raise InvalidDocument("duplicate related-document reference")
            seen.add((kind,identity))
            result.append(self.entity(kind,identity))
        return result

    def order_evidence(self,order_id):
        order_id=uuid(order_id)
        before=self.entity("customerorder",order_id)
        # These fields alone never prove full payment or full shipment.
        exact_minor(before.get("payedSum"))
        exact_minor(before.get("shippedSum"))
        lines=self.positions("customerorder",order_id)
        payments=self.linked(before,"payments",{"paymentin","cashin"})
        demands=self.linked(before,"demands",{"demand"})
        after=self.entity("customerorder",order_id)
        if before!=after:
            raise RetryLater(1,"conflict")
        # Cross-document reads are not an atomic provider snapshot. The application
        # must reconcile allocations/lines/returns and revalidate before effects.
        return OrderEvidence(before,tuple(lines),tuple(payments),tuple(demands))


@dataclass(frozen=True)
class OrderEvidence:
    order: dict
    positions: tuple
    payments: tuple
    demands: tuple
