"""Webhook capability and event parsing. Events are hints, never financial proof."""
import hashlib,hmac,json,re
from bonuspro.security import AuthenticationFailed
from bonuspro.adapters.moysklad.remap import reference,uuid,InvalidDocument
KINDS=("customerorder","demand","salesreturn","paymentin","paymentout","cashin","cashout")
ACTIONS=("CREATE","UPDATE","DELETE")

def signature(key,app,tenant,generation):
    value="bonuspro/remap-hook/v1/"+uuid(app)+"/"+uuid(tenant)+"/"+uuid(generation)
    return hmac.new(key,value.encode(),hashlib.sha256).hexdigest()

def verify_signature(key,app,tenant,generation,provided):
    if not isinstance(provided,str) or not re.fullmatch("[0-9a-f]{64}",provided):
        raise AuthenticationFailed("invalid webhook capability")
    if not hmac.compare_digest(signature(key,app,tenant,generation),provided):
        raise AuthenticationFailed("invalid webhook capability")

def parse_events(body,tenant):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("duplicate property")
            result[key]=value
        return result
    try:
        data=json.loads(body,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        values=data.get("events") if isinstance(data,dict) else None
        if not isinstance(values,list) or not 1<=len(values)<=100:raise ValueError()
        result=[]
        for value in values:
            if not isinstance(value,dict) or uuid(value.get("accountId"))!=tenant:raise ValueError()
            kind,identity=reference(value.get("meta"),set(KINDS))
            action=value.get("action")
            if action not in ACTIONS:raise ValueError()
            result.append(dict(kind=kind,id=identity,action=action))
        return result
    except (ValueError,TypeError,UnicodeError,RecursionError,InvalidDocument):
        raise InvalidDocument("invalid webhook envelope") from None

def affected_orders(client,kind,identity):
    """Resolve current references on the fixed API host; deleted links use the DB index."""
    if kind=="customerorder":return {uuid(identity)}
    doc=client.entity(kind,identity)
    refs=[]
    if kind=="demand":
        refs=[doc.get("customerOrder")] if doc.get("customerOrder") else []
    elif kind=="salesreturn":
        refs=[doc.get("demand")] if doc.get("demand") else []
    else:
        refs=doc.get("operations",[])
        if not isinstance(refs,list) or len(refs)>100:raise InvalidDocument("unsupported payment operations")
    result=set()
    for value in refs:
        if not isinstance(value,dict):raise InvalidDocument("invalid operation reference")
        meta=value.get("meta",{})
        if meta.get("type") not in ("customerorder","demand","salesreturn"):continue
        allowed={"customerorder"} if kind=="demand" else ({"demand"} if kind=="salesreturn" else {"customerorder","demand","salesreturn"})
        target,identity=reference(meta,allowed)
        if target=="customerorder":result.add(identity)
        elif target=="demand":result.update(affected_orders(client,"demand",identity))
        elif target=="salesreturn" and kind in ("paymentin","paymentout","cashin","cashout"):
            result.update(affected_orders(client,"salesreturn",identity))
    return result
