"""Monthly fuel balance; source figures are never silently corrected."""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from hashlib import sha256
from collections import defaultdict
import json
import re
import zipfile
import fuel

SCHEMA="""
CREATE TABLE IF NOT EXISTS writeoff_months(
 month TEXT NOT NULL, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
 payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(month,vehicle_id));
CREATE TABLE IF NOT EXISTS writeoff_audit(
 id INTEGER PRIMARY KEY, month TEXT NOT NULL, vehicle_id INTEGER REFERENCES vehicles(id),
 at TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT, after_json TEXT);
CREATE TABLE IF NOT EXISTS writeoff_sources(
 id INTEGER PRIMARY KEY, month TEXT NOT NULL, name TEXT NOT NULL, digest TEXT NOT NULL,
 source BLOB NOT NULL, rows_json TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(month,digest));
"""
def initialize(c): c.executescript(SCHEMA)

def month_bounds(month):
    if not isinstance(month,str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",month): raise ValueError("Выберите месяц")
    start=date.fromisoformat(month+"-01")
    end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    return start,end

def number(v):
    if v is None or v=="": return None
    if isinstance(v,bool): raise ValueError("Некорректное число")
    try: d=Decimal(str(v).replace(",","."))
    except Exception as e: raise ValueError("Некорректное число") from e
    if not d.is_finite() or d<0 or d>1000000000: raise ValueError("Число должно быть от 0 до 1 млрд")
    return float(d.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))

def text(v,limit=500):
    if v is None: return ""
    if not isinstance(v,str) or len(v)>limit: raise ValueError("Некорректный текст")
    return v.strip()

def rounded(v): return float(Decimal(str(v)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))
def now(): return datetime.now().isoformat(timespec="microseconds")
def plate(v): return "".join(str(v).upper().split()).translate(str.maketrans("ABCEHKMOPTXY","АВСЕНКМОРТХУ"))

def stored(c,month,vid):
    row=c.execute("SELECT payload,revision FROM writeoff_months WHERE month=? AND vehicle_id=?",(month,vid)).fetchone()
    return (json.loads(row[0]),row[1]) if row else ({},0)

def previous(c,month,vid):
    start,_=month_bounds(month); prev=(start-timedelta(days=1)).strftime("%Y-%m")
    p,_=stored(c,prev,vid)
    return p.get("end")

def clean(c,month,vid,d):
    start,end=month_bounds(month)
    if not isinstance(d,dict): raise ValueError("Некорректная карточка")
    out={k:number(d.get(k)) for k in ("start","end","odo_first","odo_last","km_override","summer_km","winter_km","summer_norm","winter_norm","capacity")}
    for k in ("first_ref","last_ref","km_reason","note","category","driver_label"):
        out[k]=text(d.get(k))
    for k in ("first_date","last_date"):
        val=text(d.get(k))
        if val and not start<=date.fromisoformat(val)<end: raise ValueError("Дата путевого листа вне месяца")
        out[k]=val
    if out["first_date"] and out["last_date"] and out["first_date"]>out["last_date"]: raise ValueError("Первый лист позже последнего")
    if out["km_override"] is not None and not out["km_reason"]: raise ValueError("Укажите причину корректировки пробега")
    out["season"]=d.get("season","")
    if out["season"] not in ("","summer","winter","split"): raise ValueError("Выберите сезон")
    ops=d.get("manual",[])
    if not isinstance(ops,list) or len(ops)>1000: raise ValueError("Не более 1000 ручных операций на автомобиль за месяц")
    out["manual"]=[]
    for op in ops:
        if not isinstance(op,dict): raise ValueError("Некорректная операция")
        day=date.fromisoformat(text(op.get("date")))
        if not start<=day<end: raise ValueError("Ручная операция вне месяца")
        kind=op.get("kind")
        if kind not in ("in","out"): raise ValueError("Выберите приход или слив/передачу")
        liters=number(op.get("liters"))
        if not liters: raise ValueError("Введите положительный объём")
        product=op.get("fuel_type")
        if product not in ("Бензин","Дизель"): raise ValueError("Укажите вид топлива")
        doc=text(op.get("document")); confirmed=op.get("confirmed") is True
        if confirmed and not doc: raise ValueError("Для подтверждения укажите документ или основание")
        out["manual"].append({"date":day.isoformat(),"kind":kind,"liters":liters,"fuel_type":product,
          "organization":text(op.get("organization")),"document":doc,"confirmed":confirmed,"note":text(op.get("note"))})
    return out

