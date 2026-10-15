const tabs=[...document.querySelectorAll('[role="tab"]')];
function select(id,focus=false){
 const match=tabs.find(tab=>tab.getAttribute("aria-controls")===id)||tabs[0];
 for(const tab of tabs){
  const active=tab===match;tab.setAttribute("aria-selected",String(active));tab.tabIndex=active?0:-1;
  document.getElementById(tab.getAttribute("aria-controls")).hidden=!active;
 }
 if(focus)match.focus();
}
tabs.forEach((tab,index)=>{
 tab.onclick=()=>select(tab.getAttribute("aria-controls"));
 tab.onkeydown=event=>{
  let target;
  if(event.key==="ArrowRight")target=(index+1)%tabs.length;
  if(event.key==="ArrowLeft")target=(index+tabs.length-1)%tabs.length;
  if(event.key==="Home")target=0;if(event.key==="End")target=tabs.length-1;
  if(target!==undefined){event.preventDefault();select(tabs[target].getAttribute("aria-controls"),true);}
 };
});
function fromHash(){
 const id=location.hash.slice(1);
 if(["widget-settings","launch-settings","processing-settings"].includes(id))select("connection-panel");
 else if(id==="history")select("history-panel");
}
select("rules-panel");fromHash();window.addEventListener("hashchange",fromHash);
