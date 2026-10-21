import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
let session=null,version=0,working=false,pending=null,cursor=null;
let activationReady=false,setupRetryUntil=0,setupRetryTimer=null;
const status=(value,error=false)=>{$("status").textContent=value;$("status").className=error?"error":"";};
function bps(id) {
  const value=$(id).value.trim();
  if(!/^\d{1,3}(\.\d{1,2})?$/.test(value))throw new Error("Укажите процент от 0 до 100, максимум два знака после запятой.");
  const [whole,fraction=""]=value.split(".");
  const result=Number(whole)*100+Number(fraction.padEnd(2,"0"));
  if(result>10000)throw new Error("Процент не может превышать 100.");
  return result;
}
function preview() {
  try{
    const earn=bps("earn"),spend=bps("spend"),raw=$("example-total").value.trim();
    if(!/^[0-9]{1,12}$/.test(raw)||BigInt(raw)<1n)throw new Error("Укажите сумму покупки целым числом.");
    const total=BigInt(raw),fmt=new Intl.NumberFormat("ru-KZ");
    $("example-earn").textContent=fmt.format(total*BigInt(earn)/10000n);
    $("example-spend").textContent=fmt.format(total*BigInt(spend)/10000n);
    $("preview").textContent="При достаточном балансе. Начисление — после оплаты и отгрузки.";
  }catch(error){
    $("example-earn").textContent="—";$("example-spend").textContent="—";$("preview").textContent=error.message;
  }
}
function busy(value) {
  working=value;
  $("rules-form").querySelectorAll("input,textarea,button").forEach(x=>x.disabled=value);
  $("more").disabled=value;
  $("setup-widget").disabled=value||Date.now()<setupRetryUntil;
  $("activate-program").disabled=value||!activationReady;
  $("refresh-automation").disabled=value;
  $("personal-policy-form").querySelectorAll("input,select,textarea,button").forEach(x=>x.disabled=value||(!policyLoaded&&x.id!=="reload-personal-policy"));
  $("save-personal-policy").disabled=value||!policyLoaded||policyConflict;$("more-personal-policy").disabled=value;
}
async function api(path,options={}) {
  let response;
  try {response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",Authorization:"Bearer "+session}});}
  catch {throw new Error(path==="/api/admin/rules"?"Нет ответа сервера. Повторите сохранение: повтор не создаст второе изменение.":"Нет ответа сервера. Повторите действие с прежними данными.");}
  if(response.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(!response.ok){
    let data={};try{const body=await response.json();if(body&&typeof body==="object"&&!Array.isArray(body))data=body;}catch{}
    const activationMessages={
      activation_rules_required:"Сначала сохраните правила программы.",
      activation_widget_required:"Сначала подготовьте виджет в разделе «Виджет в заказе».",
      activation_configuration_changed:"Настройки изменились во время запуска. Проверьте правила и заново подтвердите условия запуска.",
      activation_request_changed:"Запрос запуска больше не соответствует установке. Обновите статус подключения.",
      activation_installation_changed:"Установка решения изменилась. Откройте БонусПро заново из МойСклад.",
      activation_already_active:"Программа уже запущена. Обновите статус подключения."
    };
    if(path==="/api/admin/activate"&&response.status===409){
      const e=new Error(activationMessages[data.error]||"Не удалось завершить запуск. Обновите статус подключения.");
      e.activationCode=Object.hasOwn(activationMessages,data.error)?data.error:null;
      throw e;
    }
    if(response.status===403)throw new Error("Нет доступа к действию. Проверьте права администратора и подключение решения.");
    if(response.status===409){
      const e=new Error(path==="/api/admin/rules"?
        "Правила изменились. Загрузите актуальные правила и проверьте значения перед сохранением.":
        "Настройки изменились. Загрузите актуальные значения.");
      e.conflict=true;throw e;
    }
    if(path==="/api/admin/activate"&&response.status===503)
      throw new Error("МойСклад временно не подтверждает запуск. Обновите статус и повторите с тем же основанием.");
    throw new Error("Не удалось выполнить действие. Проверьте значения и повторите.");
  }
  return response.json();
}
function node(tag,value){const n=document.createElement(tag);n.textContent=value;return n;}
async function history(append=false) {
  const data=await api("/api/admin/rules/history"+(append&&cursor?"?before="+cursor:""));
  if(!append)$("history").replaceChildren();
  for(const item of data.items) {
    const entry=document.createElement("article");
    entry.append(node("strong","Версия "+item.version+" · Начисление "+item.earn_bps/100+"% · Списание "+item.spend_bps/100+"%"),
      node("p",item.reason),node("p",new Date(item.created_at).toLocaleString("ru-RU")+" · Сотрудник: "+item.actor_id));
    $("history").append(entry);
  }
  cursor=data.next_cursor;$("more").hidden=!cursor;
}
async function load() {
  const {current}=await api("/api/admin/rules");
  version=current?.version||0;pending=null;
  $("earn").value=(current?.earn_bps??500)/100;$("spend").value=(current?.spend_bps??3000)/100;
  $("reason").value=current?"":"Первоначальная настройка";
  preview();
  status(current?"Действует версия "+version+".":"Сохраните первоначальные правила.");
  await history();
}
async function connect(){
  $("retry").hidden=true;$("workspace").hidden=true;session=null;pending=null;activationReady=false;policyPending=null;policyLoaded=false;policyConflict=false;$("activation-status").textContent="";busy(true);
  status("Подключаемся к МойСклад…");
  try {
    const token=await requestContext();
    const response=await fetch("/api/session",{method:"POST",credentials:"omit",cache:"no-store",
      signal:AbortSignal.timeout(15000),headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
    if(!response.ok)throw new Error("Не удалось подтвердить доступ. Откройте раздел заново из МойСклад.");
    session=(await response.json()).token;
    const user=await api("/api/me");
    if(user.role!=="admin")throw new Error("Этот раздел доступен администратору МойСклад.");
    await load();try{await loadPolicy();}catch(error){policyStatus("Не удалось загрузить настройки скидок. "+error.message,true);}
    await updateReadiness();$("workspace").hidden=false;
  } catch(error){status(error.message,true);$("retry").hidden=false;}
  finally {busy(false);}
}
$("rules-form").addEventListener("submit",async event=>{
  event.preventDefault();if(working)return;
  let payload;
  try {
    const reason=$("reason").value.trim();if(!reason)throw new Error("Укажите причину изменения.");
    payload={earn_bps:bps("earn"),spend_bps:bps("spend"),delivery_policy:"exclude",expected_version:version,reason};
  } catch(error){status(error.message,true);return;}
  const signature=JSON.stringify(payload);
  if(!pending||pending.signature!==signature)pending={signature,id:crypto.randomUUID()};
  busy(true);status("Сохраняем правила…");
  try {
    const {current}=await api("/api/admin/rules",{method:"PUT",body:JSON.stringify({...payload,request_id:pending.id})});
    version=current.version;pending=null;$("reason").value="";
    status("Правила сохранены. Версия "+version+".");
    try{await history();await updateReadiness();}catch{status("Правила сохранены. Историю не удалось обновить — загрузите актуальные правила.");}
  } catch(error){if(error.conflict)pending=null;status(error.message,true);}
  finally {busy(false);}
});
$("reload").onclick=async()=>{if(working)return;busy(true);try{await load();}catch(e){status(e.message,true);}finally{busy(false);}};
$("more").onclick=async()=>{if(working)return;busy(true);try{await history(true);}catch(e){status(e.message,true);}finally{busy(false);}};
$("earn").oninput=preview;$("spend").oninput=preview;$("retry").onclick=connect;
function setupCooldown(seconds) {
  if(setupRetryTimer!==null)clearTimeout(setupRetryTimer);
  setupRetryUntil=Date.now()+seconds*1000;
  const update=()=>{
    const remaining=Math.max(0,Math.ceil((setupRetryUntil-Date.now())/1000));
    $("setup-widget").textContent=remaining?"Повторить через "+remaining+" с":"Подготовить виджет";
    $("setup-widget").disabled=working||remaining>0;
    setupRetryTimer=remaining?setTimeout(update,Math.min(1000,Math.max(1,setupRetryUntil-Date.now()))):null;
  };
  update();
}
$("setup-widget").onclick=async()=>{
  if(working||Date.now()<setupRetryUntil)return;busy(true);$("setup-status").textContent="Проверяем подключение…";
  try{
    const response=await fetch("/api/admin/widget/setup",{method:"POST",credentials:"omit",cache:"no-store",
      headers:{"Content-Type":"application/json",Authorization:"Bearer "+session},signal:AbortSignal.timeout(35000)});
    if(!response.ok){
      let data={};try{const body=await response.json();if(body&&typeof body==="object"&&!Array.isArray(body))data=body;}catch{}
      if(response.status===401){
        session=null;$("workspace").hidden=true;$("retry").hidden=false;
        status("Сеанс завершён. Подключитесь снова и повторите настройку виджета.",true);
        throw new Error("Сеанс завершён. Подключитесь снова и повторите настройку виджета.");
      }
      if(response.status===503&&data.error==="widget_metadata_temporarily_unavailable"){
        const header=response.headers.get("Retry-After")||"";
        const retry=Number.isSafeInteger(data.retry_after)&&data.retry_after>=1&&data.retry_after<=86400?data.retry_after:
          (/^[0-9]{1,5}$/.test(header)&&Number(header)>=1&&Number(header)<=86400?Number(header):5);
        setupCooldown(retry);
        const messages={
          rate_limited:"МойСклад ограничил запросы. Повторите через "+retry+" с. Код BP-WIDGET-RATE.",
          timeout:"МойСклад не ответил вовремя. Повторите через "+retry+" с. Код BP-WIDGET-TIMEOUT.",
          conflict:"МойСклад ещё обрабатывает настройку поля. Повторите через "+retry+" с. Код BP-WIDGET-RETRY.",
          unavailable:"МойСклад временно недоступен. Повторите через "+retry+" с. Код BP-WIDGET-PROVIDER."
        };
        throw new Error(messages[data.retry_reason]||messages.unavailable);
      }
      const messages={
        moysklad_token_rejected:"МойСклад отклонил доступ решения. Обновите установленное решение и подключитесь снова. Код BP-WIDGET-TOKEN.",
        moysklad_permissions_required:"МойСклад запретил доступ к служебному полю. Проверьте права установленного решения и ограничения аккаунта. Код BP-WIDGET-ACCESS.",
        widget_setup_in_progress:"Подключение уже выполняется в другом окне. Подождите и повторите проверку.",
        widget_metadata_temporarily_unavailable:"МойСклад временно не отвечает или ограничил запросы. Подождите и повторите; уже созданное поле будет найдено.",
        widget_metadata_invalid:"МойСклад вернул неподходящее или неоднозначное служебное поле. Требуется проверка настройки. Код BP-WIDGET-METADATA.",
        widget_binding_changed:"Служебное поле отличается от ранее подключённого. Не удаляйте поля и не повторяйте списания; нужна проверка администратора. Код BP-WIDGET-BINDING."
      };
      const codes=Array.isArray(data.provider_error_codes)?[...new Set(data.provider_error_codes.slice(0,64)
        .filter(code=>Number.isSafeInteger(code)&&code>=0&&code<=999999))].sort((a,b)=>a-b).slice(0,8):[];
      const diagnostic=codes.length?" Коды API МойСклад: "+codes.join(", ")+".":"";
      throw new Error((messages[data.error]||(response.status===403?
        "Доступ к настройке отклонён. Проверьте права администратора и установленного решения. Код BP-WIDGET-ACCESS.":
        "Не удалось подключить виджет. Код BP-WIDGET-HTTP-"+response.status+". Повторите проверку."))+diagnostic);
    }
    $("setup-status").textContent="Виджет подготовлен. Это не включает рабочие списания.";
    await updateReadiness();
  }catch(error){$("setup-status").textContent=error.name==="TimeoutError"?"Ответ задерживается. Повторная проверка найдёт уже созданное поле.":error.message;}
  finally{busy(false);}
};

const outcomes={queued:"Ожидают проверки",waiting_payment:"Ожидают полной оплаты",waiting_shipment:"Ожидают полной отгрузки",
  credited:"Начисление выполнено",historical:"До начала программы",cancelled:"Отменены",return_review:"Возвраты на проверке",
  document_review:"Требуют разбора",redemption_pending:"Проверяется списание",provider_error:"Не удалось проверить"};
async function automationStatus(){
  const data=await api("/api/admin/automation");
  const config=data.configuration;
  $("automation-status").textContent=!config?"Начало программы ещё не зафиксировано.":
    "Начало программы: "+new Date(config.starts_at).toLocaleString("ru-RU")+". "+
    (config.heartbeat_at?"Последняя обработка: "+new Date(config.heartbeat_at).toLocaleString("ru-RU")+".":"Обработчик ещё не запускался.")+
    (config.last_error?" Есть ошибка синхронизации.":"");
  $("automation-details").replaceChildren();
  for(const [key,count] of Object.entries(data.orders||{}))
    $("automation-details").append(node("p",(outcomes[key]||"На проверке")+": "+count));
  if(data.jobs?.dead)$("automation-details").append(node("p","Заданий с исчерпанными попытками: "+data.jobs.dead+". Нужна проверка подключения и документов."));
  if(data.attention?.pending_returns)$("automation-details").append(node("p","Возвратов на решении администратора: "+data.attention.pending_returns+". Откройте «Возвраты и сверка»."));
  if(data.attention?.stale_reservations)$("automation-details").append(node("p","Списаний без подтверждения более 15 минут: "+data.attention.stale_reservations+". Резервы сохранены; проверьте исходные заказы."));
  if(config?.heartbeat_at&&Date.now()-Date.parse(config.heartbeat_at)>600000)
    $("automation-details").append(node("p","Обработка не обновлялась более 10 минут. Проверьте фоновый обработчик."));
  for(const item of data.unverified||[]){
    const line=node("p",item.points===0?"Персональная скидка — ожидает подтверждения · ":"Ожидает подтверждения: "+item.points+" бонусов · ");
    const link=node("a","Открыть заказ");link.href="https://online.moysklad.ru/app/#customerorder/edit?id="+encodeURIComponent(item.order_id);
    link.target="_blank";link.rel="noopener noreferrer";line.append(link);$("automation-details").append(line);
  }
  if(data.issues?.length)$("automation-details").append(node("p","Есть расхождения в документах. Списание по затронутым клиентам приостановлено до разбора."));
}
$("refresh-automation").onclick=async()=>{
  if(working)return;busy(true);
  try{await automationStatus();}catch(e){$("automation-status").textContent=e.message;}
  finally{busy(false);}
};

let activationRequest=null;
$("activate-program").onclick=async()=>{
  if(working||!activationReady)return;
  const reason=$("activation-reason").value.trim();
  if(!$("balances-reviewed").checked||!$("previous-disabled").checked||!reason){
    $("activation-status").textContent="Подтвердите сверку остатков, отключение прежней программы и укажите основание.";return;
  }
  if(activationRequest&&activationRequest.reason!==reason){
    $("activation-status").textContent="Для повтора оставьте прежнее основание или обновите страницу и проверьте статус.";return;
  }
  activationRequest??={request_id:crypto.randomUUID(),reason,balances_reviewed:true,previous_program_disabled:true};
  busy(true);$("activate-program").disabled=true;
  try{
    const result=await api("/api/admin/activate",{method:"POST",body:JSON.stringify(activationRequest)});
    activationReady=false;$("activate-program").hidden=true;
    $("activation-status").textContent="Программа запущена. Начало начислений: "+new Date(result.starts_at).toLocaleString("ru-RU")+". Проверьте работу обработчика ниже.";
    try{await automationStatus();await updateReadiness();}
    catch{$("activation-status").textContent+=" Статус обработки пока недоступен; обновите его отдельно.";}
  }catch(e){
    if(e.activationCode){
      $("activation-status").textContent=e.message;
      if(["activation_configuration_changed","activation_request_changed"].includes(e.activationCode)){
        activationRequest=null;$("balances-reviewed").checked=false;$("previous-disabled").checked=false;
      }
    }else $("activation-status").textContent="Запуск не подтверждён. Проверьте статус подключения и повторите с тем же основанием. "+e.message;
    try{await updateReadiness();}catch{}
  }
  finally{busy(false);}
};

async function updateReadiness(){
  const message=$("readiness-state"),list=$("readiness-list");
  message.textContent="Проверяем подключение…";
  try{
    const data=await api("/api/admin/readiness");list.replaceChildren();
    for(const item of data.checks){
      const li=document.createElement("li");li.dataset.complete=String(item.complete);
      const link=document.createElement("a");link.href="#"+item.target;
      const mark=document.createElement("span");mark.className="readiness-mark";mark.setAttribute("aria-hidden","true");mark.textContent=item.complete?"✓":"·";
      const label=document.createElement("span");label.textContent=item.label+(item.complete?" — выполнено":" — требуется действие");
      link.append(mark,label);li.append(link);list.append(li);
    }
    message.textContent=data.ready?"Все подключения выполнены. Автоматическая обработка отвечает.":
      "Завершите отмеченные шаги. Статус отражает фактическое подключение.";
    const active=data.checks.find(item=>item.id==="activation")?.complete;
    const rulesReady=data.checks.some(item=>item.id==="rules"&&item.complete===true);
    const widgetReady=data.checks.some(item=>item.id==="widget"&&item.complete===true);
    activationReady=rulesReady&&widgetReady&&!active;
    $("activation-prerequisites").hidden=Boolean(active)||activationReady;
    $("activation-prerequisites").textContent=!rulesReady&&!widgetReady?"Сначала сохраните правила и подготовьте виджет.":
      !rulesReady?"Сначала сохраните правила программы.":"Сначала подготовьте виджет.";
    $("activate-program").disabled=working||!activationReady;
    $("activate-program").hidden=Boolean(active);
    if(active&&!$("activation-status").textContent.startsWith("Программа запущена."))$("activation-status").textContent="Программа уже запущена. Повторный запуск не требуется.";
  }catch(error){
    activationReady=false;$("activate-program").disabled=true;
    $("activation-prerequisites").hidden=false;$("activation-prerequisites").textContent="Обновите статус подключения перед запуском.";
    list.replaceChildren();message.textContent="Не удалось проверить подключение. Обновите статус.";
    if(!session)throw error;
  }
}
$("refresh-readiness").onclick=async()=>{
  if(working)return;busy(true);
  try{await updateReadiness();}catch(e){status(e.message,true);}finally{busy(false);}
};

$("example-total").addEventListener("input",preview);

const policyPath="/api/admin/personal-discounts/policy";
const policyLabels={skip:"Персональная скидка не применяется при скидке в позиции",best:"Выбирается больший процент скидки",sequential:"Скидки применяются последовательно"};
let policyVersion=0,policyPending=null,policyCursor=null,policyLoaded=false,policyConflict=false;
function policyStatus(message,error=false){
  $("personal-policy-status").textContent=message;$("personal-policy-status").classList.toggle("error",error);
}
function policyPreview(){
  const examples={skip:"10 000 ₸ − 10% = 9 000 ₸. Персональные 20% не применяются.",
    best:"10 000 ₸ − 20% = 8 000 ₸. Выбран больший процент: 20% вместо 10%.",
    sequential:"10 000 ₸ × 90% × 80% = 7 200 ₸. Персональные 20% применяются после скидки 10%."};
  $("personal-example").textContent="Пример: цена 10 000 ₸, скидка позиции 10%, персональная 20%. "+examples[$("personal-external").value];
  $("personal-stack-note").textContent=$("personal-stack").checked?
    "Лимит списания рассчитывается от товаров после персональной скидки.":
    "При применении персональной скидки списать бонусы нельзя.";
}
async function policyHistory(append=false){
  const data=await api(policyPath+"/history"+(append&&policyCursor?"?before="+encodeURIComponent(policyCursor):""));
  if(!append)$("personal-policy-history").replaceChildren();
  for(const item of data.items){
    const entry=node("article","");
    entry.append(node("strong","Версия "+item.version+" · "+(item.enabled?"Включены":"Выключены")),
      node("p",policyLabels[item.external_policy]+". Бонусы вместе со скидкой: "+(item.allow_bonus_stack?"да":"нет")+"."),
      node("p",item.reason),node("p",new Date(item.created_at).toLocaleString("ru-RU")+" · Сотрудник: "+item.actor_id));
    $("personal-policy-history").append(entry);
  }
  if(!$("personal-policy-history").childElementCount)$("personal-policy-history").append(node("p","Настройки персональных скидок ещё не менялись."));
  policyCursor=data.next_cursor;$("more-personal-policy").hidden=!policyCursor;
}
async function loadPolicy(){
  const {current}=await api(policyPath);
  policyVersion=current?.version||0;policyPending=null;policyConflict=false;policyLoaded=true;
  $("personal-enabled").checked=current?.enabled===true;$("personal-external").value=current?.external_policy||"skip";
  $("personal-stack").checked=current?.allow_bonus_stack===true;$("personal-policy-reason").value="";
  policyPreview();policyStatus("Персональные скидки "+(current?.enabled?"включены.":"выключены.")+" Версия "+policyVersion+".");
  try{await policyHistory();}catch(e){policyStatus("Настройки загружены. Историю не удалось обновить. "+e.message,true);}
}
$("personal-policy-form").addEventListener("submit",async event=>{
  event.preventDefault();if(working||!policyLoaded||policyConflict)return;
  const reason=$("personal-policy-reason").value.trim();
  if(!reason){policyStatus("Укажите причину изменения.",true);return;}
  const payload={enabled:$("personal-enabled").checked,external_policy:$("personal-external").value,
    allow_bonus_stack:$("personal-stack").checked,expected_version:policyVersion,reason};
  const signature=JSON.stringify(payload);
  if(!policyPending||policyPending.signature!==signature)policyPending={signature,id:crypto.randomUUID()};
  busy(true);policyStatus("Сохраняем настройки скидок…");
  try{
    const {current}=await api(policyPath,{method:"PUT",body:JSON.stringify({...payload,request_id:policyPending.id})});
    policyVersion=current.version;policyPending=null;$("personal-policy-reason").value="";
    policyStatus("Настройки скидок сохранены. Версия "+policyVersion+".");
    try{await policyHistory();}catch{policyStatus("Настройки скидок сохранены. Историю не удалось обновить.",true);}
  }catch(e){
    if(e.conflict){policyPending=null;policyConflict=true;policyStatus("Настройки скидок изменились. Загрузите актуальные настройки и проверьте значения.",true);}
    else policyStatus(e.message,true);
  }finally{busy(false);}
});
$("reload-personal-policy").onclick=async()=>{
  if(working)return;busy(true);try{await loadPolicy();}catch(e){policyStatus(e.message,true);}finally{busy(false);}
};
$("more-personal-policy").onclick=async()=>{
  if(working)return;busy(true);try{await policyHistory(true);}catch(e){policyStatus(e.message,true);}finally{busy(false);}
};
$("personal-external").onchange=policyPreview;$("personal-stack").onchange=policyPreview;
policyPreview();

connect();
