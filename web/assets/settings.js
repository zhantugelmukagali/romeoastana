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
  $("retry").hidden=true;$("workspace").hidden=true;session=null;pending=null;busy(true);
  status("Подключаемся к МойСклад…");
  try {
    const token=await requestContext();
    const response=await fetch("/api/session",{method:"POST",credentials:"omit",cache:"no-store",
      signal:AbortSignal.timeout(15000),headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
    if(!response.ok)throw new Error("Не удалось подтвердить доступ. Откройте раздел заново из МойСклад.");
    session=(await response.json()).token;
    const user=await api("/api/me");
    if(user.role!=="admin")throw new Error("Этот раздел доступен администратору МойСклад.");
    await load();$("workspace").hidden=false;
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
    try{await history();}catch{status("Правила сохранены. Историю не удалось обновить — загрузите актуальные правила.");}
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
  }catch(error){$("setup-status").textContent=error.name==="TimeoutError"?"Ответ задерживается. Повторная проверка найдёт уже созданное поле.":error.message;}
  finally{busy(false);}
};
