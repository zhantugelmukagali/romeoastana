import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
let session=null,version=0,working=false,pending=null,cursor=null;
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
  try {const earn=bps("earn"),spend=bps("spend");
    $("preview").textContent="Пример: за товары на 10 000 ₸ — "+(earn)+" бонусов после оплаты и отгрузки. Можно списать до "+(spend)+" бонусов при достаточном балансе.";
  } catch {$("preview").textContent="";}
}
function busy(value) {
  working=value;
  $("rules-form").querySelectorAll("input,textarea,button").forEach(x=>x.disabled=value);
  $("more").disabled=value;
  $("setup-widget").disabled=value;
  $("refresh-automation").disabled=value;
}
async function api(path,options={}) {
  let response;
  try {response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",Authorization:"Bearer "+session}});}
  catch {throw new Error("Нет ответа сервера. Повторите сохранение: повтор не создаст второе изменение.");}
  if(response.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(response.status===403)throw new Error("Нужны права администратора МойСклад.");
  if(response.status===409){const e=new Error("Правила изменились. Загрузите актуальные правила и проверьте значения перед сохранением.");e.conflict=true;throw e;}
  if(!response.ok)throw new Error("Не удалось выполнить действие. Проверьте значения и повторите.");
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
  $("retry").hidden=true;$("workspace").hidden=true;session=null;pending=null;$("activation-status").textContent="";busy(true);
  status("Подключаемся к МойСклад…");
  try {
    const token=await requestContext();
    const response=await fetch("/api/session",{method:"POST",credentials:"omit",cache:"no-store",
      signal:AbortSignal.timeout(15000),headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
    if(!response.ok)throw new Error("Не удалось подтвердить доступ. Откройте раздел заново из МойСклад.");
    session=(await response.json()).token;
    const user=await api("/api/me");
    if(user.role!=="admin")throw new Error("Этот раздел доступен администратору МойСклад.");
    await load();await updateReadiness();$("workspace").hidden=false;
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
connect();

$("setup-widget").onclick=async()=>{
  if(working)return;busy(true);$("setup-status").textContent="Проверяем подключение…";
  try{
    const response=await fetch("/api/admin/widget/setup",{method:"POST",credentials:"omit",cache:"no-store",
      headers:{"Content-Type":"application/json",Authorization:"Bearer "+session},signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error(response.status===403?"Нужны права администратора и доступ решения к своим дополнительным полям.":"Не удалось подготовить виджет. Проверьте подключение и повторите.");
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
    const line=node("p","Ожидает подтверждения: "+item.points+" бонусов · ");
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
  if(working)return;
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
    $("activation-status").textContent="Программа запущена. Начало начислений: "+new Date(result.starts_at).toLocaleString("ru-RU")+". Проверьте работу обработчика ниже.";
    await automationStatus();await updateReadiness();
  }catch(e){$("activation-status").textContent="Запуск не подтверждён. Проверьте статус подключения и повторите с тем же основанием. "+e.message;}
  finally{busy(false);$("activate-program").disabled=false;}
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
    $("activate-program").hidden=Boolean(active);
    if(active&&!$("activation-status").textContent)$("activation-status").textContent="Программа уже запущена. Повторный запуск не требуется.";
  }catch(error){
    list.replaceChildren();message.textContent="Не удалось проверить подключение. Обновите статус.";
    if(!session)throw error;
  }
}
$("refresh-readiness").onclick=async()=>{
  if(working)return;busy(true);
  try{await updateReadiness();}catch(e){status(e.message,true);}finally{busy(false);}
};
