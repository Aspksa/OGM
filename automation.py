"""Deterministic local automation. No network calls or automatic corrections."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import zipfile
import fuel
import operations
import writeoff

def file_kind(name,blob):
    if not 0<len(blob)<=fuel.MAX_FILE: raise ValueError("Файл должен быть до 12 МБ")
    if blob.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        ext=".xls"
    elif blob.startswith(b"PK\x03\x04"):
        ext=".xlsx"
        with zipfile.ZipFile(BytesIO(blob)) as z:
            if "[Content_Types].xml" not in z.namelist() or "xl/workbook.xml" not in z.namelist():
                raise ValueError("Не является книгой Excel")
            if sum(x.file_size for x in z.infolist())>100*1024*1024:raise ValueError("Слишком большой архив")
    else:raise ValueError("Не распознан формат XLS/XLSX")
    if not name.lower().endswith(ext):raise ValueError("Расширение файла не соответствует его содержимому")
    return ext

def preview(c,name,blob):
    ext=file_kind(name,blob)
    controls=[];rows=fuel.parse_file(name,blob,controls)
    if not rows:
        if ext==".xlsx":
            from openpyxl import load_workbook
            wb=load_workbook(BytesIO(blob),read_only=True,data_only=True,keep_links=False)
            try:
                s=wb.worksheets[0]
                if str(s["A7"].value).strip().casefold().replace(" ","")=="госномер" and str(s["E7"].value).strip()=="Пробег (км)":
                    return {"kind":"writeoff","format":ext,"message":"Месячная ведомость списания: откройте «Списание ГСМ» → импорт старого отчёта."}
            finally:wb.close()
        raise ValueError("Не обнаружены операции известного формата ведомости ГСМ")
    cards={r["number"]:r["id"] for r in c.execute("SELECT id,number FROM cards")}
    employees={r["id"]:" ".join(filter(None,(r["last_name"],r["first_name"],r["middle_name"]))) for r in c.execute("SELECT * FROM employees")}
    vehicles={r["id"]:r["plate"] for r in c.execute("SELECT id,plate FROM vehicles")}
    months=defaultdict(lambda:{"operations":0,"liters":0,"amount":0})
    products=defaultdict(lambda:{"operations":0,"liters":0,"amount":0})
    groups=defaultdict(lambda:{"operations":0,"matched":0,"unassigned":0,"mismatch":0,"links":set()})
    counts=Counter();duplicates=0;unassigned=0;unknown=0;mismatch=0;amount_errors=0;similar=defaultdict(set);ambiguous=0
    for row in rows:
        card,when,op,station,product,category,price,qty,amount=row
        for g in (months[when[:7]],products[(product,category)]):
            g["operations"]+=1;g["liters"]+=qty;g["amount"]+=amount
        g=groups[card];g["operations"]+=1
        e,v,f=fuel.link_at(c,cards[card],when) if card in cards else (None,None,"")
        if e and v:
            g["matched"]+=1;g["links"].add((employees.get(e,""),vehicles.get(v,"")))
        else:g["unassigned"]+=1;unassigned+=1
        if category=="Другое":unknown+=1
        if v and f and category!="Другое" and category!=f:g["mismatch"]+=1;mismatch+=1
        if abs(price*qty-amount*100)>100:amount_errors+=1
        sig=fuel.signature(row);counts[sig]+=1
        similar[(card,when,op)].add(sig)
        similar[(card,when,op)].update(x[0] for x in c.execute("SELECT signature FROM fuel_operations WHERE card_number=? AND moment=? AND operation=?",(card,when,op)))
        card_id=cards.get(card)
        if card_id and c.execute("SELECT COUNT(*) FROM fuel_periods WHERE card_id=? AND start<=? AND end>?",(card_id,when,when)).fetchone()[0]>1:ambiguous+=1
        if c.execute("SELECT 1 FROM fuel_operations WHERE signature=? AND occurrence=?",(sig,counts[sig])).fetchone():duplicates+=1
    def metric(g):return {**g,"liters":g["liters"]/100,"amount":g["amount"]/100}
    return {"kind":"fuel","format":ext,"digest":sha256(blob).hexdigest(),"operations":len(rows),
        "controls":controls,"control_complete":bool(controls) and all(x["checked"] for x in controls if x["operations"]) and any(x["checked"] for x in controls),"amount_errors":amount_errors,"conflicting_events":sum(len(v)>1 for v in similar.values()),"ambiguous_links":ambiguous,"duplicates":duplicates,"new_cards":sum(k not in cards for k in groups),"unassigned":unassigned,"unknown":unknown,"mismatch":mismatch,
        "months":[{"month":k,**metric(v)} for k,v in sorted(months.items())],
        "products":[{"product":k[0],"category":k[1],**metric(v)} for k,v in sorted(products.items())],
        "cards":[{"number":k,"new":k not in cards,**{**v,"links":[{"employee":e,"plate":p} for e,p in sorted(v["links"])]}} for k,v in sorted(groups.items())]}

def mileage_proposal(c,month,vid,p):
    sheets=operations.waybills(c,month,vid)
    if not sheets:return {"km":None,"message":"Нет путевых листов. Нужен подтверждённый маршрут, GPS или иной источник.","evidence":[]}
    evidence=[{"id":s["id"],"number":s["number"],"day":s["day"],"first":s["odo_first"],"last":s["odo_last"],"km":None if s["odo_first"] is None or s["odo_last"] is None or s["odo_last"]<s["odo_first"] else writeoff.rounded(s["odo_last"]-s["odo_first"])} for s in sheets]
    if any(s["km"] is None for s in evidence) or len({s["day"] for s in evidence})!=len(evidence):
        return {"km":None,"message":"Есть неполные, обратные или неоднозначно упорядоченные показания. Пробег нельзя достоверно предложить.","evidence":evidence}
    if any(a["last"]>b["first"] for a,b in zip(evidence,evidence[1:])):
        return {"km":None,"message":"Пересекающиеся показания одометра: риск двойного пробега. Нужна ручная проверка.","evidence":evidence}
    candidate=writeoff.rounded(sum(s["km"] for s in evidence))
    gap=any(a["last"]!=b["first"] for a,b in zip(evidence,evidence[1:]))
    return {"km":candidate,"message":("Есть разрывы показаний. Сумма пробегов только по представленным листам; проверьте полноту." if gap else "Сумма пробегов по представленным листам; подтвердите полноту и достоверность."),
        "evidence":evidence,"reason":"Сумма пробегов по путевым листам: "+", ".join(s["number"]+" ("+s["day"]+")" for s in evidence)[:380]}

def readiness(c,month,report=None):
    writeoff.month_bounds(month);r=report if report is not None else writeoff.report(c,month);rows=[]
    for x in r["rows"]:
        z=x["result"];p=x["payload"]
        blocked=[];warnings=[]
        if not x["revision"]:blocked.append("Месячная карточка не сохранена")
        if not x["fuel_type"]:blocked.append("Не указан вид топлива")
        for key,label in (("start","Начальный остаток"),("end","Конечный остаток"),("km","Пробег"),("actual","Факт"),("norm","Норма")):
            if z[key] is None:blocked.append(label+": нет данных")
        if z["actual"] is not None and z["actual"]<0:blocked.append("Отрицательный расход")
        if z["unconfirmed"]:blocked.append("Неподтверждённые операции")
        monthly=p.get("input_mode")=="monthly"
        if not monthly and (not p.get("first_ref") or not p.get("last_ref")):blocked.append("Не указаны путевые листы")
        if not r.get("closed"):
            sheets=operations.waybills(c,month,x["id"])
            refs={s["number"] for s in sheets}
            if (not monthly or p.get("first_ref") or p.get("last_ref")) and (p.get("first_ref") not in refs or p.get("last_ref") not in refs):blocked.append("Первый или последний лист отсутствует в реестре")
            if sheets and (not monthly or p.get("first_ref") or p.get("last_ref")) and (p.get("first_ref")!=sheets[0]["number"] or p.get("last_ref")!=sheets[-1]["number"]):blocked.append("Первый или последний лист не совпадает с реестром")
            if len({s["day"] for s in sheets})!=len(sheets):blocked.append("Не подтверждён порядок нескольких путевых листов за день")
            if any(a["odo_last"] is not None and b["odo_first"] is not None and a["odo_last"]>b["odo_first"] for a,b in zip(sheets,sheets[1:])):blocked.append("Пересекающиеся показания путевых листов")
        for flag in z["flags"]:
            if monthly and flag=="Не указана вместимость бака":warnings.append(flag);continue
            if flag in ("Нет подтверждённого остатка прошлого месяца","Использован скорректированный пробег вместо показаний одометра","Нет полного комплекта показаний одометра","Конечный одометр меньше начального"):
                if (flag.startswith("Нет подтверждённого") and p.get("start") is not None) or (not flag.startswith("Нет подтверждённого") and p.get("km_override") is not None and p.get("km_reason")):warnings.append(flag);continue
            blocked.append(flag)
        # Distinguish flags from genuine confirmed mileage; never infer from fuel consumption.
        if z["difference"] is not None and z["difference"]>0:warnings.append("Перерасход: требуется пояснение")
        rows.append({"id":x["id"],"plate":x["plate"],"model":x["model"],"fuel_type":x["fuel_type"],"driver":x["driver"],
            "result":z,"blocked":list(dict.fromkeys(blocked)),"warnings":list(dict.fromkeys(warnings))})
    return {"month":month,"closed":r.get("closed",False),"unassigned":r["unassigned"],"rows":rows,
        "ready":bool(rows) and not r["unassigned"] and all(not x["blocked"] for x in rows),
        "review":sum(bool(x["warnings"]) for x in rows),"blocked":sum(bool(x["blocked"]) for x in rows)}

def month_workspace(c,month):
    """Separate statement presence from readiness, with selected month in one read snapshot."""
    start,_=writeoff.month_bounds(month)
    year=start.year
    report=writeoff.report(c,month)
    check=readiness(c,month,report)
    counts={r["month"]:r["n"] for r in c.execute("SELECT substr(moment,1,7) AS month,COUNT(*) AS n FROM fuel_operations WHERE substr(moment,1,4)=? GROUP BY 1",(str(year).zfill(4),))}
    closed={r[0] for r in c.execute("SELECT month FROM writeoff_closures WHERE substr(month,1,4)=?",(str(year).zfill(4),))}
    saved={r[0] for r in c.execute("SELECT DISTINCT month FROM writeoff_months WHERE substr(month,1,4)=?",(str(year).zfill(4),))}
    months=[]
    for i in range(1,13):
        key=f"{year:04d}-{i:02d}"
        n=counts.get(key,0)
        if key==month:state=check
        elif n or key in saved or key in closed:state=readiness(c,key)
        else:state=None
        months.append({"month":key,"statement_loaded":n>0,"operations":n,
            "unassigned":state["unassigned"] if state else 0,
            "blocked":state["blocked"] if state else None,"review":state["review"] if state else None,
            "closed":key in closed,"ready":bool(state and state["ready"]),
            "draft":key in saved})
    return {"report":report,"readiness":check,"months":months}

def history_months(month,count):
    start,_=writeoff.month_bounds(month)
    for i in range(count):
        start=(start-timedelta(days=1)).replace(day=1)
        yield start.strftime("%Y-%m")

def forecast(c,month):
    # The selected month is the forecast target. Do not use its actuals or future months.
    start,_=writeoff.month_bounds(month)
    if start.year<2:raise ValueError("Недопустимый период")
    completed=date.today().replace(day=1)
    months=[m for m in history_months(month,6) if m<completed.isoformat()[:7]][:3]
    # Forecast consumption, not purchase volume. Only complete, nonnegative, documented month balances.
    vehicles={v["id"]:dict(v) for v in c.execute("SELECT id,plate,make,model,fuel_type FROM vehicles WHERE archived=0 AND status='Работает'")}
    obs=defaultdict(list)
    for m in months:
        r=readiness(c,m)
        if r["unassigned"]:continue
        for x in r["rows"]:
            v=vehicles.get(x["id"])
            if v and v["fuel_type"]==x["fuel_type"] and not x["blocked"] and x["result"]["actual"] is not None:
                obs[x["id"]].append({"month":m,"liters":x["result"]["actual"]})
    rows=[]
    for vid,v in vehicles.items():
        samples=obs[vid];values=[s["liters"] for s in samples]
        rows.append({"id":vid,"plate":v["plate"],"fuel_type":v["fuel_type"],"samples":samples,
            "forecast":writeoff.rounded(sum(values)/len(values)) if len(values)>=2 else None,
            "low":min(values) if len(values)>=2 else None,"high":max(values) if len(values)>=2 else None})
    totals=[]
    for kind in ("Бензин","Дизель",""):
        selected=[x for x in rows if x["fuel_type"]==kind]
        known=[x["forecast"] for x in selected if x["forecast"] is not None]
        if selected:totals.append({"fuel_type":kind or "Не указан","vehicles":len(selected),"covered":len(known),"liters":writeoff.rounded(sum(known)) if known else None})
    return {"month":month,"basis":months,"rows":rows,"totals":totals,"method":"Среднее фактического расхода за 2–3 пригодных предыдущих завершённых месяца; диапазон — минимум/максимум наблюдений, не доверительный интервал. Без сезонной поправки. Не заказ топлива."}

def price_trend(c,month):
    start,_=writeoff.month_bounds(month)
    if start.year<2:raise ValueError("Недопустимый период")
    prev=next(history_months(month,1))
    groups=defaultdict(lambda:[0,0])
    for r in c.execute("SELECT moment,product,quantity,amount FROM fuel_operations WHERE substr(moment,1,7) IN (?,?) AND quantity>0 AND operation='отгрузка'",(month,prev)):
        g=groups[(r["moment"][:7],r["product"])];g[0]+=r["quantity"];g[1]+=r["amount"]
    rows=[]
    for product in sorted({k[1] for k in groups}):
        def price(m):
            qty,amount=groups.get((m,product),(0,0))
            return writeoff.rounded(Decimal(amount)/Decimal(qty)) if qty else None
        old,new=price(prev),price(month)
        rows.append({"product":product,"category":fuel.category(product),"previous":old,"current":new,
            "difference":writeoff.rounded(new-old) if old is not None and new is not None else None,
            "percent":writeoff.rounded((new-old)/old*100) if old and new is not None else None})
    return {"month":month,"previous":prev,"rows":rows}

def export_review(c,month):
    from openpyxl import load_workbook
    r=readiness(c,month)
    wb=load_workbook(BytesIO(writeoff.export(c,month)))
    ws=wb.create_sheet("Проверка готовности",0)
    ws.append(["Статус","Закрытый период" if r.get("closed") else "Готово к проверке" if r["ready"] else "НЕ ГОТОВО — черновик"])
    ws.append(["Период",month]);ws.append(["Операций без автомобиля",r["unassigned"]])
    ws.append(["Госномер","Автомобиль","Блокирующие замечания","Требуют проверки"])
    for x in r["rows"]:
        ws.append([x["plate"],x["model"],"; ".join(x["blocked"]),"; ".join(x["warnings"])])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value,str):cell.data_type="s"
    for col,width in (("A",20),("B",30),("C",85),("D",75)):ws.column_dimensions[col].width=width
    from openpyxl.styles import Alignment,Font
    for row in ws.iter_rows():
        for cell in row:cell.alignment=Alignment(vertical="top",wrap_text=True)
    for cell in ws[4]:cell.font=Font(bold=True)
    ws.freeze_panes="C5"
    wb["Списание ГСМ"]["A6"]="За "+month+" — "+("закрытый период" if r.get("closed") else "проект к проверке" if r["ready"] else "НЕ ГОТОВО, черновик")
    wb.active=0;out=BytesIO();wb.save(out);wb.close();return out.getvalue()

def vehicle_analysis(c,month):
    """Read-only comparisons; only closed historical months supply reliable baselines."""
    start,end=writeoff.month_bounds(month)
    if start.year<2:raise ValueError("Недопустимый период")
    report=writeoff.report(c,month)
    previous=next(history_months(month,1))
    periods=list(dict.fromkeys([previous,*[f"{start.year:04d}-{i:02d}" for i in range(1,start.month)]]))
    historical={}
    for m in periods:
        snap=writeoff.closure(c,m)
        if snap:historical[m]={r["id"]:r for r in snap[0]["rows"]}
    def valid(x):
        z=x["result"]
        return z["actual"] is not None and z["actual"]>=0 and z["km"] is not None and z["km"]>0
    def rate(x):
        return writeoff.rounded(x["result"]["actual"]/x["result"]["km"]*100) if valid(x) else None
    out=[]
    for x in report["rows"]:
        z=x["result"];p=x["payload"];vid=x["id"];kind=x["fuel_type"]
        hist=[(m,rs[vid]) for m,rs in historical.items() if vid in rs and rs[vid]["fuel_type"]==kind and valid(rs[vid])]
        last=next((r for m,r in hist if m==previous),None)
        annual=[r for m,r in hist if m[:4]==month[:4]]
        if report.get("closed") and valid(x):annual.append(x)
        km=sum(r["result"]["km"] for r in annual);used=sum(r["result"]["actual"] for r in annual)
        old_rate=rate(last) if last else None;cur_rate=rate(x)
        history=x.get("fuel_history")
        matched=[r for r in history if kind and r["category"]==kind] if history is not None else None
        # A zero purchase total is known only with reviewed absence of fuel.
        reviewed=p.get("fuel_reviewed") and "Операции изменились после сверки" not in z["flags"]
        amount=sum(r["amount"] for r in matched)/100 if matched is not None and kind and (matched or reviewed) else None
        km_cost=writeoff.rounded(amount/z["km"]) if amount is not None and z["km"] is not None and z["km"]>0 else None
        issues=[{"code":"balance","message":msg,"basis":"Расчёт месячного баланса"} for msg in z["flags"]]
        def warn(code,message,basis):issues.append({"code":code,"message":message,"basis":basis})
        if x.get("previous_odo") is not None and p.get("odo_first") is not None and abs(x["previous_odo"]-p["odo_first"])>.005:
            warn("odometer_gap","Начальный одометр отличается от предыдущего месяца","Предыдущий конечный одометр: "+str(x["previous_odo"]))
        if len(hist)>=2:
            avg_km=sum(r["result"]["km"] for _,r in hist)/len(hist)
            avg_rate=sum(r["result"]["actual"] for _,r in hist)/sum(r["result"]["km"] for _,r in hist)*100
            if z["km"] is not None and z["km"]>avg_km*1.5:warn("mileage","Пробег более чем на 50% выше исторического среднего",f"Среднее {avg_km:.2f} км; {len(hist)} закрытых месяцев")
            if cur_rate is not None and cur_rate>avg_rate*1.3:warn("consumption","Расход на 100 км более чем на 30% выше исторического",f"Среднее {avg_rate:.2f} л/100 км; {len(hist)} закрытых месяцев")
        if z["km"] is not None and z["km"]>0 and z["norm"] is not None and z["start"] is not None and z["end"] is not None and z["norm"]>z["start"]+z["card"]+z["manual_in"]-z["manual_out"]-z["end"]+max(20,z["norm"]*.2):
            warn("possible_missing","Проверьте возможный пропуск заправки или отклонение от нормы","Расход по норме заметно выше доступного баланса; это не доказательство пропуска")
        if not report.get("closed"):
            sheets=operations.waybills(c,month,vid)
            if sheets:
                for k,expected in (("odo_first",sheets[0]["odo_first"]),("odo_last",sheets[-1]["odo_last"])):
                    if p.get(k) is not None and expected is not None and abs(p[k]-expected)>.005:warn("document_difference","Показания месячной карточки и путевых листов различаются",f"{k}: карточка {p[k]}, путевой лист {expected}")
        if not report.get("closed"):
            for card in x.get("current_cards",[]):
                if card["conflict"]:warn("card_conflict","Текущая карта водителя имеет другую историческую связь в этом месяце","Карта "+card["number"]+"; проверьте передачу карты, не изменяйте связь автоматически")
        for t in history or []:
            if t["flags"]:warn("operation",f"{t['moment']} · карта {t['card_number']}: "+ "; ".join(t["flags"]),"Операция из выписки")
        out.append({"id":vid,"plate":x["plate"],"model":x["model"],"fuel_type":kind,"purchase_amount":amount,"purchase_per_km":km_cost,"rate":cur_rate,"previous_month":previous,"previous_rate":old_rate,"rate_change":writeoff.rounded(cur_rate-old_rate) if cur_rate is not None and old_rate is not None else None,"annual_rate":writeoff.rounded(used/km*100) if km else None,"annual_months":len(annual),"issues":issues})
    projection=forecast(c,month)
    predicted={r["id"]:r for r in projection["rows"]}
    for r in out:
        forecast_row=predicted.get(r["id"],{})
        r["forecast"]=forecast_row.get("forecast");r["forecast_samples"]=len(forecast_row.get("samples",[]))
    return {"month":month,"rows":out,"unassigned":report["unassigned"],"method":"Стоимость — нетто покупок по картам нужного вида топлива, не бухгалтерская стоимость списания. Годовой расход — взвешенный по пробегу, только закрытые месяцы до выбранного периода включительно. Сигналы отклонений не изменяют данные."}
