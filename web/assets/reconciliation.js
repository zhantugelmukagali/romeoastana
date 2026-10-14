import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id),fmt=new Intl.NumberFormat("ru-RU");
const signed=n=>(n>0?"+":"")+fmt.format(n);
let session=null,state="pending",cursor=null,busy=false,selected=null,submitting=false;
function node(tag,text){const x=document.createElement(tag);x.textContent=text;return x;}
function status(text,error=false){$("status").textContent=text;$("status").className=error?"error":"";}
async function api(path,options={}){
  let response;
  try{response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(120000),
    headers:{"Content-Type":"application/json",Authorization:"Bearer "+session}});}
  catch{throw new Error("Нет ответа сервера. Обновите список перед повтором: решение могло сохраниться.");}
  if(response.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(response.status===403)throw new Error("Нужны права администратора МойСклад.");
  if(response.status===409)throw new Error("Документы изменились или бонусов недостаточно. Обновите список и проверьте решение.");
  if(!response.ok)throw new Error("Не удалось проверить документы. Повторите позже.");
  return response.json();
}
function link(order){
  const a=node("a","Открыть заказ в МойСклад");a.href="https://online.moysklad.ru/app/#customerorder/edit?id="+encodeURIComponent(order);
  a.target="_blank";a.rel="noopener noreferrer";return a;
}
function open(item,action){
  selected={item,action};$("reason").value="";$("error").textContent="";
  $("title").textContent=action==="release"?"Освободить резерв отменённого заказа?":action==="issue"?"Проверить исправление?":action==="apply"?"Выполнить пересчёт?":"Сохранить бонусы клиенту?";
  $("impact").textContent=action==="release"?"Закройте все вкладки этого заказа. Отмените проведение заказа без скидки, оплат и отгрузок. Сервер освободит резерв только после проверки исходных цен и отсутствия бонусной метки, не ранее 15 минут после запроса. Если скидка уже сохранена, освобождение запрещено.":action==="issue"?"Сервер заново проверит документ. Блокировка снимется только после подтверждения исправления.":
    action==="apply"?"Изменение баланса: "+signed(item.total_delta)+" бонусов. Зарезервированные бонусы не затрагиваются.":
    "Отрицательные корректировки по этому решению будут прощены клиенту. Положительные корректировки сохранятся. Решение записывается в историю.";
  $("decision").showModal();$("reason").focus();
}
function render(item){
  const card=node("article","");card.className="case";
  card.append(node("h2","Заказ "+(item.order_name||item.order_id)),link(item.order_id),
    node("p","Пересчёт начисления: "+signed(item.earn_delta)),
    node("p","Пересчёт списанных бонусов: "+signed(item.refund_delta)),
    node("strong","Изменение баланса: "+signed(item.total_delta)),
    node("p","Свободно: "+fmt.format(item.available)+" бонусов"));
  if(item.state==="pending"){
    const actions=node("div","");actions.className="actions";
    const apply=node("button","Выполнить пересчёт");apply.disabled=item.total_delta < -item.available;
    apply.onclick=()=>open(item,"apply");actions.append(apply);
    const waive=node("button","Сохранить бонусы клиенту");waive.onclick=()=>open(item,"waive");actions.append(waive);
    card.append(actions);
    if(apply.disabled)card.append(node("p","Для пересчёта не хватает свободных бонусов. Отрицательный баланс запрещён."));
  }else card.append(node("p",item.reason||"Документ изменился; прежнее решение больше не требуется."));
  return card;
}
function controls(value){busy=value;$("refresh").disabled=value;$("more").disabled=value;
  document.querySelectorAll("[data-state]").forEach(x=>x.disabled=value);}
async function load(append=false){
  if(busy||submitting)return;controls(true);status("Проверяем состояние…");
  try{
    const q=new URLSearchParams({state});if(append&&cursor)q.set("before",cursor);
    const data=await api("/api/admin/settlements?"+q);
    if(!append)$("cases").replaceChildren();
    data.items.forEach(x=>$("cases").append(render(x)));cursor=data.next_cursor;$("more").hidden=!cursor;
    const diagnostic=await api("/api/admin/automation");$("issues").replaceChildren();
    for(const item of diagnostic.issues){
      const card=node("article","");card.className="case";
      card.append(link(item.order_id),node("p","Проверьте состав заказа, скидку и связанные документы. Списание по клиенту приостановлено."));
      const button=node("button","Проверить исправление");button.onclick=()=>open(item,"issue");card.append(button);$("issues").append(card);
    }
    $("unverified").replaceChildren();
    for(const item of diagnostic.unverified||[]){
      const card=node("article","");card.className="case";
      card.append(link(item.order_id),node("p","В резерве: "+fmt.format(item.points)+" бонусов"));
      const button=node("button","Проверить отмену и освободить резерв");
      button.onclick=()=>open(item,"release");card.append(button);$("unverified").append(card);
    }
    if(!$("unverified").childElementCount)$("unverified").append(node("p","Неподтверждённых списаний нет."));
    if(!diagnostic.issues.length)$("issues").append(node("p","Расхождений нет."));
    status($("cases").childElementCount?"":"Операций в этом разделе нет.");
  }catch(e){status(e.message,true);}
  finally{controls(false);}
}
$("decision-form").onsubmit=async event=>{
  event.preventDefault();if(submitting||!selected)return;
  const reason=$("reason").value.trim();if(!reason){$("error").textContent="Укажите причину решения.";return;}
  submitting=true;$("confirm").disabled=true;$("cancel").disabled=true;$("error").textContent="Проверяем сохранённые документы…";
  let success=false;
  try{
    const issue=selected.action==="issue",release=selected.action==="release",id=encodeURIComponent(selected.item.id);
    const path=release?"redemptions/"+id+"/release":(issue?"issues/":"settlements/")+id+"/resolve";
    await api("/api/admin/"+path,
      {method:"POST",body:JSON.stringify(issue||release?{reason}:{decision:selected.action,reason})});
    $("decision").close();success=true;
  }catch(e){$("error").textContent=e.message;}
  finally{submitting=false;$("confirm").disabled=false;$("cancel").disabled=false;}
  if(success)await load();
};
$("cancel").onclick=()=>{if(!submitting)$("decision").close();};
$("decision").addEventListener("cancel",e=>{if(submitting)e.preventDefault();});
async function connect(){
  $("workspace").hidden=true;$("retry").hidden=true;session=null;
  try{
    const token=await requestContext();
    session=(await api("/api/session",{method:"POST",body:JSON.stringify({token})})).token;
    const user=await api("/api/me");if(user.role!=="admin")throw new Error("Раздел доступен администратору МойСклад.");
    $("workspace").hidden=false;await load();
  }catch(e){status(e.message,true);$("retry").hidden=false;}
}
$("retry").onclick=connect;$("refresh").onclick=()=>load();$("more").onclick=()=>load(true);
document.querySelectorAll("[data-state]").forEach(button=>button.onclick=()=>{
  if(busy||submitting)return;state=button.dataset.state;cursor=null;
  document.querySelectorAll("[data-state]").forEach(x=>x.setAttribute("aria-pressed",String(x===button)));
  load();
});
window.addEventListener("pagehide",()=>{session=null;});
connect();
