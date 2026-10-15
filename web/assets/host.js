// GWT can execute host handlers in a hidden sibling frame, not the top window.
export const HOST="https://online.moysklad.ru";
export function isHostWindow(source){
  if(!source||parent===window)return false;
  try{
    return source===parent || (source!==window && source.parent===parent);
  }catch{return false;}
}
export function isHostMessage(event){
  return event.origin===HOST && isHostWindow(event.source);
}
