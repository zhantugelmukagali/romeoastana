"""One command to update the compatible release, worker and read-only account checks."""
import argparse
from datetime import datetime,timezone
import json,os
from pathlib import Path
import subprocess,sys
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parent))
from start_processing import installation_rows,select_tenant,ProcessingFailed

class DeliveryFailed(RuntimeError):pass

def resolve_release_tenant(tenant=None):
    if tenant is not None:return str(UUID(tenant))
    try:return select_tenant(installation_rows())
    except ProcessingFailed as error:raise DeliveryFailed(str(error)) from None

def finish(root,source,tenant=None,*,runner=subprocess.run,replace_inactive_worker=False,prepare_only=False):
    # Preparation updates the shared application without selecting an account.
    # A complete release resolves once before update and keeps that account.
    tenant=(str(UUID(tenant)) if tenant is not None else None) if prepare_only else resolve_release_tenant(tenant)
    root=Path(root);source=Path(source)
    print(("1/1" if prepare_only else "1/3")+" Обновляем программу бонусов и скидок, проверяем восстановление копии.",flush=True)
    update=[sys.executable,str(source/"deploy/update_cabinet.py"),"--root",str(root),"--defer-processing"]
    if tenant is not None:update.extend(["--tenant",tenant])
    runner(update,check=True)
    if prepare_only:
        print("APPLICATION_UPDATED_SETUP_PENDING: приложение обновлено. Откройте «Настройки» приложения в МойСклад, сохраните правила и настройку виджета, затем запустите программу.",flush=True)
        return True
    print("2/3 Обновляем обработчик и ждём свежего результата.",flush=True)
    processing=[sys.executable,str(source/"deploy/start_processing.py"),"--root",str(root),"--tenant",tenant]
    if replace_inactive_worker:processing.append("--replace-inactive-worker")
    runner(processing,check=True,timeout=900)
    print("3/3 Проверяем реальные документы без изменения бонусов.",flush=True)
    result=runner(["docker","exec","bonuspro-release-app-1","python","-m","bonuspro.releasecheck",
                   "--tenant",tenant],check=False,capture_output=True,text=True,timeout=120)
    try:report=json.loads(result.stdout)
    except (ValueError,TypeError):raise DeliveryFailed("ACCOUNT_CHECK_NOT_CONFIRMED: no valid check report") from None
    if (not isinstance(report,dict) or report.get("read_only") is not True or
        report.get("business_acceptance_verified") is not False or
        type(report.get("ready_for_live_acceptance")) is not bool):
        raise DeliveryFailed("ACCOUNT_CHECK_NOT_CONFIRMED: unexpected report")
    directory=root/".release-reports"
    if directory.is_symlink():raise DeliveryFailed("Report directory must be regular")
    directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/("check-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid())+".json")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as output:
        json.dump(report,output,ensure_ascii=False,indent=2);output.write("\n")
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    print("Отчёт проверки: "+str(path),flush=True)
    if result.returncode==0 and report["ready_for_live_acceptance"]:
        print("READY_FOR_ACCEPTANCE: технические проверки пройдены; финансовую приёмку выполняют в МойСклад.",flush=True)
        return True
    print("RELEASE_NEEDS_ATTENTION: проверка обнаружила незавершённую настройку или неподдерживаемые документы.",flush=True)
    return False

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path("/home/ubuntu/bonuspro-release"))
    parser.add_argument("--prepare-only",action="store_true",help="Update only the shared application; complete account settings before starting processing")
    parser.add_argument("--tenant",help="Selected account UUID; otherwise require exactly one enabled installation")
    parser.add_argument("--replace-inactive-worker",action="store_true",help="Explicitly hand off a managed worker from an inactive previous installation")
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit("Run finish_release.py with sudo")
    os.umask(0o077)
    try:
        success=finish(args.root.resolve(),Path(__file__).resolve().parents[1],args.tenant,replace_inactive_worker=args.replace_inactive_worker,prepare_only=args.prepare_only)
    except DeliveryFailed as error:raise SystemExit(str(error)) from None
    except Exception:raise SystemExit("RELEASE_NOT_CONFIRMED: процесс остановлен; результат предыдущего шага показан выше") from None
    raise SystemExit(0 if success else 1)

if __name__=="__main__":main()
