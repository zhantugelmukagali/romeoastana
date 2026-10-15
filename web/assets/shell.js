const icons={
 overview:"M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z",
 customers:"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
 activity:"M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3M8 3v5h5M8 3l5 5M9 15l3 3 9-9",
 review:"M12 8v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
 settings:"M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
 migration:"M12 3v12m-4-4 4 4 4-4M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4",
 staff:"M12 3 3 7v6c0 5 9 8 9 8s9-3 9-8V7ZM8 12l3 3 5-5"};
function icon(key){const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 24 24");svg.setAttribute("aria-hidden","true");const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",icons[key]);svg.append(path);return svg;}
const aside=document.createElement("aside");aside.className="app-sidebar";
const brand=document.createElement("a");brand.className="app-brand";brand.href="/admin#overview";
const mark=document.createElement("span");mark.className="brand-mark";mark.textContent="Б";
const word=document.createElement("strong");word.textContent="БонусПро";brand.append(mark,word);aside.append(brand);
const caption=document.createElement("p");caption.className="nav-caption";caption.textContent="УПРАВЛЕНИЕ БОНУСАМИ";aside.append(caption);
const nav=document.createElement("nav");nav.className="app-navigation";nav.setAttribute("aria-label","Основная навигация");
for(const [id,label,href] of [["overview","Обзор","/admin#overview"],["customers","Клиенты","/admin#customers"],["activity","Операции","/admin#activity"],["review","Возвраты и сверка","/reconciliation"],["settings","Настройки","/settings"],["staff","Сотрудники","/staff"],["migration","Перенос остатков","/migration"]]){
 const a=document.createElement("a");a.href=href;a.dataset.nav=id;a.append(icon(id));const text=document.createElement("span");text.textContent=label;a.append(text);nav.append(a);
}
aside.append(nav);
const footer=document.createElement("div");footer.className="sidebar-footer";footer.textContent="Внутри вашего МойСклад";
const dot=document.createElement("span");dot.className="connection-dot";footer.prepend(dot);aside.append(footer);
document.body.prepend(aside);document.body.classList.add("with-shell");
function active(){
 let id=({"/settings":"settings","/staff":"staff","/migration":"migration","/reconciliation":"review","/returns":"review"})[location.pathname];
 if(!id)id=["customers","activity"].includes(location.hash.slice(1))?location.hash.slice(1):"overview";
 nav.querySelectorAll("a").forEach(a=>{if(a.dataset.nav===id)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
}
window.addEventListener("hashchange",active);active();

const main=document.querySelector("main");
if(main){
 const content=document.createElement("div");content.className="app-content";
 content.tabIndex=0;content.setAttribute("role","region");content.setAttribute("aria-label","Содержимое БонусПро");
 main.before(content);content.append(main);
 window.addEventListener("hashchange",()=>{content.scrollTop=0;});
}

const toggle=document.createElement("button");toggle.type="button";toggle.className="sidebar-toggle";
toggle.setAttribute("aria-label","Свернуть меню");toggle.setAttribute("aria-expanded","true");
toggle.textContent="‹";aside.prepend(toggle);
toggle.addEventListener("click",()=>{
 const collapsed=document.body.classList.toggle("compact-menu");
 toggle.textContent=collapsed?"›":"‹";toggle.setAttribute("aria-expanded",String(!collapsed));
 toggle.setAttribute("aria-label",collapsed?"Развернуть меню":"Свернуть меню");
});
nav.querySelectorAll("a").forEach(a=>{a.title=a.textContent;a.setAttribute("aria-label",a.textContent);});

if(location.pathname==="/settings"){
 const form=document.querySelector("#rules-form");
 if(form){
  const heading=document.createElement("h2");heading.textContent="Правила начисления и списания";form.prepend(heading);
  const blocks=[form,...document.querySelectorAll("#workspace>section:not(.readiness-card)")];
  blocks.forEach((block,index)=>{
   const heading=block.querySelector("h2");if(!heading)return;
   const step=document.createElement("span");step.className="setup-step";step.textContent=String(index+1);
   step.setAttribute("aria-hidden","true");heading.prepend(step);
  });
 }
}