def save(c,d):
    month=d.get("month");month_bounds(month);vid=d.get("vehicle_id")
    if type(vid) is not int or not c.execute("SELECT 1 FROM vehicles WHERE id=?",(vid,)).fetchone(): raise ValueError("Автомобиль не найден")
    reason=text(d.get("reason"))
    if not reason: raise ValueError("Укажите причину сохранения/изменения")
    p=clean(c,month,vid,d.get("payload"))
    c.execute("BEGIN IMMEDIATE")
    try:
        old,rev=stored(c,month,vid)
        if type(d.get("revision")) is not int or d["revision"]!=rev: raise ValueError("Карточка изменена, обновите страницу")
        raw=json.dumps(p,ensure_ascii=False)
        if rev:
            c.execute("UPDATE writeoff_months SET payload=?,revision=? WHERE month=? AND vehicle_id=?",(raw,rev+1,month,vid))
        else: c.execute("INSERT INTO writeoff_months(month,vehicle_id,payload) VALUES(?,?,?)",(month,vid,raw))
        c.execute("INSERT INTO writeoff_audit(month,vehicle_id,at,reason,before_json,after_json) VALUES(?,?,?,?,?,?)",(month,vid,now(),reason,json.dumps(old,ensure_ascii=False) if rev else None,raw))
        c.commit()
    except BaseException: c.rollback();raise

def calculate(p,card,vehicle_fuel,prev,issues=()):
    flags=list(issues)
    initial=p.get("start");end=p.get("end")
    if initial is None: initial=prev
    if initial is None or end is None: flags.append("Не указан остаток")
    if p.get("start") is not None and prev is not None and abs(p["start"]-prev)>0.005: flags.append("Начальный остаток отличается от прошлого месяца")
    if prev is None: flags.append("Нет подтверждённого остатка прошлого месяца")
    capacity=p.get("capacity")
    if capacity is None or capacity<=0: flags.append("Не указана вместимость бака")
    elif any(v is not None and v>capacity for v in (initial,end)): flags.append("Остаток больше вместимости бака")
    first,last=p.get("odo_first"),p.get("odo_last"); rawkm=None
    if first is not None and last is not None:
        if last<first: flags.append("Конечный одометр меньше начального")
        else: rawkm=rounded(last-first)
    else: flags.append("Нет полного комплекта показаний одометра")
    km=p.get("km_override") if p.get("km_override") is not None else rawkm
    if km is None: flags.append("Пробег не определён")
    if rawkm is not None and p.get("km_override") is not None and abs(rawkm-km)>0.005: flags.append("Использован скорректированный пробег вместо показаний одометра")
    if km==0 and initial is not None and end is not None and initial+card-end>0: flags.append("Нулевой пробег при изменении топливного баланса")
    if not p.get("first_ref") or not p.get("last_ref"): flags.append("Не указаны первый/последний путевые листы")
    ins=outs=unconfirmed=0
    for op in p.get("manual",[]):
        if not vehicle_fuel or op["fuel_type"]!=vehicle_fuel: flags.append("Ручная операция: вид топлива не совпадает или не указан");continue
        if not op["confirmed"]: unconfirmed+=op["liters"];flags.append("Есть неподтверждённые ручные операции")
        if op["kind"]=="in": ins+=op["liters"]
        else: outs+=op["liters"]
        if capacity and op["liters"]>capacity and op["kind"]=="in": flags.append("Ручная заправка превышает вместимость")
    actual=rounded(initial+card+ins-outs-end) if initial is not None and end is not None and vehicle_fuel else None
    if actual is not None and actual<0: flags.append("Отрицательный фактический расход: проверьте баланс")
    norm=None;season=p.get("season")
    if km is not None:
        if season in ("summer","winter"):
            rate=p.get(season+"_norm")
            if rate is not None and rate>0: norm=rounded(km*rate/100)
        elif season=="split":
            sk,wk,sn,wn=(p.get(k) for k in ("summer_km","winter_km","summer_norm","winter_norm"))
            if None not in (sk,wk,sn,wn) and sn>0 and wn>0:
                if abs(sk+wk-km)>0.005: flags.append("Сумма сезонных пробегов не совпадает")
                else: norm=rounded((sk*sn+wk*wn)/100)
    if norm is None: flags.append("Не определён расход по норме")
    return {"start":initial,"end":end,"raw_km":rawkm,"km":km,"card":card,"manual_in":rounded(ins),"manual_out":rounded(outs),"unconfirmed":rounded(unconfirmed),"actual":actual,"norm":norm,"difference":rounded(actual-norm) if actual is not None and norm is not None else None,"flags":list(dict.fromkeys(flags))}

