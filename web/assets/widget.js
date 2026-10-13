// MoySklad v2: Open/Change/Save/UpdateRequest and one-use UserContextRequest.
// Host events provide UI context only. The backend rereads all financial evidence.
const HOST="https://online.moysklad.ru";
const $=id=>document.getElementById(id);
let sequence=0,epoch=0,openMessage=null,orderId=null,token=null,quote=null;
let dirty=false,busy=false,attempt=null,requestId=null,draftRevision=0;
const waiting=new Map();
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const money=value=>new Intl.NumberFormat("ru-KZ",{style:"currency",currency:"KZT"}).format(value/100);
function status(text,error=false){$("status").textContent=text;$("status").dataset.error=String(error);}
function controls(){
  $("refresh").disabled=busy||!orderId;
  $("apply").disabled=busy||dirty||!quote?.can_prepare||Boolean(attempt);
  $("points").disabled=busy||Boolean(attempt);
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
  const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",
    ...(token?{Authorization:"Bearer "+token}:{}),...options.headers},signal:AbortSignal.timeout(15000)});
  if(!response.ok){
    const error=new Error("api_error");error.status=response.status;
    if(response.status===401)token=null;
    throw error;
  }
  return response.json();
}
function explain(error){
  if(error.status===401)return "Сессия истекла. Нажмите «Обновить» для повторного подключения.";
  if(error.status===403)return "Недостаточно прав. На этом этапе доступ к операциям есть только у администратора.";
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
  const done=value.state==="committed"&&value.verified!==false;
  $("pending-text").textContent=done?("Списано "+value.points+" бонусов."):
    (value.id?(value.state==="committed"?("Операция на "+value.points+" бонусов. Проверьте сохранённый заказ."):("Зарезервировано "+value.points+" бонусов. Итоговое списание ещё не подтверждено.")):"Статус резервирования неизвестен. Обновите данные заказа перед повторной попыткой.");
  // A stored committed state alone isn't fresh evidence that the order still matches.
  status(done?"Списание сохранено.":"Сохраните заказ в МойСклад, затем проверьте результат.");
  controls();
}
async function refresh(){
  if(!orderId||busy)return;
  const generation=epoch,id=orderId;busy=true;controls();
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
      $("balance").textContent=value.quote.balance+" бонусов";
      $("maximum").textContent=value.quote.max_spend_points+" бонусов";
      $("basis").textContent="Товары: "+money(value.eligible_minor)+". К оплате: "+money(value.total_minor)+".";
      $("points").max=String(value.quote.max_spend_points);
      status(dirty?"В заказе есть изменения. Сначала сохраните его в МойСклад.":
        (!value.can_prepare?"Списание пока выключено: настройка и проверка магазина не завершены.":
          value.quote.blocked_reason?"Операция требует проверки администратора.":
            "Укажите сумму бонусов и примените её к заказу."));
      updatePayable();
    }
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{if(generation===epoch){busy=false;controls();}}
}
function updatePayable(){
  const raw=$("points").value;
  const points=/^[0-9]+$/.test(raw)?Number(raw):NaN;
  $("payable").textContent=quote&&Number.isSafeInteger(points)&&points<=quote.quote.max_spend_points
    ?"После списания: "+money(quote.total_minor-points*100):"";
}
async function apply(event){
  event.preventDefault();
  if(busy||dirty||attempt||!quote?.can_prepare)return;
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
      // A lost prepare response may already have reserved points. Never generate another request.
      attempt=attempt||{state:"verifying",points};
      showPending(attempt);status(explain(error),true);
    }
  }finally{if(generation===epoch){busy=false;controls();}}
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
  finally{if(generation===epoch){busy=false;controls();}}
}
window.addEventListener("message",event=>{
  if(event.source!==parent||event.origin!==HOST||!event.data||typeof event.data!=="object")return;
  const message=event.data;
  const request=waiting.get(message.correlationId);
  if(request){
    waiting.delete(message.correlationId);clearTimeout(request.timer);
    if(request.epoch!==epoch||message.name!==request.responseName)request.reject(new Error("host_rejected"));
    else request.resolve(message);
    return;
  }
  if(message.name==="Open"&&Number.isSafeInteger(message.messageId)){
    if(openMessage===message.messageId)return;
    epoch++;openMessage=message.messageId;orderId=null;token=null;quote=null;attempt=null;requestId=null;
    dirty=false;busy=false;draftRevision=0;
    for(const pending of waiting.values()){clearTimeout(pending.timer);pending.reject(new Error("document_changed"));}
    waiting.clear();$("summary").hidden=true;$("pending").hidden=true;$("points").value="";
    status("Загружаем бонусы заказа…");controls();
    if(message.extensionPoint==="document.customerorder.edit")
      parent.postMessage({name:"OpenFeedback",correlationId:message.messageId},HOST);
    if(message.extensionPoint!=="document.customerorder.edit"||!uuid.test(message.objectId||"")){
      status("Сначала сохраните заказ с выбранным покупателем в МойСклад.");return;
    }
    orderId=message.objectId;void refresh();
  }else if(message.name==="Change"&&orderId){
    draftRevision++;dirty=true;controls();
    if(!attempt)status("В заказе есть изменения. Сначала сохраните его в МойСклад.");
  }else if(message.name==="Save"&&message.objectId===orderId&&message.extensionPoint==="document.customerorder.edit"){
    dirty=false;draftRevision++;
    if(!busy){if(attempt)void verify();else void refresh();}
  }
});
$("refresh").addEventListener("click",refresh);
$("spend-form").addEventListener("submit",apply);
$("points").addEventListener("input",()=>{requestId=null;updatePayable();});
$("maximum-button").addEventListener("click",()=>{$("points").value=String(quote.quote.max_spend_points);requestId=null;updatePayable();});
$("verify").addEventListener("click",verify);
controls();
