// MoySklad user-context transport. Secrets stay in memory and never enter diagnostics.
import {HOST,isHostWindow,isHostMessage} from "./host.js";
let sequence = 0;
let opened = false;
window.addEventListener("message", event => {
  if (!isHostMessage(event)) return;
  if (event.data?.name === "Open" && Number.isSafeInteger(event.data.messageId)) {
    opened = true;
  }
});
export function requestContext() {
  if (parent === window) return Promise.reject(new Error("Откройте БонусПро из МойСклад."));
  return new Promise((resolve,reject) => {
    const id=++sequence;
    const seen={parent:false,host:false,open:opened,response:false,string:false,error:false};
    let settled=false, sendTimer=null;
    const code=()=> "BP-CTX-P"+Number(seen.parent)+"H"+Number(seen.host)+
      "O"+Number(seen.open)+"R"+Number(seen.response)+"S"+Number(seen.string)+"E"+Number(seen.error);
    const finish=(error,token) => {
      if(settled)return;
      settled=true;
      clearTimeout(timer);clearTimeout(sendTimer);
      window.removeEventListener("message",listener);
      window.removeEventListener("load",schedule);
      error ? reject(error) : resolve(token);
    };
    const listener=event => {
      if(!isHostWindow(event.source))return;
      seen.parent=true;
      if(event.origin!==HOST)return;
      seen.host=true;
      if(typeof event.data==="string"){seen.string=true;return;}
      const data=event.data;
      if(!data||typeof data!=="object")return;
      if(data.name==="Open")seen.open=true;
      if(data.name==="UserContextResponse")seen.response=true;
      if(data.name==="InvalidMessageError")seen.error=true;
      const matches=data.correlationId===id ||
        (data.name==="InvalidMessageError" && data.correlationId==null &&
         data.invalidMessage?.messageId===id);
      if(!matches)return;
      if(data.name==="InvalidMessageError"){
        const value=data.errors?.[0]?.code;
        const reason=Number.isSafeInteger(value)&&value>=1000&&value<=1006?value:"REJECTED";
        finish(new Error("МойСклад отклонил подключение. Код BP-CTX-"+reason+"."));
      }else if(data.name!=="UserContextResponse"||typeof data.token!=="string"||
               !data.token.trim()||data.token.length>4096){
        finish(new Error("МойСклад не подтвердил доступ. Код BP-CTX-RESPONSE."));
      }else finish(null,data.token);
    };
    const send=()=>{
      if(settled)return;
      try{parent.postMessage({name:"UserContextRequest",messageId:id},HOST);}
      catch{finish(new Error("Не удалось запросить подключение. Код BP-CTX-SEND."));}
    };
    // The parent may register the iframe transport in its load handler.
    // Send on the next task after iframe load rather than during module execution.
    const schedule=()=>{sendTimer=setTimeout(send,0);};
    const timer=setTimeout(()=>finish(new Error(
      "МойСклад не ответил. Повторите подключение. Код "+code()+".")),10000);
    window.addEventListener("message",listener);
    if(document.readyState==="complete")schedule();
    else window.addEventListener("load",schedule,{once:true});
  });
}
