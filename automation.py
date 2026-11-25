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
    rows=fuel.parse_file(name,blob)
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
    counts=Counter();duplicates=0;unassigned=0;unknown=0;mismatch=0
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
        sig=fuel.signature(row);counts[sig]+=1
        if c.execute("SELECT 1 FROM fuel_operations WHERE signature=? AND occurrence=?",(sig,counts[sig])).fetchone():duplicates+=1
    def metric(g):return {**g,"liters":g["liters"]/100,"amount":g["amount"]/100}
    return {"kind":"fuel","format":ext,"digest":sha256(blob).hexdigest(),"operations":len(rows),
        "duplicates":duplicates,"new_cards":sum(k not in cards for k in groups),"unassigned":unassigned,"unknown":unknown,"mismatch":mismatch,
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

def readiness(c,month):
    writeoff.month_bounds(month);r=writeoff.report(c,month);rows=[]
    for x in r["rows"]:
        z=x["result"];p=x["payload"]
        blocked=[];warnings=[]
        if not x["revision"]:blocked.append("Месячная карточка не сохранена")
        if not x["fuel_type"]:blocked.append("Не указан вид топлива")
        for key,label in (("start","Начальный остаток"),("end","Конечный остаток"),("km","Пробег"),("actual","Факт"),("norm","Норма")):
            if z[key] is None:blocked.append(label+": нет данных")
        if z["actual"] is not None and z["actual"]<0:blocked.append("Отрицательный расход")
        if z["unconfirmed"]:blocked.append("Неподтверждённые операции")
        if not p.get("first_ref") or not p.get("last_ref"):blocked.append("Не указаны путевые листы")
        if not r.get("closed"):
            sheets=operations.waybills(c,month,x["id"])
            refs={s["number"] for s in sheets}
            if p.get("first_ref") not in refs or p.get("last_ref") not in refs:blocked.append("Первый или последний лист отсутствует в реестре")
            if sheets and (p.get("first_ref")!=sheets[0]["number"] or p.get("last_ref")!=sheets[-1]["number"]):blocked.append("Первый или последний лист не совпадает с реестром")
            if len({s["day"] for s in sheets})!=len(sheets):blocked.append("Не подтверждён порядок нескольких путевых листов за день")
            if any(a["odo_last"] is not None and b["odo_first"] is not None and a["odo_last"]>b["odo_first"] for a,b in zip(sheets,sheets[1:])):blocked.append("Пересекающиеся показания путевых листов")
        for flag in z["flags"]:
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
