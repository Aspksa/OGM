// Presentation only: SVG and native controls; no source or accounting data is changed.
const ns="http://www.w3.org/2000/svg";
const fmt=(x:number|null)=>x===null?"—":new Intl.NumberFormat("ru-RU",{maximumFractionDigits:2}).format(x);
function html<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,cls?:string){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function svg<K extends keyof SVGElementTagNameMap>(tag:K,attrs:Record<string,string|number>){const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));return e;}
export function balanceGraphic(){
 const root=html("figure",undefined,"wo-orbit"),chart=svg("svg",{viewBox:"0 0 340 180",role:"img","aria-label":"Топливный баланс"});root.append(chart);
 const arc=svg("circle",{cx:170,cy:90,r:58,fill:"none",stroke:"currentColor","stroke-width":8,class:"wo-orbit-track"});
 const value=svg("circle",{cx:170,cy:90,r:58,fill:"none","stroke-width":8,"stroke-linecap":"round",pathLength:100,"stroke-dasharray":"0 100",transform:"rotate(-90 170 90)",class:"wo-orbit-value"});
 chart.append(arc,value,svg("circle",{cx:170,cy:90,r:45,fill:"none",class:"wo-orbit-inner","stroke-dasharray":"2 5"}));
 const central=svg("text",{x:170,y:85,"text-anchor":"middle",class:"wo-orbit-caption"});central.textContent="Факт, л";
 const number=svg("text",{x:170,y:109,"text-anchor":"middle",class:"wo-orbit-number"});chart.append(central,number);
 const nodes=[["Начало",12,20,"start","M 112 65 H 78 V 37"],["По карте",328,20,"end","M 228 65 H 262 V 37"],["Конец",12,147,"start","M 112 115 H 78 V 143"],["Норма",328,147,"end","M 228 115 H 262 V 143"]] as const;
 const labels=nodes.map(([name,x,y,anchor,d])=>{chart.append(svg("path",{d,fill:"none",class:"wo-orbit-link"}));const t=svg("text",{x,y,"text-anchor":anchor,class:"wo-orbit-caption"});t.textContent=name;const v=svg("text",{x,y:y+18,"text-anchor":anchor,class:"wo-orbit-label"});chart.append(t,v);return v;});
 const note=html("figcaption",undefined,"wo-orbit-note");root.append(note);
 function update(start:number|null,card:number|null,end:number|null,actual:number|null,norm:number|null,manual:number){
  [start,card,end,norm].forEach((v,i)=>labels[i].textContent=fmt(v));number.textContent=fmt(actual);
  const known=actual!==null&&norm!==null&&actual>=0&&norm>0;value.setAttribute("stroke-dasharray",known?Math.min(100,actual!/norm!*100)+" 100":"0 100");
  root.classList.toggle("wo-orbit-over",known&&actual!>norm!);root.classList.toggle("wo-orbit-known",known);
  note.textContent="Кольцо — факт относительно нормы"+(known?": "+fmt(actual!/norm!*100)+"%":" — недостаточно данных")+". Прочие операции нетто: "+fmt(manual)+" л.";
  chart.setAttribute("aria-label","Начало "+fmt(start)+", карта "+fmt(card)+", прочие "+fmt(manual)+", конец "+fmt(end)+", факт "+fmt(actual)+", норма "+fmt(norm)+" литров");
 }
 return {root,update};
}
type Operation={moment:string;quantity:number;amount:number;category:string;operation:string};
export function fuelCalendar(month:string,ops:Operation[],kind:string,onDay:(day:string)=>void){
 const root=html("section",undefined,"wo-fuel-calendar"),head=html("h3","Заправки по дням"),calendar=html("div",undefined,"wo-days"),chart=html("div",undefined,"wo-daily-chart"),price=html("div",undefined,"wo-price-chart");
 root.append(head,html("p","Связанные операции выбранного вида топлива. День открывает соответствующие записи.","muted"),chart,calendar,price);
 const year=Number(month.slice(0,4)),m=Number(month.slice(5)),days=new Date(year,m,0).getDate();const sums=Array.from({length:days},()=>({in:0,out:0,qty:0,amount:0,count:0}));
 for(const op of ops){if(op.category!==kind||!op.moment.startsWith(month+"-"))continue;const day=Number(op.moment.slice(8,10));if(day<1||day>days)continue;const s=sums[day-1];s.count++;if(op.quantity>0&&op.operation==="отгрузка"){s.in+=op.quantity/100;s.qty+=op.quantity;s.amount+=op.amount;}else if(op.quantity<0)s.out+=-op.quantity/100;}
 const max=Math.max(1,...sums.map(s=>Math.max(s.in,s.out)));
 const choose=(i:number)=>{const key=month+"-"+String(i+1).padStart(2,"0");for(const b of root.querySelectorAll<HTMLElement>("[data-day]"))b.setAttribute("aria-pressed",String(b.dataset.day===key));onDay(key);};
 const makeDay=(i:number,cls:string)=>{const d=html("button",undefined,cls);d.type="button";d.dataset.day=month+"-"+String(i+1).padStart(2,"0");d.setAttribute("aria-pressed","false");d.title=(i+1)+" · заправки "+fmt(sums[i].in)+" л · возвраты "+fmt(sums[i].out)+" л";d.setAttribute("aria-label",d.title);d.onclick=()=>choose(i);return d;};
 sums.forEach((s,i)=>{const b=makeDay(i,"wo-daily-bar");const a=html("span",undefined,"wo-day-in"),o=html("span",undefined,"wo-day-out");a.style.height=s.in/max*100+"%";o.style.height=s.out/max*100+"%";b.append(a,o);chart.append(b);const c=makeDay(i,"wo-day");c.append(html("span",String(i+1)),html("small",s.count?fmt(s.in-s.out):"—"));c.classList.toggle("wo-day-active",s.count>0);calendar.append(c);});
 const series=sums.map(s=>s.qty?s.amount/s.qty:null),known=series.filter((x):x is number=>x!==null);price.append(html("h3","Цена покупки по дням, ₽/л"));
 if(!known.length){price.append(html("p","Нет положительных покупок для расчёта цены.","muted"));return root;}
 const low=Math.min(...known),high=Math.max(...known),span=Math.max(high-low,0.01),g=svg("svg",{viewBox:"0 0 320 78",role:"img","aria-label":"Средневзвешенная цена покупок по дням; пропуски не соединяются"});const points=series.map((v,i)=>v===null?null:[8+i*304/Math.max(1,days-1),65-(v-low)/span*52]);for(let i=0;i<points.length;i++){const p=points[i];if(!p)continue;const prev=points[i-1];if(prev)g.append(svg("line",{x1:prev[0],y1:prev[1],x2:p[0],y2:p[1],class:"wo-price-line"}));const dot=svg("circle",{cx:p[0],cy:p[1],r:3,class:"wo-price-point"}),title=svg("title",{});title.textContent=(i+1)+": "+fmt(series[i])+" ₽/л";dot.append(title);g.append(dot);}price.append(g,html("p","Мин. "+fmt(low)+" · макс. "+fmt(high)+" ₽/л · только покупки, без возвратов","muted"));return root;
}
export type AnnualPoint={month:string;actual:number|null;norm:number|null;km:number|null};
export function annualGraphic(points:AnnualPoint[]){
 const root=html("div",undefined,"wo-annual-chart"),known=points.flatMap(p=>[p.actual,p.norm]).filter((v):v is number=>v!==null&&v>=0),max=Math.max(1,...known);
 for(const p of points){const item=html("div",undefined,"wo-annual-item"),bars=html("div",undefined,"wo-annual-bars");for(const [v,c] of [[p.actual,"actual"],[p.norm,"norm"]] as const){const b=html("span",undefined,"wo-annual-"+c);if(v!==null&&v>=0)b.style.height=v/max*100+"%";else b.classList.add("wo-annual-unknown");bars.append(b);}item.title=p.month+": факт "+fmt(p.actual)+", норма "+fmt(p.norm)+" л";item.append(bars,html("small",p.month.slice(5)));root.append(item);}
 return root;
}
