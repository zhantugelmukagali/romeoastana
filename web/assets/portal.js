import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
const fmt=new Intl.NumberFormat("ru-KZ");
const number=value=>fmt.format(BigInt(value));
const date=value=>new Date(value).toLocaleString("ru-RU",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
const labels={credit:"Начисление",opening:"Перенос остатков",redemption:"Списание",presale_refund:"Возврат списания",reversal:"Отмена начисления",refund:"Возврат бонусов",settlement:"Пересчёт",adjustment:"Корректировка"};
let session=null,connecting=false,summary=null,screen="overview",connectionEpoch=0;
let customerOffset=null,activityCursor=null,customerQuery="",customersBusy=false,activityBusy=false;
let customersLoaded=false,activityLoaded=false,searchEpoch=0,profileEpoch=0,profileId=null,profileCursor=null,profileBusy=false;
let personalAssignment=null,personalPending=null,personalSaving=false,personalConflict=false,personalArchived=false;
let recalculationCursor=null,recalculationsBusy=false;
const node=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
function status(text,error=false){$("status").textContent=text;$("status").classList.toggle("error",error);}
async function api(path,options={}){
  const generation=connectionEpoch;
  let response;
  try{response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",...(session?{Authorization:"Bearer "+session}:{})}});}
  catch{throw new Error("Нет ответа сервера. Повторите загрузку.");}
  if(generation!==connectionEpoch)throw new Error("Подключение обновилось. Повторите загрузку.");
  if(response.status===401){connectionEpoch++;session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(response.status===403)throw new Error("Кабинет доступен администратору. Проверьте права и доступ к МойСклад.");
  if(response.status===404)throw new Error("Клиент или страница истории больше не доступны. Обновите данные.");
  if(response.status===409){
    let payload={};try{payload=await response.json();}catch{}
    if(generation!==connectionEpoch)throw new Error("Подключение обновилось. Повторите загрузку.");
    const archived=payload.error==="customer_archived";
    const error=new Error(archived?"Клиент в архиве МойСклад. Восстановите его в МойСклад и откройте карточку заново.":
      "Скидка уже изменена. Загрузите актуальную скидку и проверьте значения.");
    error.conflict=!archived;error.archived=archived;throw error;
  }
  if(!response.ok)throw new Error("Не удалось выполнить действие. Проверьте значения и повторите.");
  const data=await response.json();
  if(generation!==connectionEpoch)throw new Error("Подключение обновилось. Повторите загрузку.");
  return data;
}
function empty(target,title,description){
  const box=node("div","","empty-state");box.append(node("h3",title),node("p",description));target.replaceChildren(box);
}
function activityRows(target,items,append=false){
  if(!append)target.replaceChildren();
  for(const item of items){
    const row=node("div","","activity-row"),icon=node("span",item.points>0?"+":"−","operation-icon "+(item.points>0?"positive":"negative"));
    const info=node("div","","operation-info");info.append(node("strong",labels[item.kind]||"Пересчёт бонусов"),node("span",date(item.created_at),"muted"));
    const amount=node("strong",(item.points>0?"+":"−")+number(Math.abs(item.points)),"operation-amount "+(item.points>0?"positive":""));
    row.append(icon,info,amount);
    if(item.customer_id&&target.id!=="customer-history"){const b=node("button","Клиент","text-button");b.onclick=()=>void openCustomer(item.customer_id);row.append(b);}
    if(item.order_id){const a=node("a","Заказ ↗","order-link");a.href="https://online.moysklad.ru/app/#customerorder/edit?id="+encodeURIComponent(item.order_id);a.target="_blank";a.rel="noopener noreferrer";row.append(a);}
    target.append(row);
  }
  if(!target.childElementCount)empty(target,"Операций пока нет","Здесь появится история подтверждённых начислений и списаний.");
}
async function overview(){
  const result=await api("/api/admin/overview");summary=result;
  for(const [id,key] of Object.entries({"total-balance":"balance","total-customers":"customers","total-credited":"credited","total-redeemed":"redeemed","total-reserved":"reserved"}))$(id).textContent=number(result[key]);
  $("program-state").textContent=result.state==="Activated"?"Программа активна":"Требуется настройка";
  $("program-state").classList.toggle("success",result.state==="Activated");
  $("setup-callout").hidden=result.state==="Activated";
  const heartbeat=result.processing?.heartbeat_at,failedOrders=result.processing?.provider_errors;
  const processingHealthy=heartbeat && Number.isFinite(Date.parse(heartbeat)) &&
    Date.now()-Date.parse(heartbeat)>=0 && Date.now()-Date.parse(heartbeat)<600000 && !result.processing.last_error &&
    Number.isSafeInteger(failedOrders) && failedOrders===0;
  $("processing-callout").hidden=result.state!=="Activated"||Boolean(processingHealthy);
  $("processing-callout").querySelector("p").textContent=Number.isSafeInteger(failedOrders)&&failedOrders>0?
    "Не удалось проверить заказы: "+(failedOrders>=1001?"более 1000":failedOrders)+". Откройте заказ и проверьте виджет.":
    "Обработка заказов пока не подтверждена или требует внимания. Балансы ниже показывают уже сохранённые операции.";
  $("rule-earn").textContent=result.rules?fmt.format(result.rules.earn_bps/100)+"%":"—";
  $("rule-spend").textContent=result.rules?fmt.format(result.rules.spend_bps/100)+"%":"—";
  $("attention-link").textContent=result.attention?number(result.attention)+" операций требуют проверки →":"Операций на проверке нет";
  $("attention-link").classList.toggle("needs-attention",result.attention>0);
  status("Обновлено "+date(result.generated_at));
}
async function recent(){
  const data=await api("/api/admin/activity");activityRows($("recent-activity"),data.items.slice(0,6));
}
function customerRows(items,append=false){
  const target=$("customers");if(!append)target.replaceChildren();
  if(!append&&items.length){
    const head=node("div","","customer-row table-heading");
    for(const label of ["Клиент","Баланс","Доступно",""])head.append(node("span",label));target.append(head);
  }
  for(const item of items){
    const row=node("div","","customer-row");
    const identity=node("div","","customer-identity"),avatar=node("span",(item.name||"К").slice(0,1).toUpperCase(),"avatar"),text=node("div");
    text.append(node("strong",item.name||"Клиент без имени"),node("span",item.phone||(item.enrolled?"Участник программы":"Пока без бонусного счёта"),"muted"));
    identity.append(text);
    const balance=node("strong",number(item.balance),"customer-value");balance.dataset.label="Баланс";
    const available=node("span",number(item.available),"customer-value available");available.dataset.label="Доступно";
    row.append(identity,balance,available);
    const b=node("button","Открыть","small-button");b.onclick=()=>void openCustomer(item.id);row.append(b);target.append(row);
  }
  if(!target.childElementCount)empty(target,"Клиенты не найдены","Попробуйте другое имя или телефон. Новые клиенты создаются в МойСклад.");
}
async function customers(append=false){
  if(customersBusy)return;
  customersBusy=true;$("more-customers").disabled=true;
  const generation=searchEpoch,query=customerQuery,offset=append?customerOffset:0;
  $("customer-status").textContent="Загружаем клиентов…";
  try{
    const params=new URLSearchParams({q:query,offset:String(offset||0)});
    const data=await api("/api/admin/customers?"+params);
    if(generation!==searchEpoch)return;
    customerRows(data.items,append);customerOffset=data.next_offset;
    $("more-customers").hidden=customerOffset===null;customersLoaded=true;
    $("customer-status").textContent=query?"Результаты поиска: "+query:"Клиенты вашего аккаунта МойСклад";
  }catch(e){if(generation===searchEpoch)$("customer-status").textContent=e.message;}
  finally{customersBusy=false;$("more-customers").disabled=false;if(generation!==searchEpoch&&session)void customers();}
}
async function activity(append=false){
  if(activityBusy)return;activityBusy=true;$("more-activity").disabled=true;$("activity-status").textContent="Загружаем историю…";
  try{
    const data=await api("/api/admin/activity"+(append&&activityCursor?"?before="+encodeURIComponent(activityCursor):""));
    activityRows($("activity"),data.items,append);activityCursor=data.next_cursor;activityLoaded=true;
    $("more-activity").hidden=!activityCursor;$("activity-status").textContent="";
  }catch(e){$("activity-status").textContent=e.message;}
  finally{activityBusy=false;$("more-activity").disabled=false;}
}
function recalculationRows(data,append=false){
  if(!data||!Array.isArray(data.items))throw new Error("Не удалось загрузить пересчёты.");
  const target=$("customer-recalculations");if(!append)target.replaceChildren();
  const signed=value=>{const amount=BigInt(value);return (amount>0n?"+":"")+fmt.format(amount);};
  for(const item of data.items){
    const row=node("article","","activity-row");
    const icon=node("span","↺","operation-icon"),info=node("div","","operation-info");
    info.append(node("strong",BigInt(item.net_delta)===0n?"Баланс без изменений":"Пересчёт заказа"),
      node("span",date(item.created_at),"muted"),
      node("span","Пересчёт начисления: "+signed(item.earn_delta)),
      node("span","Пересчёт списанных бонусов: "+signed(item.refund_delta)),
      node("span","Изменение баланса: "+signed(item.net_delta)));
    const link=node("a","Заказ ↗","order-link");
    link.href="https://online.moysklad.ru/app/#customerorder/edit?id="+encodeURIComponent(item.order_id);
    link.target="_blank";link.rel="noopener noreferrer";row.append(icon,info,link);target.append(row);
  }
  recalculationCursor=data.next_cursor;
  $("more-customer-recalculations").hidden=!recalculationCursor;
  $("customer-recalculations-note").hidden=!target.childElementCount;
  if(!target.childElementCount)target.append(node("p","Пересчётов пока нет.","muted"));
}
async function moreCustomerRecalculations(){
  if(recalculationsBusy||!profileId||!recalculationCursor)return;
  const generation=profileEpoch,id=profileId,cursor=recalculationCursor;
  recalculationsBusy=true;$("more-customer-recalculations").disabled=true;
  $("customer-recalculations-status").textContent="Загружаем пересчёты…";
  try{
    const data=await api("/api/admin/customers/"+encodeURIComponent(id)+"/recalculations?before="+encodeURIComponent(cursor));
    if(generation!==profileEpoch)return;
    recalculationRows(data,true);$("customer-recalculations-status").textContent="";
  }catch(error){if(generation===profileEpoch)$("customer-recalculations-status").textContent=error.message;}
  finally{if(generation===profileEpoch){recalculationsBusy=false;$("more-customer-recalculations").disabled=false;}}
}

async function profileHistory(generation,append=false){
  const data=await api("/api/admin/activity?customer="+encodeURIComponent(profileId)+(append&&profileCursor?"&before="+encodeURIComponent(profileCursor):""));
  if(generation!==profileEpoch)return;
  activityRows($("customer-history"),data.items,append);profileCursor=data.next_cursor;$("more-customer-history").hidden=!profileCursor;
}
async function openCustomer(id){
  if(personalSaving)return;
  personalAssignment=null;personalPending=null;personalConflict=false;personalArchived=false;$("customer-personal-form").hidden=true;$("edit-customer-personal").disabled=true;
  $("customer-personal-value").textContent="—";$("customer-personal-policy").textContent="";$("customer-personal-audit").textContent="";personalStatus("");$("reload-customer-personal").hidden=true;
  const generation=++profileEpoch;profileId=id;profileCursor=null;recalculationCursor=null;recalculationsBusy=false;
  $("customer-recalculations").replaceChildren();$("customer-recalculations-status").textContent="";
  $("customer-recalculations-note").hidden=true;$("more-customer-recalculations").hidden=true;$("more-customer-recalculations").disabled=false;
  $("customer-name").textContent="Загружаем клиента…";$("customer-phone").textContent="";$("customer-available").textContent="—";
  $("customer-reserved").textContent="";$("customer-history").replaceChildren();$("more-customer-history").hidden=true;
  $("profile-status").textContent="";$("customer-source").hidden=true;
  if(!$("customer-dialog").open)$("customer-dialog").showModal();
  try{
    const data=await api("/api/admin/customers/"+encodeURIComponent(id));if(generation!==profileEpoch)return;
    renderPersonalAssignment(data.personal_discount);
    $("customer-name").textContent=data.name||"Клиент без имени";$("customer-phone").textContent=data.phone;
    $("customer-available").textContent=number(data.available);
    $("customer-reserved").textContent="Баланс "+number(data.balance)+" · В резерве "+number(data.reserved);
    $("customer-source").href="https://online.moysklad.ru/app/#company/edit?id="+encodeURIComponent(id);$("customer-source").hidden=false;
    if(!data.enrolled)$("profile-status").textContent="Нет бонусных операций.";
    try{recalculationRows(data.recalculations);}catch(error){$("customer-recalculations-status").textContent=error.message;}
    await profileHistory(generation);
  }catch(e){if(generation===profileEpoch)$("profile-status").textContent=e.message;}
}
function showScreen(){
  screen=["customers","activity"].includes(location.hash.slice(1))?location.hash.slice(1):"overview";
  document.querySelectorAll("[data-screen]").forEach(section=>section.hidden=section.dataset.screen!==screen);
  $("page-title").textContent={overview:"Обзор программы",customers:"Клиенты",activity:"Операции"}[screen];
  $("page-description").textContent={overview:"Балансы и операции клиентов.",customers:"Найдите клиента, проверьте баланс и историю бонусов.",activity:"Полная история подтверждённых движений бонусов."}[screen];
  $("open-customers").hidden=!session||screen!=="overview";
  if(session){if(screen==="customers"&&!customersLoaded)void customers();if(screen==="activity"&&!activityLoaded)void activity();}
}
async function refresh(){
  $("refresh").disabled=true;
  try{await overview();if(screen==="overview")await recent();else if(screen==="customers")await customers();else await activity();}
  catch(e){status(e.message,true);}
  finally{$("refresh").disabled=false;}
}
async function connect(){
  if(connecting)return;connecting=true;connectionEpoch++;searchEpoch++;profileEpoch++;session=null;summary=null;customerQuery="";customerOffset=null;activityCursor=null;$("customer-search").value="";
  for(const id of ["total-balance","total-customers","total-credited","total-redeemed","total-reserved","rule-earn","rule-spend","customer-available"])$(id).textContent="—";
  for(const id of ["customers","activity","recent-activity","customer-history"])$(id).replaceChildren();
  $("program-state").textContent="Подключение";$("program-state").classList.remove("success");
  if($("customer-dialog").open)$("customer-dialog").close();
  $("retry").hidden=true;$("workspace").hidden=true;$("refresh").hidden=true;$("open-customers").hidden=true;
  status("Подключаемся к МойСклад…");
  try{
    const token=await requestContext();
    session=(await api("/api/session",{method:"POST",body:JSON.stringify({token})})).token;
    if((await api("/api/me")).role!=="admin")throw new Error("Кабинет доступен администратору МойСклад. Сотрудники работают с бонусами в виджете заказа.");
    customersLoaded=false;activityLoaded=false;$("workspace").hidden=false;$("refresh").hidden=false;showScreen();await refresh();
  }catch(e){session=null;$("workspace").hidden=true;status(e.message,true);$("retry").hidden=false;}
  finally{connecting=false;}
}
$("search-form").onsubmit=e=>{e.preventDefault();const query=$("customer-search").value.trim();if(query&&query.length<2){$("customer-status").textContent="Введите минимум два символа.";return;}customerQuery=query;searchEpoch++;void customers();};
$("more-customers").onclick=()=>void customers(true);$("more-activity").onclick=()=>void activity(true);
$("more-customer-history").onclick=async()=>{if(profileBusy)return;profileBusy=true;const generation=profileEpoch;$("more-customer-history").disabled=true;try{await profileHistory(generation,true);}catch(e){if(generation===profileEpoch)$("profile-status").textContent=e.message;}finally{profileBusy=false;$("more-customer-history").disabled=false;}};
$("more-customer-recalculations").onclick=()=>void moreCustomerRecalculations();
$("close-customer").onclick=()=>{if(!personalSaving){$("customer-dialog").close();profileEpoch++;}};
$("customer-dialog").addEventListener("cancel",event=>{if(personalSaving)event.preventDefault();});
$("customer-dialog").addEventListener("close",()=>{profileEpoch++;});
$("widget-help").onclick=()=>$("help-dialog").showModal();$("close-help").onclick=()=>$("help-dialog").close();
$("refresh").onclick=()=>void refresh();$("retry").onclick=()=>void connect();
window.addEventListener("hashchange",showScreen);window.addEventListener("pagehide",()=>{session=null;profileEpoch++;connectionEpoch++;});

function personalStatus(message,error=false){
  $("customer-personal-status").textContent=message;$("customer-personal-status").classList.toggle("error",error);
}
function renderPersonalAssignment(value){
  if(!value||!Number.isInteger(value.version)||!(value.rate_bps===null||(Number.isInteger(value.rate_bps)&&value.rate_bps>=0&&value.rate_bps<=10000))){
    personalAssignment=null;$("customer-personal-value").textContent="Не удалось загрузить скидку";$("edit-customer-personal").disabled=true;
    $("reload-customer-personal").hidden=false;return;
  }
  personalAssignment=value;const rate=value.rate_bps;
  $("customer-personal-value").textContent=rate===null?"Не назначена":fmt.format(rate/100)+"%"+(rate===0?" · скидка отключена для клиента":"");
  $("customer-personal-policy").textContent=typeof personalAssignment.enabled!=="boolean"?
    "Обновите карточку, чтобы проверить состояние программы.":
    !personalAssignment.enabled?"Персональные скидки выключены в настройках программы.":
    rate===null?"Назначьте процент, чтобы использовать скидку в заказе.":
    rate===0?"Для клиента задана скидка 0%.":
    "Применяется в виджете нового заказа по правилам персональных скидок.";
  $("customer-personal-audit").textContent=personalAssignment.reason?"Последнее изменение: "+personalAssignment.reason+
    (personalAssignment.created_at?" · "+date(personalAssignment.created_at):""):"";
  $("edit-customer-personal").disabled=false;
}
function personalFormControls(){
  $("customer-personal-form").querySelectorAll("input,textarea,select,button").forEach(control=>control.disabled=personalSaving||personalArchived);
  const hasRate=$("customer-personal-mode").value==="rate";
  $("customer-personal-rate-field").hidden=!hasRate;$("customer-personal-rate").disabled=personalSaving||personalArchived||!hasRate;
  $("customer-personal-rate").required=hasRate;
  $("save-customer-personal").disabled=personalSaving||personalConflict||personalArchived;
  $("reload-customer-personal").disabled=personalSaving;$("edit-customer-personal").disabled=personalSaving||personalArchived||!personalAssignment;
  $("close-customer").disabled=personalSaving;
}
$("edit-customer-personal").onclick=()=>{
  if(personalSaving||personalArchived||!personalAssignment)return;
  $("customer-personal-mode").value=personalAssignment.rate_bps===null?"none":"rate";
  $("customer-personal-rate").value=(personalAssignment.rate_bps??0)/100;$("customer-personal-reason").value="";
  $("customer-personal-form").hidden=false;personalStatus("");personalFormControls();$("customer-personal-mode").focus();
};
$("customer-personal-mode").onchange=personalFormControls;
$("cancel-customer-personal").onclick=()=>{if(!personalSaving){$("customer-personal-form").hidden=true;personalStatus("");}};
$("reload-customer-personal").onclick=async()=>{
  if(personalSaving||!profileId)return;const generation=profileEpoch,id=profileId;
  personalSaving=true;personalFormControls();personalStatus("Загружаем актуальную скидку…");
  try{
    const data=await api("/api/admin/customers/"+encodeURIComponent(id));if(generation!==profileEpoch)return;
    renderPersonalAssignment(data.personal_discount);personalPending=null;personalConflict=false;$("customer-personal-form").hidden=true;
    $("reload-customer-personal").hidden=true;personalStatus("Актуальная скидка загружена.");
  }catch(e){if(generation===profileEpoch)personalStatus(e.message,true);}
  finally{personalSaving=false;personalFormControls();}
};
$("customer-personal-form").addEventListener("submit",async event=>{
  event.preventDefault();if(personalSaving||personalConflict||personalArchived||!personalAssignment||!profileId)return;
  let rate=null;
  if($("customer-personal-mode").value==="rate"){
    const raw=$("customer-personal-rate").value.trim();
    if(!/^\d{1,3}(\.\d{1,2})?$/.test(raw)){personalStatus("Укажите процент от 0 до 100, максимум два знака после запятой.",true);return;}
    const [whole,fraction=""]=raw.split(".");rate=Number(whole)*100+Number(fraction.padEnd(2,"0"));
    if(rate>10000){personalStatus("Процент не может превышать 100.",true);return;}
  }
  const reason=$("customer-personal-reason").value.trim();if(!reason){personalStatus("Укажите причину изменения скидки.",true);return;}
  const payload={rate_bps:rate,expected_version:personalAssignment.version,reason},signature=JSON.stringify(payload);
  if(!personalPending||personalPending.signature!==signature)personalPending={signature,id:crypto.randomUUID()};
  const generation=profileEpoch,id=profileId;personalSaving=true;personalFormControls();personalStatus("Сохраняем скидку…");
  try{
    const {current}=await api("/api/admin/customers/"+encodeURIComponent(id)+"/personal-discount",
      {method:"PUT",body:JSON.stringify({...payload,request_id:personalPending.id})});
    if(generation!==profileEpoch)return;
    renderPersonalAssignment(current);personalPending=null;$("customer-personal-form").hidden=true;$("reload-customer-personal").hidden=false;
    personalStatus("Скидка сохранена. Версия "+current.version+".");
    try{
      const fresh=await api("/api/admin/customers/"+encodeURIComponent(id));if(generation!==profileEpoch)return;
      renderPersonalAssignment(fresh.personal_discount);$("reload-customer-personal").hidden=true;
    }catch{if(generation===profileEpoch)personalStatus("Скидка сохранена. Обновите карточку, чтобы проверить действующее правило.",true);}
  }catch(e){
    if(generation===profileEpoch){
      if(e.conflict){personalPending=null;personalConflict=true;$("reload-customer-personal").hidden=false;}
      if(e.archived){personalPending=null;personalArchived=true;$("reload-customer-personal").hidden=true;}
      personalStatus(e.message,true);
    }
  }finally{personalSaving=false;personalFormControls();}
});

showScreen();void connect();

function findCustomer(){
 if(!session)return;
 location.hash="customers";
 showScreen();
 $("customer-search").focus({preventScroll:true});
}
$("open-customers").onclick=findCustomer;
window.addEventListener("keydown",event=>{
 if(event.key!=="/"||event.ctrlKey||event.metaKey||event.altKey||event.defaultPrevented||
   document.querySelector("dialog[open]")||event.target.closest("input,textarea,select,[contenteditable=true]"))return;
 event.preventDefault();findCustomer();
});
