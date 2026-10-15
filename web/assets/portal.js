import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
const fmt=new Intl.NumberFormat("ru-KZ");
const number=value=>fmt.format(BigInt(value));
const date=value=>new Date(value).toLocaleString("ru-RU",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
const labels={credit:"Начисление",opening:"Перенос остатков",redemption:"Списание",reversal:"Отмена начисления",refund:"Возврат бонусов",settlement:"Пересчёт",adjustment:"Корректировка"};
let session=null,connecting=false,summary=null,screen="overview";
let customerOffset=null,activityCursor=null,customerQuery="",customersBusy=false,activityBusy=false;
let customersLoaded=false,activityLoaded=false,searchEpoch=0,profileEpoch=0,profileId=null,profileCursor=null,profileBusy=false;
const node=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
function status(text,error=false){$("status").textContent=text;$("status").classList.toggle("error",error);}
async function api(path,options={}){
  let response;
  try{response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",...(session?{Authorization:"Bearer "+session}:{})}});}
  catch{throw new Error("Нет ответа сервера. Повторите загрузку.");}
  if(response.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(response.status===403)throw new Error("Кабинет доступен администратору. Проверьте права и доступ к МойСклад.");
  if(response.status===404)throw new Error("Клиент или страница истории больше не доступны. Обновите данные.");
  if(!response.ok)throw new Error("Не удалось загрузить данные. Повторите попытку.");
  return response.json();
}
function empty(target,title,description){
  const box=node("div","","empty-state");box.append(node("span","↗","empty-symbol"),node("h3",title),node("p",description));target.replaceChildren(box);
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
  $("program-state").textContent=result.state==="Activated"?"Программа запущена":"Требуется настройка";
  $("program-state").classList.toggle("success",result.state==="Activated");
  $("setup-callout").hidden=result.state==="Activated";
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
    identity.append(avatar,text);row.append(identity,node("strong",number(item.balance)),node("span",number(item.available),"available"));
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
  finally{customersBusy=false;$("more-customers").disabled=false;if(generation!==searchEpoch)void customers();}
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
async function profileHistory(generation,append=false){
  const data=await api("/api/admin/activity?customer="+encodeURIComponent(profileId)+(append&&profileCursor?"&before="+encodeURIComponent(profileCursor):""));
  if(generation!==profileEpoch)return;
  activityRows($("customer-history"),data.items,append);profileCursor=data.next_cursor;$("more-customer-history").hidden=!profileCursor;
}
async function openCustomer(id){
  const generation=++profileEpoch;profileId=id;profileCursor=null;
  $("customer-name").textContent="Загружаем клиента…";$("customer-phone").textContent="";$("customer-available").textContent="—";
  $("customer-reserved").textContent="";$("customer-history").replaceChildren();$("more-customer-history").hidden=true;
  $("profile-status").textContent="";$("customer-source").hidden=true;
  if(!$("customer-dialog").open)$("customer-dialog").showModal();
  try{
    const data=await api("/api/admin/customers/"+encodeURIComponent(id));if(generation!==profileEpoch)return;
    $("customer-name").textContent=data.name||"Клиент без имени";$("customer-phone").textContent=data.phone;
    $("customer-available").textContent=number(data.available);
    $("customer-reserved").textContent="Баланс "+number(data.balance)+" · В резерве "+number(data.reserved);
    $("customer-source").href="https://online.moysklad.ru/app/#company/edit?id="+encodeURIComponent(id);$("customer-source").hidden=false;
    if(!data.enrolled)$("profile-status").textContent="Бонусный счёт появится при первой операции или переносе остатков.";
    await profileHistory(generation);
  }catch(e){if(generation===profileEpoch)$("profile-status").textContent=e.message;}
}
function showScreen(){
  screen=["customers","activity"].includes(location.hash.slice(1))?location.hash.slice(1):"overview";
  document.querySelectorAll("[data-screen]").forEach(section=>section.hidden=section.dataset.screen!==screen);
  $("page-title").textContent={overview:"Обзор программы",customers:"Клиенты",activity:"Операции"}[screen];
  $("page-description").textContent={overview:"Бонусы, клиенты и покупки — в одном месте.",customers:"Найдите клиента, проверьте баланс и историю бонусов.",activity:"Полная история подтверждённых движений бонусов."}[screen];
  if(session){if(screen==="customers"&&!customersLoaded)void customers();if(screen==="activity"&&!activityLoaded)void activity();}
}
async function refresh(){
  $("refresh").disabled=true;
  try{await overview();if(screen==="overview")await recent();else if(screen==="customers")await customers();else await activity();}
  catch(e){status(e.message,true);}
  finally{$("refresh").disabled=false;}
}
async function connect(){
  if(connecting)return;connecting=true;session=null;$("retry").hidden=true;$("workspace").hidden=true;$("refresh").hidden=true;
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
$("close-customer").onclick=()=>{$("customer-dialog").close();profileEpoch++;};
$("customer-dialog").addEventListener("close",()=>{profileEpoch++;});
$("widget-help").onclick=()=>$("help-dialog").showModal();$("close-help").onclick=()=>$("help-dialog").close();
$("refresh").onclick=()=>void refresh();$("retry").onclick=()=>void connect();
window.addEventListener("hashchange",showScreen);window.addEventListener("pagehide",()=>{session=null;profileEpoch++;});
showScreen();void connect();
