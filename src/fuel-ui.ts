type Row={id:number;[key:string]:string|number|null};
type Registry={employees:Row[];vehicles:Row[];cards:Row[]};
type Op={card_number:string;moment:string;operation:string;employee:string;vehicle:string;station:string;product:string;category:string;price:number;quantity:number;amount:number;flags:string[]};
type Report={rows:Op[];months:string[];imports:Row[];history:Row[];periods:Row[]};
const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,cls?:string)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const fmt=(n:number)=>new Intl.NumberFormat("ru-RU",{minimumFractionDigits:2,maximumFractionDigits:2}).format(n/100);
const fio=(r:Row)=>[r.last_name,r.first_name,r.middle_name].filter(Boolean).join(" ");
const car=(r:Row)=>[r.make,r.model,r.plate].filter(Boolean).join(" ");
function button(text:string,fn:()=>void){const b=el("button",text);b.type="button";b.onclick=fn;return b;}
function select(opts:[string,string][],label:string){const s=el("select");s.setAttribute("aria-label",label);for(const [v,n] of opts){const o=el("option",n);o.value=v;s.append(o);}return s;}
function table(headers:string[],rows:string[][]){const w=el("div",undefined,"table-wrap"),t=el("table"),head=el("thead"),tr=el("tr");for(const h of headers)tr.append(el("th",h));head.append(tr);t.append(head);const body=el("tbody");for(const row of rows){const r=el("tr");for(const c of row)r.append(el("td",c));body.append(r);}t.append(body);w.append(t);if(!rows.length)w.append(el("p","Нет записей","empty"));return w;}
async function json(url:string,options?:RequestInit){const r=await fetch(url,options),v=await r.json();if(!r.ok)throw new Error(v.error||"Ошибка запроса");return v;}
export async function renderFuel(root:HTMLElement,refresh:()=>Promise<Registry>){
 const host=el("div",undefined,"fuel-panel");root.append(host);const toolbar=el("div",undefined,"toolbar"),status=el("p",undefined,"muted"),body=el("div");status.setAttribute("role","status");
 const file=el("input");file.type="file";file.accept=".xls,.xlsx";file.setAttribute("aria-label","Ведомость Excel");
 const period=select([["","Все периоды"]],"Период"),mode=select([["ops","Операции"],["employee","По сотрудникам"],["vehicle","По автомобилям"],["category","По топливу"],["anomalies","Расхождения"],["history","История закреплений"],["imports","Исходные файлы"]],"Отчёт");
 const q=el("input");q.type="search";q.placeholder="Карта, ФИО, госномер, АЗС";q.setAttribute("aria-label","Поиск");
 const upload=button("Загрузить Excel",()=>void importFile());upload.className="primary";
 toolbar.append(file,upload,period,mode,q,button("Прогноз и цены",()=>void forecast()),button("Проверка заправок",()=>void alerts()),button("Экспорт Excel",()=>{location.href="/api/fuel/export?month="+encodeURIComponent(period.value);}));
 host.append(toolbar,status,body);
 let report:Report={rows:[],months:[],imports:[],history:[],periods:[]},registry:Registry={employees:[],vehicles:[],cards:[]},generation=0;
 async function load(){const token=++generation;try{const selected=period.value;const [r,d]=await Promise.all([json("/api/fuel?month="+encodeURIComponent(selected)),refresh()]);if(token!==generation||!host.isConnected)return;report=r;registry=d;period.replaceChildren();for(const m of ["",...report.months]){const o=el("option",m||"Все периоды");o.value=m;period.append(o);}period.value=selected;draw();}catch(e){status.textContent=String(e);}}
 function modal(title:string){const d=el("dialog",undefined,"history-dialog"),content=el("div",undefined,"ops-panel");d.append(content);content.append(el("h2",title),button("Закрыть",()=>d.close()));d.onclose=()=>d.remove();document.body.append(d);d.showModal();return {d,content};}
 const display=(v:unknown)=>v===null||v===undefined?"—":typeof v==="number"?new Intl.NumberFormat("ru-RU",{maximumFractionDigits:2}).format(v):String(v);
 async function importFile(){const f=file.files?.[0];if(!f){status.textContent="Выберите файл.";return;}upload.disabled=true;try{
 const opts:RequestInit={method:"POST",headers:{"Content-Type":"application/octet-stream"},body:f};
 const name=encodeURIComponent(f.name),r=await json("/api/automation/preview?name="+name,opts);
 if(!host.isConnected)return;
 if(r.kind!=="fuel"){status.textContent=r.message;return;}
 const {d,content}=modal("Предварительная проверка · "+f.name);
 content.append(el("p","Операций: "+r.operations+" · повторов: "+r.duplicates+" · новых карт: "+r.new_cards+" · без привязки: "+r.unassigned+" · неизвестное топливо: "+r.unknown+" · несовпадение топлива: "+r.mismatch,"muted"),
 el("h2","Распределение по месяцам","section-title"),table(["Месяц","Операции","Литры нетто","Сумма, ₽"],r.months.map((x:any)=>[x.month,display(x.operations),display(x.liters),display(x.amount)])),
 el("h2","Топливные карты и закрепления на даты операций","section-title"),table(["Карта","Новая","Операции","Без привязки","Несовпадения","Исторические связи"],r.cards.map((x:any)=>[x.number,x.new?"Да":"Нет",display(x.operations),display(x.unassigned),display(x.mismatch),x.links.map((l:any)=>l.employee+" / "+l.plate).join("; ")||"—"])),
 el("h2","Распознанное топливо","section-title"),table(["Наименование","Категория","Литры","Сумма"],r.products.map((x:any)=>[x.product,x.category,display(x.liters),display(x.amount)])),
 el("p","Новые карты будут созданы без закрепления. Операции распределяются по дате; неизвестные связи не угадываются. Перед записью сервер заново проверит файл.","muted"));
 const note=el("p",undefined,"muted"),approve=button("Подтвердить импорт",async()=>{approve.disabled=true;try{const v=await json("/api/fuel/import?name="+name,opts);d.close();status.textContent=v.duplicate?"Этот файл уже загружен.":"Добавлено: "+v.inserted+" · повторов: "+v.skipped+" · новых карт: "+v.new_cards;file.value="";await load();if(r.months.length===1){period.value=r.months[0].month;await load();}}catch(e){note.textContent=String(e);approve.disabled=false;}});approve.className="primary";content.append(approve,note);
 }catch(e){status.textContent=String(e);}finally{upload.disabled=false;}}
 async function forecast(){const {d,content}=modal("Прогноз потребности и изменение цен");const target=el("input");target.type="month";const t=new Date();target.value=period.value||t.getFullYear()+"-"+String(t.getMonth()+1).padStart(2,"0");target.setAttribute("aria-label","Целевой месяц");const result=el("div");content.append(target,button("Рассчитать",()=>void run()),result);let generation=0;
 async function run(){const token=++generation;result.replaceChildren(el("p","Расчёт…","muted"));try{const m=encodeURIComponent(target.value),[a,b]=await Promise.all([json("/api/automation/forecast?month="+m),json("/api/automation/prices?month="+m)]);if(!d.isConnected||token!==generation)return;result.replaceChildren(el("p",a.method,"muted"),el("p","История: "+a.basis.join(", ")+" · только завершённые месяцы","muted"),
 table(["Топливо","Авто с прогнозом / всего","Расход, л"],a.totals.map((x:any)=>[x.fuel_type,x.covered+" / "+x.vehicles,display(x.liters)])),
 table(["Госномер","Топливо","Прогноз, л","Мин. – макс.","Наблюдения"],a.rows.map((x:any)=>[x.plate,x.fuel_type||"—",display(x.forecast),display(x.low)+" – "+display(x.high),x.samples.map((y:any)=>y.month+": "+display(y.liters)).join("; ")||"Недостаточно данных"])),
 el("h2","Цены: "+b.previous+" → "+b.month,"section-title"),table(["Топливо","Прошлый, ₽/л","Выбранный, ₽/л","Изменение, ₽","Изменение, %"],b.rows.map((x:any)=>[x.product,display(x.previous),display(x.current),display(x.difference),display(x.percent)])));}catch(e){result.textContent=String(e);}}
 await run();}
 async function alerts(){const {d,content}=modal("Подозрительные операции");try{const r=await json("/api/ops/fuel?month="+encodeURIComponent(period.value));if(!d.isConnected)return;content.append(el("p","Сигналы для проверки: частые покупки, большой объём, несоответствие топлива, выходные. Это не доказательство нарушения.","muted"),table(["Дата","Карта","Авто","Топливо","Литры","Замечания"],r.alerts.map((x:any)=>[x.moment,x.card_number,x.vehicle||"—",x.product,display(x.quantity/100),x.alerts.join("; ")])));}catch(e){content.append(el("p",String(e),"danger"));}}
 function draw(){body.replaceChildren();const search=q.value.trim().toLocaleLowerCase("ru");const rows=report.rows.filter(r=>[r.card_number,r.employee,r.vehicle,r.station,r.product].join(" ").toLocaleLowerCase("ru").includes(search));
 const metrics=el("div",undefined,"metrics");for(const [n,v] of [["Операции",String(rows.length)],["Литры (нетто)",fmt(rows.reduce((s,r)=>s+r.quantity,0))],["Сумма, ₽",fmt(rows.reduce((s,r)=>s+r.amount,0))],["Замечания",String(rows.filter(r=>r.flags.length).length)]]){const m=el("div",undefined,"metric");m.append(el("span",n,"muted"),el("strong",v));metrics.append(m);}body.append(metrics);
 if(mode.value==="imports"){for(const r of report.imports){const p=el("p"),a=el("a",String(r.filename));a.href="/api/fuel/source?id="+r.id;p.append(a,document.createTextNode(" · "+r.imported_at+" · операций "+r.inserted+" · повторов "+r.skipped));body.append(p);}return;}
 if(mode.value==="history"){history();return;}
 if(["employee","vehicle","category"].includes(mode.value)){const key=mode.value as "employee"|"vehicle"|"category",groups=new Map<string,number[]>();for(const r of rows){const k=r[key]||"Не определено",g=groups.get(k)||[0,0,0];g[0]++;g[1]+=r.quantity;g[2]+=r.amount;groups.set(k,g);}body.append(table(["Группа","Операции","Литры","Рубли"],Array.from(groups,([k,v])=>[k,String(v[0]),fmt(v[1]),fmt(v[2])])));return;}
 const visible=mode.value==="anomalies"?rows.filter(r=>r.flags.length):rows;
 body.append(table(["Дата","Карта","Сотрудник","Автомобиль","Топливо","Операция","АЗС","Цена","Литры","Сумма, ₽","Замечания"],visible.map(r=>[r.moment.replace("T"," "),r.card_number,r.employee||"Не определён",r.vehicle||"Не определён",r.product,r.operation,r.station,fmt(r.price),fmt(r.quantity),fmt(r.amount),r.flags.join("; ")])));}
 function history(){const f=el("form",undefined,"fuel-history");f.append(el("h2","Закрепление за прошлый период"));
 const card=select([["","Выберите карту"],...registry.cards.map(r=>[String(r.id),String(r.number)] as [string,string])],"Карта");
 const employee=select([["","Выберите сотрудника"],...registry.employees.map(r=>[String(r.id),fio(r)] as [string,string])],"Сотрудник");
 const vehicle=select([["","Без автомобиля"],...registry.vehicles.map(r=>[String(r.id),car(r)] as [string,string])],"Автомобиль");
 const start=el("input"),end=el("input");start.type=end.type="date";start.required=end.required=card.required=employee.required=true;
 for(const [name,node] of [["Карта",card],["Сотрудник",employee],["Автомобиль",vehicle],["С",start],["По включительно",end]] as [string,HTMLElement][]){const l=el("label",name);l.append(node);f.append(l);}
 const save=el("button","Сохранить период","primary");save.type="submit";f.append(save);
 f.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await json("/api/fuel/period",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({card_id:Number(card.value),employee_id:Number(employee.value),vehicle_id:vehicle.value?Number(vehicle.value):null,start:start.value,end:end.value})});status.textContent="Период сохранён.";await load();}catch(err){status.textContent=String(err);}finally{save.disabled=false;}};
 const label=(r:Row)=>{const e=registry.employees.find(x=>x.id===r.employee_id),v=registry.vehicles.find(x=>x.id===r.vehicle_id);return [String(r.number),e?fio(e):"—",v?car(v):"—",String(r.fuel_type||"—")];};
 body.append(f,el("h2","Ручные периоды","section-title"),table(["Карта","Сотрудник","Автомобиль","Топливо","С","До (не включительно)"],report.periods.map(r=>[...label(r),String(r.start),String(r.end)])),el("h2","Автоматическая история","section-title"),table(["Карта","Сотрудник","Автомобиль","Топливо","С даты"],report.history.map(r=>[...label(r),String(r.at)])));}
 period.onchange=()=>void load();mode.onchange=draw;q.oninput=draw;await load();
}
