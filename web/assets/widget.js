// MoySklad v2: Open/Change/Save/UpdateRequest and one-use UserContextRequest.
// Host events provide UI context only. The backend rereads all financial evidence.
import {HOST,isHostMessage} from "./host.js";
const $=id=>document.getElementById(id);
let sequence=0,epoch=0,openMessage=null,orderId=null,token=null,quote=null;
let dirty=false,busy=false,attempt=null,requestId=null,draftRevision=0,savedWhileBusy=false;
let resumeRequired=false,resumeAuthorized=false,resumeSaveRevision=null;
let undo=null,undoRequestId=null,undoResumeAuthorized=false,undoSaveRevision=null,undoNotice=null,undoConfirmation=null;
const waiting=new Map();
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const documentErrors={
  vat_mode_missing:"МойСклад не вернул режим НДС. Обновите заказ; если ошибка остаётся, нужен администратор.",
  vat_mode_unsupported:"Этот режим НДС пока не поддерживается. Нужна проверка администратора.",
  customer_missing:"Выберите покупателя и сохраните заказ.",
  currency_unsupported:"Бонусы работают только с заказами в KZT.",
  exchange_rate_unsupported:"Не удалось подтвердить курс KZT в заказе. Нужна проверка администратора.",
  positions_missing:"В заказе нет сохранённых позиций. Добавьте товары и сохраните заказ.",
  positions_limit:"В заказе больше 1000 позиций. Для списания нужен администратор.",
  position_precision_unsupported:"Формат количества, цены или скидки пока не поддерживается. Нужна проверка администратора.",
  totals_mismatch:"Сумма позиций не совпадает с итогом МойСклад. Сохраните и обновите заказ; если ошибка остаётся, нужен администратор.",
  linked_documents_present:"У заказа есть связанные документы оплаты или отгрузки. Новое списание недоступно.",
  order_has_marker:"В заказе уже отмечено списание. Нужна сверка с администратором.",
  discount_not_representable:"Эту сумму бонусов нельзя списать точно. Измените сумму.",
  invalid_request:"Обновите виджет. Если ошибка повторяется, нужен администратор."
};
const readOnlyMessages={
  order_not_posted:"Заказ не проведён. Новое списание недоступно.",
  order_fulfilled:"По заказу уже есть оплата или отгрузка. Новое списание недоступно.",
  linked_documents_present:documentErrors.linked_documents_present,
  order_has_marker:documentErrors.order_has_marker,
  order_has_redemption:"В заказе уже есть операция списания. Обновите данные для проверки.",
  return_review:"По клиенту есть возврат или расхождение. Администратору: «Возвраты и сверка»."
};
const undoErrors={
  undo_restore_original:"Заказ изменён. Восстановите сохранённый вариант со скидкой или обратитесь к администратору.",
  undo_sale_locked:"У заказа есть оплата, отгрузка или возврат. Отмена через виджет недоступна.",
  undo_operation_changed:"Операция изменилась. Обновите виджет.",
  undo_not_confirmed:"Сохраните заказ и дождитесь подтверждения применения.",
  undo_generation_changed:"Операция уже отменена или заменена. Обновите виджет.",
  undo_not_representable:"Верните удалённые строки и исходные цены в заказе, сохраните и повторите отмену.",
  undo_balance_limit:"Возврат бонусов превысит допустимый баланс. Нужен администратор."
};
const digest=value=>typeof value==="string"&&/^[a-f0-9]{64}$/.test(value);
const amountFormat=new Intl.NumberFormat("ru-KZ");
const money=value=>{
 const minor=BigInt(value),fraction=minor%100n;return amountFormat.format(minor/100n)+(fraction?","+String(fraction).padStart(2,"0"):"")+" ₸";
};
function enteredPoints(){
 const raw=$("points").value;
 return /^[0-9]+$/.test(raw)&&Number.isSafeInteger(Number(raw))?Number(raw):null;
}
function hasPersonal(){const p=quote?.personal_discount;return Boolean(quote?.can_prepare&&p?.enabled&&p.can_apply&&!p.blocked_reason&&p.discount_minor>0);}
function personalSelected(){return hasPersonal()&&$("apply-personal").checked;}
function projection(){return personalSelected()?quote.personal_discount.quote:quote?.quote;}
function canSpend(){const value=projection();return Boolean(quote?.can_prepare&&!value?.blocked_reason&&value?.max_spend_points>0);}
function requestedPoints(){return personalSelected()&&(!canSpend()||$("points").value==="")?0:enteredPoints();}
function validAmount(){const value=requestedPoints();return value!==null&&value>=(personalSelected()?0:1)&&value<=(projection()?.max_spend_points??0);}
function preservedOperation(){return attempt?.apply_personal&&attempt.points===0?"Операция сохранена":"Резерв сохранён";}
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
  const personal=personalSelected(),spending=Boolean(quote&&canSpend()&&!attempt&&!undo),applying=Boolean(quote&&!attempt&&!undo&&(spending||personal));
  $("refresh").disabled=busy||!orderId;
  $("apply").disabled=busy||dirty||!applying||!validAmount()||Boolean(attempt);
  $("points").disabled=busy||dirty||!spending||Boolean(attempt);$("points").required=!personal;
  $("apply-personal").disabled=busy||dirty||!hasPersonal()||Boolean(attempt);
  $("apply").textContent=busy&&!attempt?"Применяем…":personal&&requestedPoints()===0?"Применить скидку":"Применить";
  $("spend-form").hidden=!spending;$("apply").hidden=!applying;
  $("verify").hidden=!attempt?.id||(attempt.state==="committed"&&attempt.verified===true);
  $("refresh").setAttribute("aria-busy",String(busy));$("spend-form").setAttribute("aria-busy",String(busy));
  $("maximum-button").disabled=$("points").disabled||!projection()?.max_spend_points;
  $("verify").disabled=busy||!attempt;
  $("resume").hidden=!resumeRequired||!attempt?.id||attempt.recoverable!==true;
  $("resume").disabled=busy||dirty||!resumeAuthorized||$("resume").hidden;
  $("resume").textContent=attempt?.apply_personal?"Продолжить применение":"Продолжить списание";
  $("undo-start").hidden=Boolean(undo)||!(attempt?.state==="committed"&&
    (attempt.verified===true||attempt.undo_restore_required===true)&&
    attempt.undo_available===true&&!attempt.original_buyer_id&&digest(attempt.undo_source_hash));
  $("undo-start").textContent=attempt?.undo_restore_required===true?"Восстановить исходные строки и отменить":"Отменить применение";
  $("undo-start").disabled=busy||dirty||Boolean(undoConfirmation)||$("undo-start").hidden;
  $("undo-verify").hidden=!undo?.id;$("undo-verify").disabled=busy||dirty||Boolean(undoConfirmation)||!undo?.id;
  $("undo-resume").hidden=!undo?.id||undo.recoverable!==true||Boolean(undo.original_buyer_id);
  $("undo-resume").disabled=busy||dirty||Boolean(undoConfirmation)||!undoResumeAuthorized||$("undo-resume").hidden;
  $("undo-resume").textContent=undo?.restore_required===true?"Восстановить исходные строки и отменить":"Продолжить отмену";
  $("undo-confirm-apply").disabled=busy||dirty;
  $("undo-confirm-cancel").disabled=busy;
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
    error.stage=path.includes("/undo/")?"undo":path.endsWith("/prepare")?"prepare":path.endsWith("/verify")?"verify":path.endsWith("/resume")?"resume":path.startsWith("/api/widget/orders/")?"load":"session";
    if(Object.hasOwn(documentErrors,payload?.reason)&&
       (payload.reason!=="discount_not_representable"||error.stage==="prepare"))error.reason=payload.reason;
    if(Object.hasOwn(undoErrors,payload?.reason))error.reason=payload.reason;
    if(typeof payload?.trace_id==="string"&&/^[a-f0-9]{12}$/.test(payload.trace_id))error.trace=payload.trace_id;
    if(["moysklad_permissions_required","operator_access_required","program_not_configured"].includes(payload?.error))error.code=payload.error;
    if(response.status===401)token=null;
    throw error;
  }
  const value=await response.json();
  if(generation!==epoch)throw new Error("document_changed");
  return value;
}
function explain(error){
  if(error.stage==="undo"){
    if(undoErrors[error.reason])return undoErrors[error.reason];
    if(error.status===401)return "Сессия истекла. Нажмите «Обновить».";
    if(error.status===403)return "Нет доступа к отмене. Нужен администратор.";
    return "Отмена не подтверждена. Обновите данные — повторно применять скидку не нужно.";
  }
  if(error.message==="host_rejected")return "МойСклад отклонил скидку. "+preservedOperation()+" — нужен администратор.";
  if(error.status===401)return "Сессия истекла. Нажмите «Обновить».";
  if(error.status===403)return error.code==="moysklad_permissions_required"?"Нет доступа к данным МойСклад. Проверьте подключение решения.":"Нет доступа к бонусам. Обратитесь к администратору.";
  if(error.status===409)return error.code==="program_not_configured"?"Программа ещё не настроена. Нужен администратор.":
    error.stage==="resume"?"Не удалось продолжить "+(attempt?.apply_personal?"применение":"списание")+". "+preservedOperation()+" — обновите данные или обратитесь к администратору.":
    "Заказ или баланс изменился. Обновите данные.";
  if(error.status===422){
    const fallback=error.stage==="load"?"Не удалось проверить данные заказа. Нужен администратор.":
      (error.stage==="verify"||error.stage==="resume")?"Не удалось проверить сохранённое "+(attempt?.apply_personal?"применение скидки":"списание")+". "+preservedOperation()+" — нужен администратор.":
      error.stage==="session"?"Не удалось подключить сессию. Обновите виджет.":
      "Не удалось проверить заказ перед списанием. Обновите данные.";
    // Verification already has a reservation: never suggest changing its customer or positions.
    const reason=error.reason==="discount_not_representable"&&personalSelected()?"Эту скидку нельзя применить точно. Нужен администратор.":documentErrors[error.reason];
    return (["verify","resume"].includes(error.stage)?fallback:(reason||fallback))+(error.trace?" Код BP-"+error.trace+".":"");
  }
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
  clearUndoConfirmation();
  undo=null;undoResumeAuthorized=false;undoSaveRevision=null;$("undo-panel").hidden=true;
  attempt=value;quote=null;view("pending");$("summary").hidden=true;$("pending").hidden=false;$("personal-row").hidden=true;
  const done=value.state==="committed"&&value.verified===true,personal=value.apply_personal===true;
  if(done){resumeRequired=false;resumeAuthorized=false;resumeSaveRevision=null;}
  else if(value.recovered||value.verified===false)resumeRequired=true;
  $("pending").dataset.state=done?"done":value.verified===false?"review":"waiting";
  $("pending-title").textContent=done?(personal?"Скидка применена":"Списано"):!value.id?"Проверьте операцию":
    value.verified===false?"Проверьте сохранение":value.recovered?"Проверяем сохранение":"Сохраните заказ";
  $("pending-text").textContent=!value.id?"Ответ не получен":
    personal?"Скидка клиента: −"+money(value.personal_discount_minor):pointsLabel(value.points)+(done?"":" в резерве");
  $("pending-bonus").hidden=!value.id||!personal||value.points<=0;
  $("pending-bonus").textContent=personal&&value.points>0?pointsLabel(value.points)+(done?" списано":" в резерве"):"";
  $("pending-note").textContent=value.original_buyer_id?"Верните первоначального покупателя в заказ и сохраните. Затем повторите отмену.":
    value.undo_restore_required===true?"Можно восстановить исходные строки и отменить применение. Текущие изменения товаров будут заменены.":done?"Заказ сохранён и проверен.":
    !value.id?"Обновите данные. Не применяйте "+(personal?"скидку":"бонусы")+" повторно.":
    resumeRequired&&value.recoverable===true?"Сохраните заказ в МойСклад, затем продолжите "+(personal?"применение скидки.":"списание."):
    value.verified===false&&value.state==="committed"?"Заказ изменён. Восстановите сохранённые условия и повторите отмену или обратитесь к администратору.":
    value.verified===false?"Сохраните заказ. Если сумма отличается — нужен администратор.":
    value.recovered?"Сверяем операцию с заказом.":"Нажмите «Сохранить» в МойСклад.";
  status(dirty&&done?"Верните заказ к сохранённому варианту со скидкой, затем отмените применение.":"");controls();
}
function showUndo(value){
  clearUndoConfirmation();
  undo=value;quote=null;resumeRequired=false;resumeAuthorized=false;resumeSaveRevision=null;
  view("undo");$("summary").hidden=true;$("pending").hidden=true;$("personal-row").hidden=true;$("undo-panel").hidden=false;
  $("undo-panel").dataset.state=value.recoverable===false&&value.reason?"review":"waiting";
  $("undo-title").textContent="Отмена применения";
  $("undo-note").textContent=value.original_buyer_id?"Верните первоначального покупателя в заказ и сохраните. Затем повторите отмену.":
    !value.id?"Ответ не получен. Обновите данные, чтобы проверить отмену.":
    value.restore_required===true&&value.recoverable===true?"Для отмены нужно восстановить исходные строки. Сохраните заказ, затем подтвердите восстановление.":
    value.reason&&undoErrors[value.reason]?undoErrors[value.reason]:
    dirty?"Сохраните заказ в МойСклад. Отмена завершится после проверки.":
    value.recovered?"Проверяем сохранённый заказ.":
    value.recoverable===true?"Сохраните заказ, затем продолжите отмену.":
    "Сохраните заказ в МойСклад. Отмена завершится после проверки.";
  status("");controls();
}
function renderQuote(value){
  clearUndoConfirmation();
  undo=null;undoRequestId=null;undoResumeAuthorized=false;undoSaveRevision=null;$("undo-panel").hidden=true;
  quote=value;attempt=null;resumeRequired=false;resumeAuthorized=false;resumeSaveRevision=null;$("summary").hidden=false;$("pending").hidden=true;
  $("order-label").textContent=value.name?"Заказ №"+value.name:"Заказ покупателя";
  renderBalance("balance",value.quote.balance);
  const reserved=Number.isSafeInteger(value.quote.available)&&value.quote.available<value.quote.balance;
  $("available-note").hidden=!reserved;$("available-note").textContent=reserved?"В резерве: "+amountFormat.format(value.quote.balance-value.quote.available):"";
  $("balances").hidden=false;$("apply-personal").checked=hasPersonal();
  view(value.quote.balance===0?"empty":"ready");
  quoteStatus();updatePayable();if(undoNotice&&!dirty)status(undoNotice);
}
function quoteStatus(){
  if(!quote)return;
  const value=quote,personal=personalSelected();
  status(dirty?"Сохраните изменения в заказе.":
    !value.can_prepare?"Программа ещё не настроена. Нужен администратор.":
    value.quote.blocked_reason?(readOnlyMessages[value.quote.blocked_reason]||"Списание приостановлено. Нужен администратор."):
    value.personal_discount?.blocked_reason==="discount_not_representable"?"Персональная скидка не рассчитана точно. "+(canSpend()?"Бонусы можно списать отдельно.":"Нужен администратор."):
    !personal&&value.quote.max_spend_points===0?(value.eligible_minor===0?"В заказе только услуги — бонусы не списываются.":
      value.quote.balance===0?"":"Бонусы пока недоступны. Проверьте резервы."):"");
}
async function refresh(){
  if(!orderId||busy)return;
  clearUndoConfirmation();
  const generation=epoch,id=orderId;busy=true;quote=null;resumeAuthorized=false;resumeSaveRevision=null;undoResumeAuthorized=false;undoSaveRevision=null;$("summary").hidden=true;$("pending").hidden=true;$("undo-panel").hidden=true;
  view("loading");status("Загружаем бонусы…");controls();
  try{
    await authenticate(generation);
    if(generation!==epoch)return;
    const value=await api("/api/widget/orders/"+id);
    if(generation!==epoch)return;
    if(value.undo){
      showUndo({...value.undo,recovered:true});
      if(!dirty)savedWhileBusy=true;
    }else if(value.redemption){
      showPending({...value.redemption,recovered:true});
      // Refresh resumes verification; a stored state alone never renders success.
      savedWhileBusy=true;
    }else renderQuote(value);
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
function updatePayable(){
  const raw=$("points").value,points=requestedPoints(),valid=validAmount(),value=projection(),personal=personalSelected();
  const invalid=raw!==""&&!valid&&canSpend();
  $("points").setAttribute("aria-invalid",String(invalid));$("amount-hint").hidden=!invalid;
  $("amount-hint").textContent=invalid?"От "+(personal?"0":"1")+" до "+amountFormat.format(value?.max_spend_points??0)+", целыми бонусами.":"";
  $("personal-row").hidden=!hasPersonal();document.querySelector("main").dataset.personal=String(hasPersonal());
  $("personal-limit").hidden=!hasPersonal()||!canSpend();$("personal-limit").textContent=hasPersonal()&&canSpend()?"до "+amountFormat.format(value.max_spend_points):"";
  if(quote){
    const p=quote.personal_discount;
    $("personal-rate").textContent=hasPersonal()?amountFormat.format(p.rate_bps/100)+"%":"";
    $("personal-value").textContent=hasPersonal()?personal?"−"+money(p.discount_minor):money(0):"";
    $("personal-conditions").hidden=!hasPersonal();$("personal-conditions").textContent=hasPersonal()?
      "Персональная скидка: "+amountFormat.format(p.rate_bps/100)+"%. "+(p.allow_bonus_stack?"Бонусы списываются после скидки.":"Бонусы вместе со скидкой не списываются."):"";
    renderBalance("maximum",value.max_spend_points);$("limit-block").hidden=!canSpend();$("points").max=String(value.max_spend_points);
    $("basis").textContent="Товары для расчёта: "+money(personal?p.eligible_minor_after:quote.eligible_minor)+".";
    const earn=value.potential_earn_points;
    $("earning").hidden=!quote.can_prepare||Boolean(value.blocked_reason)||!Number.isSafeInteger(earn)||earn<=0;
    $("earn-value").textContent=$("earning").hidden?"":"+"+pointsLabel(earn);
    const total=BigInt(personal?p.total_minor_after:quote.total_minor);
    $("payable").textContent=money(total-(valid?BigInt(points)*100n:0n));
  }else $("payable").textContent="";
  controls();
}
function finishBusy(generation){
  if(generation!==epoch)return;
  busy=false;controls();
  if(savedWhileBusy){
    savedWhileBusy=false;
    // Save can race the prepare/UpdateResponse or a provider read. Verify the
    // saved document after the in-flight operation, never treat Save as proof.
    queueMicrotask(()=>{if(generation===epoch){if(undo)void verifyUndo();else if(attempt)void verify();else void refresh();}});
  }
}
async function apply(event){
  event.preventDefault();
  if(busy||dirty||attempt||undo||(!canSpend()&&!personalSelected()))return;
  undoNotice=null;
  const points=requestedPoints(),personal=personalSelected(),personalPreview=quote?.personal_discount;
  if(!validAmount()){
    status("Введите целое число в пределах лимита.",true);return;
  }
  const generation=epoch,id=orderId,revision=draftRevision;
  busy=true;controls();requestId=requestId||crypto.randomUUID();
  try{
    const payload={points,request_id:requestId,source_hash:quote.saved_source_hash};
    if(personal)Object.assign(payload,{apply_personal:true,expected_policy_version:personalPreview.policy_version,expected_assignment_version:personalPreview.assignment_version});
    const result=await api("/api/widget/orders/"+id+"/prepare",{method:"POST",body:JSON.stringify(payload)});
    if(generation!==epoch)return;
    attempt=result;
    if(revision!==draftRevision||dirty||!result.update_state){
      resumeRequired=true;showPending(result);status("Заказ изменился. "+preservedOperation()+" — проверьте операцию.",true);return;
    }
    status(personal?"Применяем скидку…":"Применяем бонусы…");
    // Dispatch may precede Save even when the host acknowledgement arrives later.
    dirty=true;controls();
    await hostRequest("UpdateRequest",{updateState:result.update_state},"UpdateResponse");
    if(generation!==epoch)return;
    showPending(result);
  }catch(error){
    if(generation===epoch){
      const rejected=!attempt && [400,401,403,404,409,422].includes(error.status);
      if(rejected){
        // An explicit rejection is different from an ambiguous network failure.
        // Require a fresh quote, without inventing a successful reservation.
        requestId=null;
        // This typed rejection occurs before any reservation. Keep the entered
        // value and quote so staff can choose another amount without starting over.
        if(error.reason!=="discount_not_representable"||!quote){
          quote=null;$("summary").hidden=true;$("pending").hidden=true;
        }
      }else{
        // A lost prepare response may already have reserved points.
        // Preserve uncertainty and never dispatch a second mutation blindly.
        attempt=attempt||{state:"verifying",points,...(personal?{apply_personal:true,personal_discount_minor:personalPreview.discount_minor,personal_rate_bps:personalPreview.rate_bps}:{})};
        resumeRequired=true;resumeAuthorized=false;resumeSaveRevision=null;showPending(attempt);
      }
      status(explain(error),true);
    }
  }finally{finishBusy(generation);}
}
async function verify(){
  if(busy||!orderId||!attempt||undo)return;
  const generation=epoch,id=orderId,revision=draftRevision;
  const savedForResume=resumeSaveRevision===revision;
  resumeAuthorized=false;
  busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/verify",{method:"POST"});
    if(generation!==epoch)return;
    showPending(result);
    resumeAuthorized=savedForResume&&resumeSaveRevision===revision&&draftRevision===revision&&!dirty&&
      result.recoverable===true&&typeof result.resume_source_hash==="string"&&/^[a-f0-9]{64}$/.test(result.resume_source_hash);
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
async function resume(){
  if(busy||dirty||!resumeAuthorized||!resumeRequired||!attempt?.id||attempt.recoverable!==true)return;
  const generation=epoch,id=orderId,revision=draftRevision;
  const body={attempt_id:attempt.id,source_hash:attempt.resume_source_hash};
  // A Save and matching authoritative verification authorize one explicit request.
  resumeAuthorized=false;resumeSaveRevision=null;busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/resume",{method:"POST",body:JSON.stringify(body)});
    if(generation!==epoch)return;
    if(revision!==draftRevision||dirty||!result.update_state){
      resumeRequired=true;showPending(result);
      status("Заказ изменился. "+preservedOperation()+" — проверьте операцию.",true);return;
    }
    status(attempt.apply_personal?"Продолжаем применение…":"Продолжаем списание…");
    // Preserve a Save or Change received after dispatch and before its acknowledgement.
    dirty=true;controls();
    await hostRequest("UpdateRequest",{updateState:result.update_state},"UpdateResponse");
    if(generation!==epoch)return;
    resumeRequired=false;showPending(result);
  }catch(error){
    if(generation===epoch){resumeRequired=true;showPending(attempt);status(explain(error),true);}
  }finally{
    if(generation===epoch){resumeAuthorized=false;resumeSaveRevision=null;}
    finishBusy(generation);
  }
}
function clearUndoConfirmation(){
  const visible=Boolean(undoConfirmation);
  undoConfirmation=null;$("undo-confirm").hidden=true;
  if(visible){$("pending").hidden=Boolean(undo)||!attempt;$("undo-panel").hidden=!undo;}
}
function confirmUndo(mode){
  const value=mode==="start"?attempt:undo;
  const source=mode==="start"?value?.undo_source_hash:value?.resume_source_hash;
  if(busy||dirty||!value?.id||!digest(source))return;
  undoConfirmation={mode,epoch,revision:draftRevision,id:value.id,source};
  $("pending").hidden=true;$("undo-panel").hidden=true;$("undo-confirm").hidden=false;
  controls();$("undo-confirm-apply").focus();
}
function acceptUndoConfirmation(){
  const confirmation=undoConfirmation;
  if(!confirmation)return;
  const value=confirmation.mode==="start"?attempt:undo;
  const source=confirmation.mode==="start"?value?.undo_source_hash:value?.resume_source_hash;
  const valid=!busy&&!dirty&&confirmation.epoch===epoch&&confirmation.revision===draftRevision&&
    confirmation.id===value?.id&&confirmation.source===source;
  clearUndoConfirmation();controls();
  if(!valid){status("Заказ изменился. Проверьте данные и повторите отмену.",true);return;}
  if(confirmation.mode==="start")void startUndo(true);else void resumeUndo(true);
}
function completeUndo(value){
  if(value?.state!=="completed"||value.verified!==true)return false;
  clearUndoConfirmation();
  // A host acknowledgement or Save event is never financial proof.
  undo=null;attempt=null;quote=null;requestId=null;undoRequestId=null;
  undoResumeAuthorized=false;undoSaveRevision=null;undoNotice="Применение отменено. Можно изменить заказ.";
  $("undo-panel").hidden=true;savedWhileBusy=true;return true;
}
async function startUndo(restoreOriginal=false){
  if(busy||dirty||undo||!attempt?.id||attempt.state!=="committed"||
     (attempt.verified!==true&&attempt.undo_restore_required!==true)||attempt.original_buyer_id||
     attempt.undo_available!==true||!digest(attempt.undo_source_hash))return;
  if(attempt.undo_restore_required===true&&restoreOriginal!==true){confirmUndo("start");return;}
  const generation=epoch,id=orderId,revision=draftRevision,applied=attempt;
  undoNotice=null;undoRequestId=undoRequestId||crypto.randomUUID();
  const body={request_id:undoRequestId,attempt_id:applied.id,source_hash:applied.undo_source_hash};
  if(restoreOriginal===true)body.restore_original=true;
  busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/undo/prepare",{method:"POST",body:JSON.stringify(body)});
    if(generation!==epoch)return;
    if(completeUndo(result.undo))return;
    showUndo(result.undo);
    if(revision!==draftRevision||dirty||!result.update_state){
      status(revision!==draftRevision||dirty?"Заказ изменился. Отмена не подтверждена — проверьте сохранение.":
        "Проверьте сохранение заказа перед продолжением отмены.",true);return;
    }
    // Dispatch itself may change the draft even when its acknowledgement is lost.
    dirty=true;controls();
    await hostRequest("UpdateRequest",{updateState:result.update_state},"UpdateResponse");
    if(generation!==epoch)return;
    showUndo(result.undo);
  }catch(error){
    error.stage="undo";
    if(generation===epoch){
      if(!undo&&[400,401,403,404,409,422].includes(error.status)){
        undoRequestId=null;showPending(applied);
      }else{
        showUndo(undo||{state:"prepared",attempt_id:applied.id});
      }
      status(explain(error),true);
    }
  }finally{finishBusy(generation);}
}
async function verifyUndo(){
  if(busy||dirty||!orderId||!undo?.id)return;
  const generation=epoch,id=orderId,revision=draftRevision;
  const savedForResume=undoSaveRevision===revision;
  undoResumeAuthorized=false;busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/undo/verify",{method:"POST",body:JSON.stringify({undo_id:undo.id})});
    if(generation!==epoch)return;
    const value=result.undo;
    if(completeUndo(value))return;
    showUndo(value);
    if(dirty)status("Сохраните заказ, чтобы проверить отмену.");
    undoResumeAuthorized=savedForResume&&undoSaveRevision===revision&&draftRevision===revision&&!dirty&&
      value?.recoverable===true&&digest(value.resume_source_hash);
  }catch(error){if(generation===epoch)status(explain(error),true);}
  finally{finishBusy(generation);}
}
async function resumeUndo(restoreOriginal=false){
  if(busy||dirty||!undoResumeAuthorized||!undo?.id||undo.recoverable!==true||undo.original_buyer_id||!digest(undo.resume_source_hash))return;
  if(undo.restore_required===true&&restoreOriginal!==true){confirmUndo("resume");return;}
  const generation=epoch,id=orderId,revision=draftRevision,value=undo;
  const body={undo_id:value.id,source_hash:value.resume_source_hash};
  if(restoreOriginal===true)body.restore_original=true;
  undoResumeAuthorized=false;undoSaveRevision=null;busy=true;controls();
  try{
    await authenticate(generation);if(generation!==epoch)return;
    const result=await api("/api/widget/orders/"+id+"/undo/resume",{method:"POST",body:JSON.stringify(body)});
    if(generation!==epoch)return;
    if(completeUndo(result.undo))return;
    showUndo(result.undo);
    if(revision!==draftRevision||dirty||!result.update_state){
      status(revision!==draftRevision||dirty?"Заказ изменился. Отмена не подтверждена — проверьте сохранение.":
        "Проверьте сохранение заказа перед продолжением отмены.",true);return;
    }
    // Dispatch itself may change the draft even when its acknowledgement is lost.
    dirty=true;controls();
    await hostRequest("UpdateRequest",{updateState:result.update_state},"UpdateResponse");
    if(generation!==epoch)return;
    showUndo(result.undo);
  }catch(error){
    error.stage="undo";
    if(generation===epoch){showUndo(undo||value);status(explain(error),true);}
  }finally{
    if(generation===epoch){undoResumeAuthorized=false;undoSaveRevision=null;}
    finishBusy(generation);
  }
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
    dirty=false;busy=false;draftRevision=0;savedWhileBusy=false;resumeRequired=false;resumeAuthorized=false;resumeSaveRevision=null;
    undo=null;undoRequestId=null;undoResumeAuthorized=false;undoSaveRevision=null;undoNotice=null;clearUndoConfirmation();$("undo-panel").hidden=true;
    for(const pending of waiting.values()){clearTimeout(pending.timer);pending.reject(new Error("document_changed"));}
    waiting.clear();$("summary").hidden=true;$("pending").hidden=true;$("points").value="";$("apply-personal").checked=false;$("personal-row").hidden=true;$("pending-bonus").hidden=true;$("order-label").textContent="";$("amount-hint").textContent="";$("payable").textContent="";$("points").setAttribute("aria-invalid","false");
    view("loading");status("Загружаем бонусы…");controls();
    if(message.extensionPoint==="document.customerorder.edit")
      parent.postMessage({name:"OpenFeedback",correlationId:message.messageId,messageId:++sequence},HOST);
    if(message.extensionPoint!=="document.customerorder.edit"||!uuid.test(message.objectId||"")){
      view("idle");status("Выберите покупателя и сохраните заказ.");return;
    }
    orderId=message.objectId;void refresh();
  }else if(message.name==="Change"&&orderId){
    clearUndoConfirmation();
    draftRevision++;dirty=true;resumeAuthorized=false;resumeSaveRevision=null;undoResumeAuthorized=false;undoSaveRevision=null;undoNotice=null;controls();
    if(undo)status("Сохраните заказ, чтобы проверить отмену.");
    else if(attempt?.state==="committed")status("Верните заказ к сохранённому варианту со скидкой, затем отмените применение.");
    else if(!attempt)status("Сохраните изменения в заказе.");
  }else if(message.name==="Save"&&message.objectId===orderId&&message.extensionPoint==="document.customerorder.edit"){
    clearUndoConfirmation();dirty=false;draftRevision++;
    resumeAuthorized=false;undoResumeAuthorized=false;undoSaveRevision=undo?.id?draftRevision:null;
    resumeSaveRevision=attempt?.id&&attempt.state==="verifying"?draftRevision:null;
    if(busy)savedWhileBusy=true;
    else if(undo)void verifyUndo();else if(attempt)void verify();else void refresh();
  }
});
$("refresh").addEventListener("click",refresh);
$("spend-form").addEventListener("submit",apply);
$("points").addEventListener("input",()=>{requestId=null;undoNotice=null;updatePayable();});
$("maximum-button").addEventListener("click",()=>{$("points").value=String(projection().max_spend_points);requestId=null;updatePayable();});
$("apply-personal").addEventListener("change",()=>{requestId=null;quoteStatus();updatePayable();});
$("verify").addEventListener("click",verify);
$("resume").addEventListener("click",resume);
$("undo-start").addEventListener("click",()=>void startUndo());
$("undo-verify").addEventListener("click",verifyUndo);
$("undo-resume").addEventListener("click",()=>void resumeUndo());
$("undo-confirm-apply").addEventListener("click",acceptUndoConfirmation);
$("undo-confirm-cancel").addEventListener("click",()=>{clearUndoConfirmation();controls();});
controls();

window.addEventListener("pagehide",()=>{
 epoch++;token=null;quote=null;attempt=null;resumeAuthorized=false;resumeRequired=false;resumeSaveRevision=null;
 undo=null;undoRequestId=null;undoResumeAuthorized=false;undoSaveRevision=null;undoNotice=null;clearUndoConfirmation();
 for(const pending of waiting.values()){clearTimeout(pending.timer);pending.reject(new Error("page_closed"));}
 waiting.clear();
});
