// MoySklad v2: Open/Change/Save/UpdateRequest and one-use UserContextRequest.
// Host events provide UI context only. The backend rereads all financial evidence.
import {HOST,isHostMessage} from "./host.js";
const $=id=>document.getElementById(id);
let sequence=0,epoch=0,openMessage=null,orderId=null,token=null,quote=null;
let dirty=false,busy=false,attempt=null,requestId=null,draftRevision=0,savedWhileBusy=false;
const waiting=new Map();
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const amountFormat=new Intl.NumberFormat("ru-KZ");
const money=value=>{
 const minor=BigInt(value),fraction=minor%100n;return amountFormat.format(minor/100n)+(fraction?","+String(fraction).padStart(2,"0"):"")+" ₸";
};
function enteredPoints(){
 const raw=$("points").value;
 return /^[0-9]+$/.test(raw)&&Number.isSafeInteger(Number(raw))?Number(raw):null;
}
function canSpend(){return Boolean(quote?.can_prepare&&!quote.quote.blocked_reason&&quote.quote.max_spend_points>0);}
function validAmount(){const value=enteredPoints();return value!==null&&value>0&&value<=quote?.quote.max_spend_points;}
const plurals=new Intl.PluralRules("ru");
const pointsUnit=value=>({one:"бонус",few:"бонуса",many:"бонусов",other:"бонусов"}[plurals.select(value)]);
const pointsLabel=value=>amountFormat.format(value)+" "+pointsUnit(value);
function renderBalance(id,value){
 const amount=document.createElement("span"),unit=document.createElement("span");
 amount.className="points-number";amount.textContent=amountFormat.format(value);
 unit.className="points-unit";unit.textContent=pointsUnit(value);
 $(id).replaceChildren(amount,document.createTextNode(" "),unit);
}
function view(name){document.querySelector("main").dataset.view=name;}
function status(text,error=false){$("status").textContent=text;$("status").dataset.error=String(error);$("status").hidden=!text;}
function controls(){
  $("refresh").disabled=busy||!orderId;
  $("apply").disabled=busy||dirty||!canSpend()||!validAmount()||Boolean(attempt);
  $("points").disabled=busy||dirty||!canSpend()||Boolean(attempt);
  $("apply").textContent=busy&&!attempt?"Применяем…":"Применить";
  const spending=Boolean(quote&&canSpend()&&!attempt);
  $("spend-form").hidden=!spending;$("apply").hidden=!spending;
  $("verify").hidden=!attempt?.id||(attempt.state==="committed"&&attempt.verified===true);
  $("refresh").setAttribute("aria-busy",String(busy));
  $("spend-form").setAttribute("aria-busy",String(busy));
  $("maximum-button").disabled=$("points").disabled||!quote?.quote.max_spend_points;
  $("verify").disabled=busy||!attempt;
}
function hostRequest(name,extra={},responseName){
  return new Promise((resolve,reject)=>{
    const id=++sequence;
    const timer=setTimeout(()=>{waiting.delete(id);reject(new Error("host_timeout"));},10000);
    waiting.set(id,{resolve,reject,timer,responseName,epoch});
    parent.postMessage({name,messageId:id,...extra},HOST);
  });
}
async function api(path,options={}){
  const generation=epoch;
  const response=await fetch(path,{...options,credentials:"omit",cache:"no-store",headers:{"Content-Type":"application/json",
    ...(token?{Authorization:"Bearer "+token}:{}),...options.headers},signal:AbortSignal.timeout(55000)});
  if(generation!==epoch)throw new Error("document_changed");
  if(!response.ok){
    let payload;
    try{payload=await response.json();}catch{payload=null;}
    if(generation!==epoch)throw new Error("document_changed");
    const error=new Error("api_error");error.status=response.status;
    if(["moysklad_permissions_required","operator_access_required"].includes(payload?.error))error.code=payload.error;
    if(response.status===401)token=null;
    throw error;
  }
  const value=await response.json();
  if(generation!==epoch)throw new Error("document_changed");
  return value;
}
function explain(error){
  if(error.message==="host_rejected")return "МойСклад отклонил скидку. Резерв сохранён — нужен администратор.";
  if(error.status===401)return "Сессия истекла. Нажмите «Обновить».";
  if(error.status===403)return error.code==="moysklad_permissions_required"?"Нет доступа к данным МойСклад. Проверьте подключение решения.":"Нет доступа к бонусам. Обратитесь к администратору.";
  if(error.status===409)return "Заказ или баланс изменился. Обновите данные.";
  if(error.status===422)return "Скидка не рассчитана точно. Проверьте позиции и НДС.";
  return attempt?"Связь прервана. Нажмите «Обновить».":"Не удалось загрузить бонусы. Нажмите «Обновить».";
}
async function authenticate(generation){
  if(token)return;
  const message=await hostRequest("UserContextRequest",{},"UserContextResponse");
  if(typeof message.token!=="string"||!message.token.trim()||message.token.length>4096)throw new Error("invalid_context");
  const session=await api("/api/session",{method:"POST",body:JSON.stringify({token:message.token})});
  if(generation!==epoch)return;
  token=session.token;
}
function showPending(value){
  attempt=value;quote=null;view("pending");$("summary").hidden=true;$("pending").hidden=false;
  const done=value.state==="committed"&&value.verified===true;
  $("pending").dataset.state=done?"done":value.verified===false?"review":"waiting";
  $("pending-title").textContent=done?"Списано":!value.id?"Проверьте операцию":
    value.verified===false?"Проверьте сохранение":value.recovered?"Проверяем сохранение":"Сохраните заказ";
  $("pending-text").textContent=!value.id?"Ответ не получен":
    pointsLabel(value.points)+(done?"":" в резерве");
  $("pending-note").textContent=done?"Заказ сохранён и проверен.":
    !value.id?"Обновите данные. Не применяйте бонусы повторно.":
    value.verified===false?"Сохраните заказ. Если сумма отличается — нужен администратор.":
    value.recovered?"Сверяем операцию с заказом.":
    "Нажмите «Сохранить» в МойСклад.";
  status("");controls();
}
function renderQuote(value){
  quote=value;attempt=null;$("summary").hidden=false;$("pending").hidden=true;
  $("order-label").textContent=value.name?"Заказ №"+value.name:"Заказ покупателя";
  renderBalance("balance",value.quote.balance);
  const reserved=Number.isSafeInteger(value.quote.available)&&value.quote.available<value.quote.balance;
  $("available-note").hidden=!reserved;
  $("available-note").textContent=reserved?"В резерве: "+amountFormat.format(value.quote.balance-value.quote.available):"";
  renderBalance("maximum",value.quote.max_spend_points);
  $("limit-block").hidden=!canSpend();
  const empty=value.quote.balance===0;
  $("balances").hidden=empty;
  $("basis").textContent="Товары для расчёта: "+money(value.eligible_minor)+".";
  $("points").max=String(value.quote.max_spend_points);
  const earn=value.quote.potential_earn_points;
  $("earning").hidden=!value.can_prepare||Boolean(value.quote.blocked_reason)||!Number.isSafeInteger(earn)||earn<=0;
  $("earn-value").textContent=$("earning").hidden?"":"+"+pointsLabel(earn);
  view(empty?"empty":"ready");
  status(dirty?"Сохраните изменения в заказе.":
    !value.can_prepare?"Программа ещё не настроена. Нужен администратор.":
    value.quote.blocked_reason?"Списание приостановлено. Нужен администратор.":
    value.quote.max_spend_points===0?(value.eligible_minor===0?"В заказе только услуги — бонусы не списываются.":
      empty?"Бонусов пока нет":"Бонусы пока недоступны. Проверьте резервы."):"");
  updatePayable();
}
async function refresh(){
  if(!orderId||busy)return;
  const generation=epoch,id=orderId;busy=true;quote=null;$("summary").hidden=true;$("pending").hidden=true;
  $("order-label").textContent="";view("loading");status("Загружаем бонусы…");controls();
  try{
    await authenticate(generation);
    if(generation!==epoch)return;
    const value=await api("/api/widget/orders/"+id);
    if(generation!==epoch)return;
    if(value.redemption){
      showPending({...value.redemption,recovered:true});
      // Refresh resumes verification; a stored state alone never renders success.
      savedWhileBusy=true;
    }else renderQuote(value);
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
function updatePayable(){
  const raw=$("points").value,points=enteredPoints(),valid=validAmount();
  const invalid=raw!==""&&!valid;
  $("points").setAttribute("aria-invalid",String(invalid));
  $("amount-hint").hidden=!invalid;
  $("amount-hint").textContent=invalid?"От 1 до "+amountFormat.format(quote?.quote.max_spend_points??0)+", целыми бонусами.":"";
  $("payable").textContent=quote?money(quote.total_minor-(valid?points*100:0)):"";
  controls();
}
function finishBusy(generation){
  if(generation!==epoch)return;
  busy=false;controls();
  if(savedWhileBusy){
    savedWhileBusy=false;
    // Save can race the prepare/UpdateResponse or a provider read. Verify the
    // saved document after the in-flight operation, never treat Save as proof.
    queueMicrotask(()=>{if(generation===epoch){if(attempt)void verify();else void refresh();}});
  }
}
async function apply(event){
  event.preventDefault();
  if(busy||dirty||attempt||!canSpend())return;
  const raw=$("points").value,points=Number(raw);
  if(!/^[0-9]+$/.test(raw)||!Number.isSafeInteger(points)||points<1||points>quote.quote.max_spend_points){
    status("Введите целое число в пределах лимита.",true);return;
  }
  const generation=epoch,id=orderId,revision=draftRevision;
  busy=true;controls();requestId=requestId||crypto.randomUUID();
  try{
    const result=await api("/api/widget/orders/"+id+"/prepare",{method:"POST",
      body:JSON.stringify({points,request_id:requestId,source_hash:quote.saved_source_hash})});
    if(generation!==epoch)return;
    attempt=result;
    if(revision!==draftRevision||dirty||!result.update_state){
      showPending(result);status("Заказ изменился. Резерв сохранён — проверьте операцию.",true);return;
    }
    status("Применяем бонусы…");
    await hostRequest("UpdateRequest",{updateState:result.update_state},"UpdateResponse");
    if(generation!==epoch)return;
    dirty=true;showPending(result);
  }catch(error){
    if(generation===epoch){
      const rejected=!attempt && [400,401,403,404,409,422].includes(error.status);
      if(rejected){
        // An explicit rejection is different from an ambiguous network failure.
        // Require a fresh quote, without inventing a successful reservation.
        quote=null;requestId=null;
        $("summary").hidden=true;$("pending").hidden=true;
      }else{
        // A lost prepare response may already have reserved points.
        // Preserve uncertainty and never dispatch a second mutation blindly.
        attempt=attempt||{state:"verifying",points};
        showPending(attempt);
      }
      status(explain(error),true);
    }
  }finally{finishBusy(generation);}
}
async function verify(){
  if(busy||!orderId||!attempt)return;
  const generation=epoch,id=orderId;
  busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/verify",{method:"POST"});
    if(generation!==epoch)return;
    showPending(result);
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
window.addEventListener("message",event=>{
  if(!isHostMessage(event)||!event.data||typeof event.data!=="object")return;
  const message=event.data;
  const correlation=message.correlationId ??
    (message.name==="InvalidMessageError"?message.invalidMessage?.messageId:undefined);
  const request=waiting.get(correlation);
  if(request){
    waiting.delete(correlation);clearTimeout(request.timer);
    if(request.epoch!==epoch||message.name!==request.responseName)request.reject(new Error("host_rejected"));
    else request.resolve(message);
    return;
  }
  if(message.name==="Open"&&Number.isSafeInteger(message.messageId)){
    if(openMessage===message.messageId)return;
    epoch++;openMessage=message.messageId;orderId=null;token=null;quote=null;attempt=null;requestId=null;
    dirty=false;busy=false;draftRevision=0;savedWhileBusy=false;
    for(const pending of waiting.values()){clearTimeout(pending.timer);pending.reject(new Error("document_changed"));}
    waiting.clear();$("summary").hidden=true;$("pending").hidden=true;$("points").value="";$("order-label").textContent="";$("amount-hint").textContent="";$("payable").textContent="";$("points").setAttribute("aria-invalid","false");
    view("loading");status("Загружаем бонусы…");controls();
    if(message.extensionPoint==="document.customerorder.edit")
      parent.postMessage({name:"OpenFeedback",correlationId:message.messageId,messageId:++sequence},HOST);
    if(message.extensionPoint!=="document.customerorder.edit"||!uuid.test(message.objectId||"")){
      view("idle");status("Выберите покупателя и сохраните заказ.");return;
    }
    orderId=message.objectId;void refresh();
  }else if(message.name==="Change"&&orderId){
    draftRevision++;dirty=true;controls();
    if(!attempt)status("Сохраните изменения в заказе.");
  }else if(message.name==="Save"&&message.objectId===orderId&&message.extensionPoint==="document.customerorder.edit"){
    dirty=false;draftRevision++;
    if(busy)savedWhileBusy=true;
    else if(attempt)void verify();else void refresh();
  }
});
$("refresh").addEventListener("click",refresh);
$("spend-form").addEventListener("submit",apply);
$("points").addEventListener("input",()=>{requestId=null;updatePayable();});
$("maximum-button").addEventListener("click",()=>{$("points").value=String(quote.quote.max_spend_points);requestId=null;updatePayable();});
$("verify").addEventListener("click",verify);
controls();

window.addEventListener("pagehide",()=>{
 epoch++;token=null;quote=null;attempt=null;
 for(const pending of waiting.values()){clearTimeout(pending.timer);pending.reject(new Error("page_closed"));}
 waiting.clear();
});