def report(c,month):
    month_bounds(month);ops=fuel.report(c,month)["rows"]
    by=defaultdict(list)
    for r in ops:
        if r["vehicle_id"]: by[r["vehicle_id"]].append(r)
    rows=[]
    for v in c.execute("SELECT * FROM vehicles ORDER BY plate").fetchall():
        p,rev=stored(c,month,v["id"]);prev=previous(c,month,v["id"])
        if v["archived"] and not rev and not by[v["id"]]: continue
        p={"summer_norm":v["summer"],"winter_norm":v["winter"],**p}
        card=0;flags=[]
        for op in by[v["id"]]:
            if op["category"]==v["fuel_type"] and v["fuel_type"]: card+=op["quantity"]
            else: flags.append("Операция по карте с другим или неизвестным топливом исключена из баланса")
            if op["flags"]: flags.extend(op["flags"])
            if p.get("capacity") and op["quantity"]/100>p["capacity"]: flags.append("Заправка по карте превышает вместимость")
        driver=c.execute("SELECT last_name,first_name,middle_name FROM employees WHERE id=?",(v["employee_id"],)).fetchone()
        rows.append({"id":v["id"],"plate":v["plate"],"model":v["make"]+" "+v["model"],"fuel_type":v["fuel_type"],"driver":" ".join(filter(None,driver)) if driver else "", "revision":rev,"payload":p,"previous":prev,"result":calculate(p,card/100,v["fuel_type"],prev,flags)})
    sources=[{"id":r["id"],"name":r["name"],"rows":json.loads(r["rows_json"]),"at":r["at"]} for r in c.execute("SELECT * FROM writeoff_sources WHERE month=? ORDER BY id DESC",(month,))]
    audit=[dict(r) for r in c.execute("SELECT * FROM writeoff_audit WHERE month=? ORDER BY id DESC",(month,))]
    return {"month":month,"rows":rows,"unassigned":sum(1 for r in ops if not r["vehicle_id"]),"sources":sources,"audit":audit}

def import_old(c,month,name,blob):
    month_bounds(month)
    if not name.lower().endswith(".xlsx") or not 0<len(blob)<=fuel.MAX_FILE: raise ValueError("Нужен XLSX до 12 МБ")
    from openpyxl import load_workbook
    with zipfile.ZipFile(BytesIO(blob)) as z:
        if sum(x.file_size for x in z.infolist())>100*1024*1024: raise ValueError("Слишком большой архив")
    book=load_workbook(BytesIO(blob),read_only=True,data_only=True,keep_links=False)
    try:
        ws=book.worksheets[0]
        if ws.max_row>10000 or ws.max_column>256: raise ValueError("Слишком большая ведомость")
        if str(ws["A7"].value).strip().lower()!="гос номер" or str(ws["E7"].value).strip()!="Пробег (км)": raise ValueError("Не соответствует образцу отчёта")
        rows=[]
        for i,r in enumerate(ws.iter_rows(min_row=9,max_col=18,values_only=True),9):
            if str(r[0]).strip().upper()=="ИТОГО": break
            if not r[0]: continue
            vals=[number(v) for v in r[4:17]]
            match=c.execute("SELECT id FROM vehicles WHERE plate=?",(plate(r[0]),)).fetchone()
            issues=[]
            active=[any((vals[j+offset] or 0)!=0 for j in (1,3,5,7,9,11)) for offset in (0,1)]
            if all(active): issues.append("В строке есть значения по двум видам топлива")
            for offset,label in ((0,"ДТ"),(1,"Бензин")):
                a,b,actual,end=(vals[j+offset] for j in (1,3,5,9))
                if a is not None and end is not None and abs(rounded(a+(b or 0)-end)-(actual or 0))>0.01:
                    issues.append(label+": исходный факт не совпадает с балансом без дополнительных операций")
            rows.append({"row":i,"plate":str(r[0]),"model":str(r[1] or ""),"category":str(r[2] or ""),"driver":str(r[3] or ""),"values":vals,"note":str(r[17] or ""),"flags":issues,"vehicle_id":match[0] if match else None})
        if not rows: raise ValueError("Нет автомобилей")
    finally: book.close()
    digest=sha256(blob).hexdigest();name=name.replace("\\","/").rsplit("/",1)[-1][:200]
    with c:
        cur=c.execute("INSERT INTO writeoff_sources(month,name,digest,source,rows_json,at) VALUES(?,?,?,?,?,?) ON CONFLICT(month,digest) DO NOTHING",(month,name,digest,blob,json.dumps(rows,ensure_ascii=False),now()))
    return {"rows":len(rows),"matched":sum(r["vehicle_id"] is not None for r in rows),"duplicate":not bool(cur.rowcount)}

