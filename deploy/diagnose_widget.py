"""Read finite widget metadata diagnostics from the installed app without rebuilding it."""
import argparse
import json
import os
import subprocess
from uuid import UUID

from start_processing import installation_rows,select_tenant,ProcessingFailed

CHILD_CODE=r"""
import json,sys
from contextlib import closing

def diagnose():
    import httpx,psycopg
    from bonuspro.settings import Settings
    from bonuspro.security import AuthenticationFailed
    from bonuspro.adapters.postgres.installations import Installations
    from bonuspro.adapters.postgres.redemptions import Redemptions
    from bonuspro.adapters.moysklad import remap as remap_module
    from bonuspro.adapters.moysklad.remap import (
        RemapClient,RemapError,AccessDenied,InvalidDocument,EntityMissing,RetryLater,_access_error_codes,
    )
    tenant=sys.argv[1];prepare=sys.argv[2]=="prepare"
    report={"read_only":not prepare,"requests":[],"result":"diagnostic_unavailable"}
    original_send=httpx.Client.send
    provider=None
    def send(client,request,*args,**kwargs):
        # Never instrument or send an unrelated resource, URL or caller payload.
        if (request.url.scheme!="https" or request.url.host!="api.moysklad.ru"
            or request.url.path!="/api/remap/1.2/entity/customerorder/metadata/attributes"
            or request.method not in ("GET","POST") or (request.method=="POST" and not prepare)):
            raise InvalidDocument("unexpected diagnostic request")
        item={"phase":"metadata_read" if request.method=="GET" else "metadata_create","http":None,"codes":[]}
        report["requests"].append(item)
        response=original_send(client,request,*args,**kwargs)
        item["http"]=response.status_code
        if response.status_code!=200:
            try:
                details=getattr(remap_module,"_provider_error_details",None)
                if callable(details):
                    codes,concurrent_only=details(response,getattr(provider,"deadline",None))
                else:
                    codes=_access_error_codes(response,getattr(provider,"deadline",None))
                    concurrent_only=False
                item["codes"]=list(codes)
            finally:
                response.close()
            # Retain unknown-error evidence: filtering numeric codes alone must
            # not turn a mixed validation response into an exclusive1065 proof.
            errors=[{"code":code} for code in item["codes"]]
            if concurrent_only is not True or item["codes"]!=[1065]:errors.append({})
            return httpx.Response(response.status_code,json={"errors":errors},request=request)
        return response
    try:
        config=Settings.from_env()
        repo=Installations(config.database_url,config.lifecycle_database_url,config.credentials)
        # Same session advisory lock as the application's configure_marker.
        with closing(psycopg.connect(config.database_url,autocommit=True,connect_timeout=5)) as guard:
            guard.execute("SET statement_timeout='5s'")
            locked=guard.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                 ("widget/provision/"+tenant,)).fetchone()[0]
            if not locked:
                report["result"]="setup_in_progress"
            else:
                provider=RemapClient(repo.remap_token(tenant),tenant,budget_seconds=40)
                binding=Redemptions(repo.ledger).binding(tenant)
                httpx.Client.send=send
                marker=provider.marker_attribute(create=prepare and binding is None)
                report["result"]="marker_valid" if binding is None or marker==binding else "binding_mismatch"
    except AccessDenied:
        report["result"]="access_denied"
    except (InvalidDocument,EntityMissing):
        report["result"]="metadata_error"
    except RetryLater:
        report["result"]="transient_error"
    except AuthenticationFailed:
        report["result"]="installation_inactive"
    except RemapError:
        # Updated images may classify permanent metadata errors separately.
        # Do not import a new exception absent from the installed legacy image.
        report["result"]="metadata_error"
    except Exception:
        # Never include exception text, response bodies, headers or identities.
        report["result"]="diagnostic_unavailable"
    finally:
        httpx.Client.send=original_send
    print(json.dumps(report,separators=(",",":")))
    return 0 if report["result"]=="marker_valid" else 1

raise SystemExit(diagnose())
"""

RESULTS=frozenset(("marker_valid","access_denied","metadata_error","transient_error",
                   "installation_inactive","setup_in_progress","binding_mismatch","diagnostic_unavailable"))
PHASES=frozenset(("metadata_read","metadata_create"))

class DiagnosticFailed(RuntimeError):pass

def tenant_uuid(value):
    try:return str(UUID(value))
    except (ValueError,TypeError,AttributeError):
        raise argparse.ArgumentTypeError("Укажите корректный UUID аккаунта") from None

def safe_report(raw,*,prepare):
    if not isinstance(raw,str) or len(raw)>16384:raise DiagnosticFailed()
    try:report=json.loads(raw)
    except (ValueError,TypeError):raise DiagnosticFailed() from None
    if (not isinstance(report,dict) or report.get("read_only") is not (not prepare)
        or not isinstance(report.get("result"),str) or report["result"] not in RESULTS
        or not isinstance(report.get("requests"),list)
        or len(report["requests"])>4):raise DiagnosticFailed()
    requests=[]
    for item in report["requests"]:
        if not isinstance(item,dict) or item.get("phase") not in PHASES:raise DiagnosticFailed()
        status=item.get("http");codes=item.get("codes")
        if status is not None and (type(status) is not int or not 100<=status<=599):raise DiagnosticFailed()
        if (not isinstance(codes,list) or len(codes)>8
            or any(type(code) is not int or not 0<=code<=999999 for code in codes)):
            raise DiagnosticFailed()
        if not prepare and item["phase"]=="metadata_create":raise DiagnosticFailed()
        requests.append({"phase":item["phase"],"http":status,"codes":sorted(set(codes))})
    return {"read_only":not prepare,"requests":requests,"result":report["result"]}

def diagnose(tenant=None,*,prepare=False,runner=subprocess.run):
    selected=select_tenant(installation_rows(),tenant)
    completed=runner(["docker","exec","-i","bonuspro-release-app-1","python","-",selected,
                      "prepare" if prepare else "read_only"],
                     input=CHILD_CODE,text=True,capture_output=True,timeout=75,check=False)
    report=safe_report(completed.stdout,prepare=prepare)
    if (completed.returncode==0)!=(report["result"]=="marker_valid"):raise DiagnosticFailed()
    return report

def main():
    parser=argparse.ArgumentParser(description="Диагностика служебного поля: HTTP и числовые коды, без секретов")
    parser.add_argument("--tenant",type=tenant_uuid,help="UUID аккаунта; по умолчанию единственная действующая установка")
    parser.add_argument("--prepare",action="store_true",help="Разрешить установленному адаптеру создать своё поле; привязка в БонусПро выполняется затем кнопкой в интерфейсе")
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Запустите diagnose_widget.py с sudo")
    try:
        report=diagnose(args.tenant,prepare=args.prepare)
    except ProcessingFailed:
        report={"read_only":not args.prepare,"requests":[],"result":"account_selection_required",
                "hint":"Укажите --tenant UUID действующей установки; автоматически выбирается только единственная"}
    except Exception:
        report={"read_only":not args.prepare,"requests":[],"result":"diagnostic_unavailable"}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report["result"]=="marker_valid" else 1)

if __name__=="__main__":main()
