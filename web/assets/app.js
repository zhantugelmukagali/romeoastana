import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
let session=null, state="pending", cursor=null, loading=false, selected=null, submitting=false;
const points=new Intl.NumberFormat("ru-RU");
const message=(text,error=false)=>{$("status").textContent=text;$("status").className=error?"error":"";};
async function api(path, options={}) {
  let response;
  try {
    response=await fetch(path,{...options,credentials:"omit",cache:"no-store",
      signal:AbortSignal.timeout(15000),
      headers:{"Content-Type":"application/json",...(session?{Authorization:"Bearer "+session}:{}),...options.headers}});
  } catch {throw new Error("Нет ответа сервера. Проверьте соединение и повторите действие.");}
  if(response.status===401) {
    session=null;$("workspace").hidden=true;$("refresh").hidden=true;$("retry").hidden=false;
    throw new Error("Сеанс завершён. Подключитесь снова.");
  }
  if(response.status===403) throw new Error("Для этого действия нужны права администратора МойСклад.");
  if(response.status===409) throw new Error("Данные изменились или бонусов недостаточно. Обновите список.");
  if(!response.ok) throw new Error("Не удалось выполнить действие. Попробуйте ещё раз.");
  return response.status===204?null:response.json();
}
function text(tag,value,className) {
  const node=document.createElement(tag);node.textContent=value;
  if(className)node.className=className;return node;
}
function renderCase(item) {
  const card=document.createElement("article");card.className="case";
  card.append(text("h2",points.format(item.points)+" бонусов"));
  const link=document.createElement("a");
  // UUID is validated by the API; no arbitrary external URL is rendered.
  link.href="https://online.moysklad.ru/app/#salesreturn/edit?id="+encodeURIComponent(item.return_id);
  link.target="_blank";link.rel="noopener noreferrer";link.textContent="Открыть возврат в МойСклад";
  card.append(link);
  card.append(text("p","Клиент: "+item.wallet_id,"identifier"));
  card.append(text("p","Баланс: "+points.format(item.balance)+" · Доступно: "+points.format(item.available)));
  if(item.state==="pending") {
    const actions=document.createElement("div");actions.className="actions";
    for(const [action,label] of [["reverse","Отменить начисление"],["waive","Сохранить бонусы клиенту"]]) {
      const button=text("button",label);
      if(action==="reverse" && item.available<item.points) {
        button.disabled=true;button.title="Недостаточно свободных бонусов";
      }
      button.addEventListener("click",()=>openDecision(item,action));
      actions.append(button);
    }
    card.append(actions);
    if(item.available<item.points) card.append(text("p","Для отмены начисления не хватает свободных бонусов.","hint"));
  } else {
    card.append(text("p",item.state==="waived"?"Бонусы сохранены клиенту":"Начисление отменено","badge"));
    card.append(text("p",item.resolution_reason||""));
  }
  return card;
}
async function loadCases(append=false) {
  if(loading || submitting)return;loading=true;
  $("refresh").disabled=true;$("more").disabled=true;
  document.querySelectorAll("[data-state]").forEach(b=>b.disabled=true);
  message("Загружаем возвраты…");
  try {
    const query=new URLSearchParams({state});if(append&&cursor)query.set("before",cursor);
    const result=await api("/api/admin/returns?"+query);
    if(!append)$("cases").replaceChildren();
    for(const item of result.items)$("cases").append(renderCase(item));
    cursor=result.next_cursor;$("more").hidden=!cursor;
    message($("cases").childElementCount?"":"Возвратов в этом разделе нет.");
  } catch(error) {message(error.message,true);}
  finally {
    loading=false;$("refresh").disabled=false;$("more").disabled=false;
    document.querySelectorAll("[data-state]").forEach(b=>b.disabled=false);
  }
}
function openDecision(item,action) {
  selected={item,action};$("reason").value="";$("form-error").textContent="";
  $("decision-title").textContent=action==="reverse"?"Отменить начисление?":"Сохранить бонусы клиенту?";
  $("impact").textContent=action==="reverse"
    ? "С баланса будет списано "+points.format(item.points)+" бонусов. Решение сохранится в журнале."
    : points.format(item.points)+" бонусов не будут списаны за этот возврат. Решение сохранится в журнале.";
  $("decision").showModal();$("reason").focus();
}
$("cancel").onclick=()=>{if(!submitting)$("decision").close();};
$("decision").addEventListener("cancel",event=>{if(submitting)event.preventDefault();});
$("decision-form").addEventListener("submit",async event=>{
  event.preventDefault();if(submitting||!selected)return;
  const reason=$("reason").value.trim();
  if(!reason){$("form-error").textContent="Укажите причину решения.";$("reason").focus();return;}
  submitting=true;$("confirm").disabled=true;$("cancel").disabled=true;$("form-error").textContent="";
  let done=false;
  try {
    await api("/api/admin/returns/"+encodeURIComponent(selected.item.id)+"/resolve",
      {method:"POST",body:JSON.stringify({decision:selected.action,reason})});
    $("decision").close();done=true;
  } catch(error) {$("form-error").textContent=error.message;}
  finally {submitting=false;$("confirm").disabled=false;$("cancel").disabled=false;}
  if(done)await loadCases();
});
async function connect() {
  $("retry").hidden=true;$("workspace").hidden=true;$("refresh").hidden=true;session=null;
  message("Подключаемся к МойСклад…");
  try {
    const token=await requestContext();
    session=(await api("/api/session",{method:"POST",body:JSON.stringify({token})})).token;
    const user=await api("/api/me");
    if(user.role!=="admin")throw new Error("Этот раздел доступен администратору МойСклад.");
    $("workspace").hidden=false;$("refresh").hidden=false;
    await loadCases();
  } catch(error) {message(error.message,true);$("retry").hidden=false;}
}
$("retry").onclick=connect;$("refresh").onclick=()=>loadCases();
$("more").onclick=()=>loadCases(true);
document.querySelectorAll("[data-state]").forEach(button=>button.onclick=()=>{
  state=button.dataset.state;cursor=null;$("cases").replaceChildren();$("more").hidden=true;
  document.querySelectorAll("[data-state]").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));
  loadCases();
});
window.addEventListener("pagehide",()=>{session=null;});
connect();
