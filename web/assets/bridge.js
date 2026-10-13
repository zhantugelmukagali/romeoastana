// Wire contract: moysklad/js-widget-sdk requestUserContextToken (reviewed 2026-10-13).
// Tokens stay in memory. Parent/source/origin are checked before accepting any message.
const HOST = "https://online.moysklad.ru";
let sequence = 0;
window.addEventListener("message", event => {
  if (event.source !== parent || event.origin !== HOST) return;
  if (event.data?.name === "Open" && Number.isSafeInteger(event.data.messageId)) {
    parent.postMessage({name:"OpenFeedback",correlationId:event.data.messageId,messageId:++sequence}, HOST);
  }
});
export function requestContext() {
  if (parent === window) return Promise.reject(new Error("Откройте БонусПро из МойСклад."));
  return new Promise((resolve,reject) => {
    const id=++sequence;
    const finish=(error,token) => {
      clearTimeout(timer);
      window.removeEventListener("message", listener);
      error ? reject(error) : resolve(token);
    };
    const listener=event => {
      if (event.source !== parent || event.origin !== HOST || event.data?.correlationId !== id) return;
      const data=event.data;
      if (data.name !== "UserContextResponse" || typeof data.token !== "string" ||
          !data.token.trim() || data.token.length>4096) {
        finish(new Error("МойСклад не подтвердил доступ. Повторите подключение."));
      } else finish(null,data.token);
    };
    const timer=setTimeout(()=>finish(new Error("МойСклад не ответил. Повторите подключение.")),10000);
    window.addEventListener("message",listener);
    parent.postMessage({name:"UserContextRequest",messageId:id},HOST);
  });
}