def export(c,month):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.worksheet.page import PageMargins
    from openpyxl.utils import get_column_letter
    data=report(c,month);wb=Workbook();ws=wb.active;ws.title="Списание ГСМ"
    ws.merge_cells("I1:R2");ws["I1"]="Согласовано: __________________________"
    ws.merge_cells("A4:R4");ws["A4"]="АНАЛИЗ РАСХОДА ГСМ"
    ws.merge_cells("A6:R6");ws["A6"]="За "+month
    for col,label in [(1,"Госномер"),(2,"Модель"),(3,"Тип"),(4,"Фамилия, имя, отчество"),(5,"Пробег (км)"),(18,"Примечание")]:
        ws.merge_cells(start_row=7,start_column=col,end_row=8,end_column=col);ws.cell(7,col,label)
    for col,label in [(6,"Остаток на начало"),(8,"Заправка"),(10,"Расход факт"),(12,"Расход по норме"),(14,"Остаток на конец"),(16,"Слив / передача")]:
        ws.merge_cells(start_row=7,start_column=col,end_row=7,end_column=col+1);ws.cell(7,col,label);ws.cell(8,col,"ДТ");ws.cell(8,col+1,"Бензин")
    for i,r in enumerate(data["rows"],9):
        p=r["payload"];x=r["result"];values=[r["plate"],r["model"],p.get("category",""),p.get("driver_label") or r["driver"],x["km"]]+[None]*12+["; ".join(filter(None,[p.get("note",""),*x["flags"]]))]
        if r["fuel_type"] in ("Дизель","Бензин"):
            offset=0 if r["fuel_type"]=="Дизель" else 1
            for j,val in enumerate((x["start"],rounded(x["card"]+x["manual_in"]),x["actual"],x["norm"],x["end"],x["manual_out"])): values[5+j*2+offset]=val
        for j,val in enumerate(values,1):
            cell=ws.cell(i,j,val)
            if isinstance(val,str): cell.data_type="s"
            if 5<=j<=17: cell.number_format="0.00"
    last=8+len(data["rows"]);total=last+1;ws.cell(total,1,"ИТОГО")
    for j in range(5,18):
        col=get_column_letter(j)
        if last>=9: ws.cell(total,j,f'=IF(COUNT({col}9:{col}{last})=0,"",SUM({col}9:{col}{last}))')
    ws.merge_cells(start_row=total+2,start_column=1,end_row=total+2,end_column=18);ws.cell(total+2,1,"Ответственный за составление: ____________________  Подпись: __________")
    ws.merge_cells(start_row=total+3,start_column=1,end_row=total+3,end_column=18);ws.cell(total+3,1,"Предварительный расчёт; операции без привязки к автомобилю: "+str(data["unassigned"]))
    for i,width in enumerate([14,22,12,25,12]+[11]*12+[38],1):ws.column_dimensions[get_column_letter(i)].width=width
    for row in ws.iter_rows(min_row=7,max_row=total,max_col=18):
        for cell in row:
            cell.alignment=Alignment(vertical="center",wrap_text=True,horizontal="center" if cell.column>=5 and cell.column<18 else "left")
            cell.border=Border(bottom=Side(style="thin",color="CBD5E1"))
            if cell.row<=8 or cell.row==total: cell.fill=PatternFill("solid",fgColor="324860");cell.font=Font(bold=True,color="FFFFFF")
    ws["A4"].font=Font(size=16,bold=True);ws["A4"].alignment=Alignment(horizontal="center")
    ws.freeze_panes="F9";ws.print_title_rows="7:8";ws.print_area=f"A1:R{total+3}";ws.sheet_properties.pageSetUpPr.fitToPage=True;ws.page_setup.orientation="landscape";ws.page_setup.paperSize=ws.PAPERSIZE_A3;ws.page_setup.fitToWidth=1;ws.page_setup.fitToHeight=0;ws.page_margins=PageMargins(left=.25,right=.25,top=.35,bottom=.35,header=.1,footer=.1)
    detail=wb.create_sheet("Операции вручную");detail.append(["Госномер","Дата","Тип","Топливо","Литры","Организация","Документ","Подтверждено","Примечание"])
    for r in data["rows"]:
        for op in r["payload"].get("manual",[]):
            detail.append([r["plate"],op["date"],op["kind"],op["fuel_type"],op["liters"],op["organization"],op["document"],"Да" if op["confirmed"] else "Нет",op["note"]])
            for cell in detail[detail.max_row]:
                if isinstance(cell.value,str): cell.data_type="s"
    out=BytesIO();wb.save(out);return out.getvalue()
