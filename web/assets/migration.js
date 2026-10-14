import {requestContext} from "./bridge.js";
const $=id=>document.getElementById(id),fmt=new Intl.NumberFormat("ru-RU");
let session=null,preview=null,busy=false,pendingReason=null;
function status(s,error=false){$("status").textContent=s;$("status").className=error?"error":"";}
function node(t,s){const x=document.createElement(t);x.textContent=s;return x;}
function controls(value){busy=value;$("preview-button").disabled=value;$("refresh").disabled=value;$("file").disabled=value;
  $("reason").disabled=value;$("confirmed").disabled=value;$("commit").disabled=value||!preview||!$("confirmed").checked;}
async function api(path,options={}){
  let response;
  try{response=await fetch(path,{...options,credentials:"omit",cache:"no-store",signal:AbortSignal.timeout(120000),
    headers:{"Content-Type":"application/json",Authorization:"Bearer "+session}});}
  catch{throw new Error("Нет ответа сервера. Проверьте историю переноса перед повтором.");}
  if(response.status===401){session=null;$("workspace").hidden=true;$("retry").hidden=false;throw new Error("Сеанс завершён. Подключитесь снова.");}
  if(response.status===403)throw new Error("Нужны права администратора.");
  if(response.status===409)throw new Error("Перенос недоступен: программа запущена, проверка устарела или у клиента уже есть операции. Повторно проверьте файл.");
  const data=await response.json();
  if(!response.ok)throw new Error(data.error==="invalid_import"?data.message:"Не удалось проверить клиентов в МойСклад. Проверьте файл и подключение.");
  return data;
}
async function history(){
  const data=await api("/api/admin/imports");$("history").replaceChildren();
  for(const x of data.items){
    const card=node("article","");card.className="case";
    card.append(node("strong",(x.committed_at?"Перенесено":"Проверено, не зачислено")+": "+fmt.format(x.total)+" бонусов"),
      node("p","Клиентов: "+x.customers+" · "+new Date(x.committed_at||x.created_at).toLocaleString("ru-RU")),
      node("p","Отпечаток файла: "+x.source_hash));$("history").append(card);
  }
  if(!data.items.length)$("history").append(node("p","Переносов пока нет."));
}
$("upload").onsubmit=async e=>{
  e.preventDefault();if(busy)return;preview=null;pendingReason=null;$("preview").hidden=true;$("confirmed").checked=false;controls(true);
  try{
    const file=$("file").files[0];if(!file||file.size>60000)throw new Error("Выберите CSV-файл размером до 60 КБ.");
    status("Проверяем клиентов и суммы…");
    preview=await api("/api/admin/imports/preview",{method:"POST",body:JSON.stringify({csv_text:await file.text()})});
    $("total").textContent="Клиентов: "+preview.rows.length+" · Всего: "+fmt.format(preview.total)+" бонусов";
    $("rows").replaceChildren();
    for(const x of preview.rows)$("rows").append(node("p",x.name+" · "+fmt.format(x.points)+" бонусов"));
    $("preview").hidden=false;status("Проверка завершена. Балансы ещё не изменены.");await history();
  }catch(e){status(e.message,true);}
  finally{controls(false);}
};
$("confirmed").onchange=()=>controls(busy);
$("file").onchange=()=>{preview=null;pendingReason=null;$("preview").hidden=true;controls(false);};
$("commit").onclick=async()=>{
  if(busy||!preview||!$("confirmed").checked)return;
  const reason=$("reason").value.trim();if(!reason){status("Укажите основание переноса.",true);return;}
  if(pendingReason&&pendingReason!==reason){status("Для повтора сохраните прежнее основание или заново проверьте файл.",true);return;}
  pendingReason=reason;controls(true);status("Переносим остатки…");
  try{
    const result=await api("/api/admin/imports/"+encodeURIComponent(preview.id)+"/commit",
      {method:"POST",body:JSON.stringify({digest:preview.digest,reason})});
    preview=null;$("preview").hidden=true;status("Перенесено "+fmt.format(result.total)+" бонусов для "+result.customers+" клиентов.");
    await history();
  }catch(e){status(e.message,true);}
  finally{controls(false);}
};
$("refresh").onclick=async()=>{if(busy)return;controls(true);try{await history();}catch(e){status(e.message,true);}finally{controls(false);}};
async function connect(){
  $("retry").hidden=true;$("workspace").hidden=true;session=null;
  try{
    const token=await requestContext();session=(await api("/api/session",{method:"POST",body:JSON.stringify({token})})).token;
    const user=await api("/api/me");if(user.role!=="admin")throw new Error("Раздел доступен администратору.");
    $("workspace").hidden=false;await history();status("Загрузите сопоставленный файл остатков.");
  }catch(e){status(e.message,true);$("retry").hidden=false;}
}
$("retry").onclick=connect;window.addEventListener("pagehide",()=>{session=null;});connect();
