import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id);
let session=null,pending=null,busy=false;
function status(s){$("status").textContent=s;}
function node(t,s){const n=document.createElement(t);n.textContent=s;return n;}
async function api(path,options={}){
  let r;
  try{r=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(15000),
    headers:{"Content-Type":"application/json",Authorization:"Bearer "+session}});}
  catch{throw new Error("Ответ не получен. Повторите с прежней причиной: изменение не продублируется.");}
  if(r.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(r.status===409)throw new Error("Доступ уже изменён. Закройте окно и обновите список.");
  if(!r.ok)throw new Error("Не удалось изменить доступ. Нужны права администратора.");
  return r.json();
}
async function listing(){
  const data=await api("/api/admin/operators");$("people").replaceChildren();
  for(const p of data.items){
    const a=node("article","");a.className="case";a.append(node("h2",p.login||p.user_id),
      node("p",p.enabled?"Доступ разрешён":"Доступ сотрудника не разрешён"),
      node("p","Администратор МойСклад имеет доступ независимо от этого разрешения."));
    if(p.reason)a.append(node("p","Последнее изменение: "+p.reason));
    const b=node("button",p.enabled?"Отозвать доступ":"Разрешить доступ");
    b.onclick=()=>{
      pending={user:p.user_id,enabled:!p.enabled,expected_version:p.version,request_id:crypto.randomUUID(),reason:null};
      $("decision-title").textContent=p.enabled?"Отозвать доступ":"Разрешить доступ к бонусам аккаунта";
      $("employee").textContent=p.login||p.user_id;$("reason").value="";$("error").textContent="";$("decision").showModal();
    };
    a.append(b);$("people").append(a);
  }
  if(!data.items.length)$("people").append(node("p","Сотрудники ещё не открывали БонусПро."));
}
$("decision-form").onsubmit=async e=>{
  e.preventDefault();if(busy||!pending)return;
  const reason=$("reason").value.trim();if(!reason){$("error").textContent="Укажите причину.";return;}
  if(pending.reason&&pending.reason!==reason){$("error").textContent="Для повтора сохраните прежнюю причину.";return;}
  pending.reason=reason;busy=true;$("confirm").disabled=true;$("cancel").disabled=true;
  try{
    const {user,...payload}=pending;await api("/api/admin/operators/"+encodeURIComponent(user),{method:"PUT",body:JSON.stringify(payload)});
    $("decision").close();pending=null;status("Доступ обновлён.");await listing();
  }catch(e){$("error").textContent=e.message;}
  finally{busy=false;$("confirm").disabled=false;$("cancel").disabled=false;}
};
$("decision").addEventListener("cancel",e=>{if(busy)e.preventDefault();});
$("cancel").onclick=()=>{if(!busy){$("decision").close();pending=null;}};
$("refresh").onclick=async()=>{try{await listing();}catch(e){status(e.message);}};
async function connect(){
  session=null;$("workspace").hidden=true;$("retry").hidden=true;
  try{
    const token=await requestContext();session=(await api("/api/session",{method:"POST",body:JSON.stringify({token})})).token;
    if((await api("/api/me")).role!=="admin")throw new Error("Раздел доступен администратору.");
    await listing();$("workspace").hidden=false;status("Управление доступом к бонусам.");
  }catch(e){status(e.message);$("retry").hidden=false;}
}
$("retry").onclick=connect;window.addEventListener("pagehide",()=>{session=null;});connect();
