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
 const minor=BigInt(value);return amountFormat.format(minor/100n)+","+String(minor%100n).padStart(2,"0")+" ₸";
};
function enteredPoints(){
 const raw=$("points").value;
 return /^[0-9]+$/.test(raw)&&Number.isSafeInteger(Number(raw))?Number(raw):null;
}
function canSpend(){return Boolean(quote?.can_prepare&&!quote.quote.blocked_reason&&quote.quote.max_spend_points>0);}
function validAmount(){const value=enteredPoints();return value!==null&&value>0&&value<=quote?.quote.max_spend_points;}
function status(text,error=false){$("status").textContent=text;$("status").dataset.error=String(error);}
function controls(){
  $("refresh").disabled=busy||!orderId;
  $("apply").disabled=busy||dirty||!canSpend()||!validAmount()||Boolean(attempt);
  $("points").disabled=busy||dirty||!canSpend()||Boolean(attempt);
  $("apply").textContent=busy&&!attempt?"Применяем…":"Применить к заказу";
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
    const error=new Error("api_error");error.status=response.status;
    if(response.status===401)token=null;
    throw error;
  }
  const value=await response.json();
  if(generation!==epoch)throw new Error("document_changed");
  return value;
}
function explain(error){
  if(error.message==="host_rejected")return "МойСклад отклонил изменение формы. Резерв сохранён; не повторяйте списание, обратитесь к администратору.";
  if(error.status===401)return "Сессия истекла. Нажмите «Обновить» для повторного подключения.";
  if(error.status===403)return "Нет доступа к бонусным операциям. Попросите администратора открыть «Сотрудники» в настройках БонусПро и выдать вам доступ.";
  if(error.status===409)return "Заказ или баланс изменился. Обновите данные и проверьте состояние операции.";
  if(error.status===422)return "Не удалось точно рассчитать скидку для этого заказа. Проверьте сумму, НДС и позиции; списание не подтверждено.";
  return "Нет подтверждения операции. Проверьте сохранение. Повторное списание не выполняется.";
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
  attempt=value;$("summary").hidden=true;$("pending").hidden=false;
  const done=value.state==="committed"&&value.verified===true;
  $("pending").dataset.state=done?"done":"waiting";
  $("pending-title").textContent=done?"Бонусы списаны":!value.id?"Проверьте состояние операции":
    value.verified===false?"Проверьте сохранённый заказ":"Осталось сохранить заказ";
  $("pending-note").textContent=done?"Сумма проверена по сохранённому заказу МойСклад.":
    !value.id?"Нажмите «Обновить», чтобы найти операцию на сервере. Повторное списание заблокировано.":
    value.verified===false?"Проверьте результат кнопкой ниже. При расхождении обратитесь к администратору; резерв не снимается автоматически.":
    "Нажмите «Сохранить» в самом заказе. Виджет проверит сумму автоматически; при необходимости нажмите кнопку ниже.";
  $("pending-text").textContent=done?("Списано "+value.points+" бонусов."):
    (value.id?(value.state==="committed"?("Операция на "+value.points+" бонусов. Проверьте сохранённый заказ."):("Зарезервировано "+value.points+" бонусов. Итоговое списание ещё не подтверждено.")):"Статус резервирования неизвестен. Обновите данные заказа перед повторной попыткой.");
  // A stored committed state alone isn't fresh evidence that the order still matches.
  status(done?"Списание сохранено.":"Сохраните заказ в МойСклад, затем проверьте результат.");
  controls();
}
async function refresh(){
  if(!orderId||busy)return;
  const generation=epoch,id=orderId;busy=true;quote=null;$("summary").hidden=true;
  $("order-label").textContent="";status("Загружаем актуальные бонусы…");controls();
  try{
    await authenticate(generation);
    if(generation!==epoch)return;
    const value=await api("/api/widget/orders/"+id);
    if(generation!==epoch)return;
    if(value.redemption){
      showPending({...value.redemption,verified:false});
      status("Для заказа уже есть операция. Проверьте её по сохранённым данным.");
    }else{
      quote=value;attempt=null;$("summary").hidden=false;$("pending").hidden=true;
      $("order-label").textContent=value.name?"Заказ №"+value.name:"Заказ покупателя";
      $("balance").textContent=value.quote.balance+" бонусов";
      $("available-note").textContent=Number.isSafeInteger(value.quote.available)&&value.quote.available!==value.quote.balance
        ?"Свободно: "+amountFormat.format(value.quote.available)+" · Часть бонусов в резерве":"Бонусы покупателя";
      $("maximum").textContent=value.quote.max_spend_points+" бонусов";
      $("basis").textContent="Товары: "+money(value.eligible_minor)+". К оплате: "+money(value.total_minor)+".";
      $("points").max=String(value.quote.max_spend_points);
      status(dirty?"В заказе есть изменения. Сначала сохраните его в МойСклад.":
        (!value.can_prepare?"Списание пока выключено: настройка и проверка магазина не завершены.":
          value.quote.blocked_reason?"Операция требует проверки администратора.":
            value.quote.max_spend_points===0?(value.eligible_minor===0?"В заказе нет товаров для списания. Доставка и услуги не участвуют.":
              value.quote.balance===0?"У покупателя пока нет бонусов. Начисление — после полной оплаты и отгрузки.":
              "Доступных для списания бонусов нет: проверьте резервы и лимит заказа."):
            "Укажите сумму бонусов и примените её к заказу."));
      updatePayable();
    }
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
function updatePayable(){
  const points=enteredPoints(),raw=$("points").value,valid=validAmount();
  const invalid=raw!==""&&!valid;
  $("points").setAttribute("aria-invalid",String(invalid));
  $("amount-hint").textContent=invalid?"Введите целое число от 1 до "+(quote?.quote.max_spend_points??0)+".":
    quote?"До "+amountFormat.format(quote.quote.max_spend_points)+" бонусов. 1 бонус = 1 ₸.":"";
  $("amount-hint").dataset.error=String(invalid);
  $("payable").textContent=quote&&valid?"К оплате после списания: "+money(quote.total_minor-points*100):
    quote?"К оплате без списания: "+money(quote.total_minor):"";
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
    status("Укажите целое число в пределах доступного списания.",true);return;
  }
  const generation=epoch,id=orderId,revision=draftRevision;
  busy=true;controls();requestId=requestId||crypto.randomUUID();
  try{
    const result=await api("/api/widget/orders/"+id+"/prepare",{method:"POST",
      body:JSON.stringify({points,request_id:requestId,source_hash:quote.saved_source_hash})});
    if(generation!==epoch)return;
    attempt=result;
    if(revision!==draftRevision||dirty||!result.update_state){
      showPending(result);status("Данные изменились или запрос уже выполнялся. Проверьте сохранение; резерв сохранён.",true);return;
    }
    status("Применяем скидку к форме заказа…");
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
    if(!result.verified)status("Сохранённый заказ не совпадает с рассчитанной скидкой. Резерв сохранён; нужна проверка.",true);
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
    status("Загружаем бонусы заказа…");controls();
    if(message.extensionPoint==="document.customerorder.edit")
      parent.postMessage({name:"OpenFeedback",correlationId:message.messageId,messageId:++sequence},HOST);
    if(message.extensionPoint!=="document.customerorder.edit"||!uuid.test(message.objectId||"")){
      status("Сначала сохраните заказ с выбранным покупателем в МойСклад.");return;
    }
    orderId=message.objectId;void refresh();
  }else if(message.name==="Change"&&orderId){
    draftRevision++;dirty=true;controls();
    if(!attempt)status("В заказе есть изменения. Сначала сохраните его в МойСклад.");
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
