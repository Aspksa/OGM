from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import re
import zipfile

MAX_FILE=12*1024*1024
SCHEMA="""
CREATE TABLE IF NOT EXISTS fuel_imports(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, imported_at TEXT NOT NULL, source BLOB NOT NULL, inserted INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fuel_operations(id INTEGER PRIMARY KEY, import_id INTEGER REFERENCES fuel_imports(id), card_id INTEGER REFERENCES cards(id), card_number TEXT NOT NULL, moment TEXT NOT NULL, operation TEXT NOT NULL, station TEXT NOT NULL, product TEXT NOT NULL, category TEXT NOT NULL, price INTEGER NOT NULL, quantity INTEGER NOT NULL, amount INTEGER NOT NULL, signature TEXT NOT NULL, occurrence INTEGER NOT NULL, UNIQUE(signature,occurrence));
CREATE INDEX IF NOT EXISTS fuel_ops_moment ON fuel_operations(moment);
CREATE INDEX IF NOT EXISTS fuel_ops_month ON fuel_operations(substr(moment,1,7));
CREATE TABLE IF NOT EXISTS fuel_links(id INTEGER PRIMARY KEY, card_id INTEGER REFERENCES cards(id), at TEXT NOT NULL, employee_id INTEGER REFERENCES employees(id), vehicle_id INTEGER REFERENCES vehicles(id), fuel_type TEXT NOT NULL, UNIQUE(card_id,at));
CREATE TABLE IF NOT EXISTS fuel_periods(id INTEGER PRIMARY KEY, card_id INTEGER REFERENCES cards(id), start TEXT NOT NULL, end TEXT NOT NULL, employee_id INTEGER REFERENCES employees(id), vehicle_id INTEGER REFERENCES vehicles(id), fuel_type TEXT NOT NULL, CHECK(start<end));
CREATE INDEX IF NOT EXISTS fuel_periods_lookup ON fuel_periods(card_id,start,end);
"""
def initialize(c):
    if "fuel_type" not in {r[1] for r in c.execute("PRAGMA table_info(vehicles)")}:
        c.execute("ALTER TABLE vehicles ADD COLUMN fuel_type TEXT NOT NULL DEFAULT '' CHECK(fuel_type IN ('','Бензин','Дизель'))")
    c.executescript(SCHEMA)
    cols={r[1] for r in c.execute("PRAGMA table_info(fuel_imports)")}
    if "cancelled_at" not in cols:c.execute("ALTER TABLE fuel_imports ADD COLUMN cancelled_at TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS fuel_corrections(id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT)""")
    sync_links(c)

def sync_links(c):
    now=datetime.now().isoformat(timespec="microseconds")
    for r in c.execute("""SELECT c.id, CASE WHEN c.archived=0 THEN e.id END employee_id, CASE WHEN c.archived=0 THEN v.id END vehicle_id, CASE WHEN c.archived=0 THEN COALESCE(v.fuel_type,'') ELSE '' END fuel_type FROM cards c LEFT JOIN employees e ON e.id=c.employee_id AND e.archived=0 LEFT JOIN vehicles v ON v.employee_id=e.id AND v.archived=0""").fetchall():
        old=c.execute("SELECT employee_id,vehicle_id,fuel_type FROM fuel_links WHERE card_id=? ORDER BY at DESC LIMIT 1",(r[0],)).fetchone()
        value=tuple(r)[1:]
        if old is None or tuple(old)!=value:
            c.execute("INSERT INTO fuel_links(card_id,at,employee_id,vehicle_id,fuel_type) VALUES(?,?,?,?,?)",(r[0],now,*value))

def legacy_category(product):
    t=product.casefold()
    return "Бензин" if "бензин" in t else "Дизель" if "дизель" in t or re.search(r"\bдт\b",t) else "Другое"

def category(product):
    t=product.casefold().strip()
    petrol=bool("бензин" in t or re.search(r"(?<!\w)(?:аи|ai|а-и|a-i|а|a)[\s\-–]*(?:76|80|92|93|95|98|100)(?!\d)",t))
    diesel=bool("дизель" in t or re.search(r"(?<!\w)(?:дт|dt)(?![а-яa-z])",t))
    return "Бензин" if petrol and not diesel else "Дизель" if diesel and not petrol else "Другое"

def signature(row):
    # Keep identity compatible with all 0.2–0.4 imports despite improved classification.
    legacy=(*row[:5],legacy_category(row[4]),*row[6:])
    return sha256(json.dumps(legacy,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

def link_at(c,card_id,when):
    link=c.execute("SELECT employee_id,vehicle_id,fuel_type FROM fuel_periods WHERE card_id=? AND start<=? AND end>?",(card_id,when,when)).fetchone()
    if link is None:
        link=c.execute("SELECT employee_id,vehicle_id,fuel_type FROM fuel_links WHERE card_id=? AND at<=? ORDER BY at DESC LIMIT 1",(card_id,when)).fetchone()
    return tuple(link) if link else (None,None,"")

def cents(value):
    d=Decimal(str(value).replace(" ","").replace("\xa0","").replace(",","."))
    if not d.is_finite() or abs(d)>1000000000: raise ValueError("Некорректное число")
    return int((d*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))

def stamp(day,clock):
    if isinstance(day,datetime): day=day.date()
    if not isinstance(day,date):
        try: day=datetime.strptime(str(day).strip(),"%d.%m.%Y").date()
        except ValueError: day=date.fromisoformat(str(day).strip())
    if isinstance(clock,datetime): clock=clock.time()
    if not isinstance(clock,time): clock=time.fromisoformat(str(clock).strip())
    return datetime.combine(day,clock).isoformat(timespec="seconds")

def parse_rows(rows):
    card=None; result=[]; order=list(range(8));card_col=None
    aliases=(
        {"операция","тип операции","вид операции"},
        {"дата","дата операции"},
        {"время","время операции"},
        {"азс","станция","адрес азс","место заправки"},
        {"топливо","продукт","товар","наименование"},
        {"цена","цена за литр","цена руб/л","цена, руб"},
        {"кол-во","количество","объем","объём","литры","количество, л"},
        {"сумма","сумма, руб","стоимость","сумма, ₽"})
    for i,raw in enumerate(rows,1):
        values=list(raw)
        label=str(values[0] or "").strip() if values else ""
        m=re.match(r"^Карта\s*№\s*(\d+)(?:\s|$)",label,re.I)
        if m: card=m.group(1);card_col=None;continue
        if re.match(r"^Карта\s*№",label,re.I): raise ValueError(f"Строка {i}: неверный номер карты")
        normalized=[" ".join(str(x or "").strip().casefold().split()) for x in values]
        if any(x in aliases[0] for x in normalized):
            matches=[[j for j,v in enumerate(normalized) if v in names] for names in aliases]
            if any(len(x)!=1 for x in matches):raise ValueError(f"Строка {i}: неполные или неоднозначные заголовки")
            order=[x[0] for x in matches]
            card_matches=[j for j,v in enumerate(normalized) if v in {"карта","номер карты","№ карты","топливная карта","номер топливной карты"}]
            if len(card_matches)>1:raise ValueError(f"Строка {i}: неоднозначный столбец карты")
            card_col=card_matches[0] if card_matches else None
            continue
        r=[values[j] if j<len(values) else None for j in order]
        op=str(r[0] or "").strip().casefold()
        if op not in ("отгрузка","возврат"):
            if op and any(r[j] not in (None,"") for j in (1,2)) and any(r[j] not in (None,"") for j in (5,6,7)):
                # Reject transaction-like rows, but leave explicit statement subtotals separate.
                if not op.startswith(("итого","всего","итог","сальдо")):
                    raise ValueError(f"Строка {i}: неизвестная операция {op[:80]}")
            continue
        try:
            if card_col is not None:
                val=values[card_col] if card_col<len(values) else None
                if not isinstance(val,str) or not re.fullmatch(r"\d+",val.strip()):
                    raise ValueError("Номер карты в столбце должен быть текстом (с сохранением ведущих нулей)")
                card=val.strip()
            if not card: raise ValueError("Нет номера карты")
            when=stamp(r[1],r[2]); station=str(r[3] or "").strip(); product=str(r[4] or "").strip()
            price,qty,amount=map(cents,r[5:8])
            if not station or not product or price<0 or (op=="возврат" and (qty>0 or amount>0)) or (op=="отгрузка" and (qty<0 or amount<0)): raise ValueError("Некорректная операция")
            result.append((card,when,op,station,product,category(product),price,qty,amount))
        except Exception as e: raise ValueError(f"Строка {i}: {e}") from e
    return result

def open_legacy_xls(xlrd,blob):
    # Old Russian supplier exports (BIFF before 8) omit CODEPAGE.
    # Preserve explicit workbook encodings and Unicode BIFF8; use CP1251
    # only when xlrd would otherwise silently fall back to Latin-1.
    from io import StringIO
    diagnostics=StringIO()
    book=xlrd.open_workbook(file_contents=blob,on_demand=True,logfile=diagnostics)
    if book.biff_version<80 and (book.codepage is None or "No CODEPAGE record" in diagnostics.getvalue()):
        book.release_resources()
        return xlrd.open_workbook(file_contents=blob,on_demand=True,encoding_override="cp1251",logfile=StringIO())
    return book

def parse_file(name,blob):
    result=[]
    if name.lower().endswith(".xls"):
        import xlrd
        book=open_legacy_xls(xlrd,blob)
        try:
            for sheet in book.sheets():
                if sheet.nrows>100000 or sheet.ncols>256: raise ValueError("Слишком большая таблица")
                def rows():
                    for i in range(sheet.nrows):
                        values=[]
                        for cell in sheet.row(i):
                            v=cell.value
                            if cell.ctype==xlrd.XL_CELL_DATE:
                                d=xlrd.xldate.xldate_as_datetime(v,book.datemode); v=d.time() if 0<=v<1 else d
                            values.append(v)
                        yield values
                result.extend(parse_rows(rows()))
        finally: book.release_resources()
    elif name.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        with zipfile.ZipFile(BytesIO(blob)) as z:
            if sum(x.file_size for x in z.infolist())>100*1024*1024: raise ValueError("Слишком большой архив")
        book=load_workbook(BytesIO(blob),read_only=True,data_only=True,keep_links=False)
        try:
            for sheet in book.worksheets:
                if (sheet.max_row or 0)>100000 or (sheet.max_column or 0)>256: raise ValueError("Слишком большая таблица")
                result.extend(parse_rows(sheet.iter_rows(max_col=max(8,sheet.max_column or 8),values_only=True)))
        finally: book.close()
    else: raise ValueError("Поддерживаются только XLS и XLSX")
    return result

def ingest(c,name,blob):
    if not 0<len(blob)<=MAX_FILE: raise ValueError("Лимит файла 12 МБ")
    name=name.replace("\\","/").rsplit("/",1)[-1][:200]; digest=sha256(blob).hexdigest()
    c.execute("BEGIN IMMEDIATE")
    try:
        previous=c.execute("SELECT id,cancelled_at FROM fuel_imports WHERE digest=?",(digest,)).fetchone()
        if previous and not previous["cancelled_at"]:
            c.rollback();return {"duplicate":True,"inserted":0,"skipped":0,"new_cards":0}
        rows=parse_file(name,blob)
        if not rows: raise ValueError("Не найдено операций")
        if previous:
            ident=previous["id"]
            c.execute("UPDATE fuel_imports SET cancelled_at=NULL,imported_at=?,filename=? WHERE id=?",(datetime.now().isoformat(timespec="microseconds"),name,ident))
            c.execute("INSERT INTO fuel_corrections(at,kind,reason,before_json,after_json) VALUES(?,?,?,?,?)",(datetime.now().isoformat(),"restore_import","Повторная загрузка исходного файла",json.dumps(dict(previous)),json.dumps({"id":ident})))
        else:
            ident=c.execute("INSERT INTO fuel_imports(filename,digest,imported_at,source) VALUES(?,?,?,?)",(name,digest,datetime.now().isoformat(timespec="microseconds"),blob)).lastrowid
        counts=Counter(); inserted=skipped=new_cards=0
        for row in rows:
            card=c.execute("SELECT id FROM cards WHERE number=?",(row[0],)).fetchone()
            if card: card_id=card[0]
            else:
                card_id=c.execute("INSERT INTO cards(number) VALUES(?)",(row[0],)).lastrowid;new_cards+=1
            sig=signature(row)
            counts[sig]+=1
            if c.execute("SELECT 1 FROM writeoff_closures WHERE month=?",(row[1][:7],)).fetchone() and not c.execute("SELECT 1 FROM fuel_operations WHERE signature=? AND occurrence=?",(sig,counts[sig])).fetchone():raise ValueError("Новые операции за закрытый месяц: "+row[1][:7])
            cur=c.execute("INSERT INTO fuel_operations(import_id,card_id,card_number,moment,operation,station,product,category,price,quantity,amount,signature,occurrence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signature,occurrence) DO NOTHING",(ident,card_id,*row,sig,counts[sig]))
            if cur.rowcount: inserted+=1
            else: skipped+=1
        sync_links(c)
        c.execute("UPDATE fuel_imports SET inserted=?,skipped=? WHERE id=?",(inserted,skipped,ident))
        c.commit();return {"duplicate":False,"inserted":inserted,"skipped":skipped,"new_cards":new_cards}
    except BaseException: c.rollback();raise

def cancel_import(c,d):
    ident=d.get("id");reason=d.get("reason")
    if type(ident) is not int or not isinstance(reason,str) or not reason.strip() or len(reason)>500:raise ValueError("Укажите файл и основание отмены")
    c.execute("BEGIN IMMEDIATE")
    try:
        last=c.execute("SELECT id FROM fuel_imports WHERE cancelled_at IS NULL ORDER BY imported_at DESC,id DESC LIMIT 1").fetchone()
        if not last or last[0]!=ident:raise ValueError("Отменять можно только последний действующий импорт: это защищает перекрывающиеся ведомости")
        rows=[dict(r) for r in c.execute("SELECT * FROM fuel_operations WHERE import_id=?",(ident,))]
        for m in {r["moment"][:7] for r in rows}:
            if c.execute("SELECT 1 FROM writeoff_closures WHERE month=?",(m,)).fetchone():raise ValueError("Импорт затрагивает закрытый месяц")
        at=datetime.now().isoformat(timespec="microseconds")
        c.execute("INSERT INTO fuel_corrections(at,kind,reason,before_json) VALUES(?,?,?,?)",(at,"cancel_import",reason.strip(),json.dumps({"import_id":ident,"operations":rows},ensure_ascii=False)))
        c.execute("DELETE FROM fuel_operations WHERE import_id=?",(ident,))
        c.execute("UPDATE fuel_imports SET cancelled_at=? WHERE id=?",(at,ident))
        c.commit()
    except BaseException:c.rollback();raise

def period_guard(c,start,end):
    if c.execute("SELECT 1 FROM writeoff_closures WHERE month||'-01T00:00:00'<? AND date(month||'-01','+1 month')||'T00:00:00'>?",(end,start)).fetchone():raise ValueError("Период пересекает закрытый месяц")

def add_period(c,d):
    ident=d.get("id");action=d.get("action","save");reason=d.get("reason","")
    if action not in ("save","delete"):raise ValueError("Неизвестное действие")
    if ident is not None and (type(ident) is not int or not isinstance(reason,str) or not reason.strip() or len(reason)>500):raise ValueError("Укажите основание исправления периода")
    if action=="delete":
        if ident is None:raise ValueError("Период не выбран")
        c.execute("BEGIN IMMEDIATE")
        try:
            old=c.execute("SELECT * FROM fuel_periods WHERE id=?",(ident,)).fetchone()
            if not old:raise ValueError("Период не найден")
            period_guard(c,old["start"],old["end"])
            c.execute("INSERT INTO fuel_corrections(at,kind,reason,before_json) VALUES(?,?,?,?)",(datetime.now().isoformat(),"delete_period",reason.strip(),json.dumps(dict(old),ensure_ascii=False)))
            c.execute("DELETE FROM fuel_periods WHERE id=?",(ident,))
            c.commit();return
        except BaseException:c.rollback();raise
    card,employee,vehicle=(d.get(k) for k in ("card_id","employee_id","vehicle_id"))
    start=date.fromisoformat(d["start"]);end=date.fromisoformat(d["end"])+timedelta(days=1)
    if end<=start or type(card) is not int or type(employee) is not int or (vehicle is not None and type(vehicle) is not int): raise ValueError("Неверный период или закрепление")
    start=start.isoformat()+"T00:00:00";end=end.isoformat()+"T00:00:00"
    c.execute("BEGIN IMMEDIATE")
    try:
        if not c.execute("SELECT 1 FROM cards WHERE id=?",(card,)).fetchone() or not c.execute("SELECT 1 FROM employees WHERE id=?",(employee,)).fetchone(): raise ValueError("Запись не найдена")
        v=c.execute("SELECT fuel_type FROM vehicles WHERE id=?",(vehicle,)).fetchone() if vehicle else None
        if vehicle and not v: raise ValueError("Автомобиль не найден")
        if c.execute("SELECT 1 FROM writeoff_closures WHERE month||\'-01T00:00:00\'<? AND date(month||\'-01\',\'+1 month\')||\'T00:00:00\'>?",(end,start)).fetchone():raise ValueError("Период пересекает закрытый месяц")
        old=c.execute("SELECT * FROM fuel_periods WHERE id=?",(ident,)).fetchone() if ident is not None else None
        if ident is not None:
            if not old:raise ValueError("Период не найден")
            period_guard(c,old["start"],old["end"])
        if c.execute("SELECT 1 FROM fuel_periods WHERE card_id=? AND start<? AND end>? AND id!=?",(card,end,start,ident or 0)).fetchone(): raise ValueError("Периоды пересекаются")
        values=(card,start,end,employee,vehicle,v[0] if v else "")
        if old:
            c.execute("UPDATE fuel_periods SET card_id=?,start=?,end=?,employee_id=?,vehicle_id=?,fuel_type=? WHERE id=?",(*values,ident))
            new=c.execute("SELECT * FROM fuel_periods WHERE id=?",(ident,)).fetchone()
            c.execute("INSERT INTO fuel_corrections(at,kind,reason,before_json,after_json) VALUES(?,?,?,?,?)",(datetime.now().isoformat(),"edit_period",reason.strip(),json.dumps(dict(old),ensure_ascii=False),json.dumps(dict(new),ensure_ascii=False)))
        else:c.execute("INSERT INTO fuel_periods(card_id,start,end,employee_id,vehicle_id,fuel_type) VALUES(?,?,?,?,?,?)",values)
        c.commit()
    except BaseException: c.rollback();raise

def report(c,month=""):
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",month): raise ValueError("Некорректный месяц")
    query="SELECT id,card_id,card_number,moment,operation,station,product,category,price,quantity,amount FROM fuel_operations"
    rows=[dict(r) for r in c.execute(query+(" WHERE substr(moment,1,7)=?" if month else "")+" ORDER BY moment,id",(month,) if month else ())]
    employees={r["id"]:" ".join(filter(None,(r["last_name"],r["first_name"],r["middle_name"]))) for r in c.execute("SELECT * FROM employees")}
    vehicles={r["id"]:" ".join((r["make"],r["model"],r["plate"])) for r in c.execute("SELECT * FROM vehicles")}
    for r in rows:
        e,v,f=link_at(c,r["card_id"],r["moment"])
        r["category"]=category(r["product"])
        r.update(employee_id=e,vehicle_id=v,employee=employees.get(e,""),vehicle=vehicles.get(v,""))
        flags=[]
        if not e: flags.append("Нет закрепления на дату")
        elif not v: flags.append("Нет автомобиля на дату")
        elif not f: flags.append("Не указан вид топлива автомобиля")
        elif f!=r["category"]: flags.append("Несоответствие топлива")
        if abs(r["price"]*r["quantity"]-r["amount"]*100)>100: flags.append("Цена × объём не совпадает с суммой")
        r["flags"]=flags
    return {"rows":rows,"months":[r[0] for r in c.execute("SELECT DISTINCT substr(moment,1,7) FROM fuel_operations ORDER BY 1 DESC")],"imports":[dict(r) for r in c.execute("SELECT id,filename,imported_at,inserted,skipped,cancelled_at FROM fuel_imports ORDER BY imported_at DESC,id DESC")],"history":[dict(r) for r in c.execute("SELECT l.*,c.number FROM fuel_links l JOIN cards c ON c.id=l.card_id ORDER BY at DESC")],"periods":[dict(r) for r in c.execute("SELECT l.*,c.number FROM fuel_periods l JOIN cards c ON c.id=l.card_id ORDER BY start DESC")]}

def export(c,month=""):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    rows=report(c,month)["rows"];wb=Workbook();ws=wb.active;ws.title="Операции"
    def add(sheet,headers,values):
        sheet.append(headers)
        for cell in sheet[1]: cell.font=Font(bold=True,color="FFFFFF");cell.fill=PatternFill("solid",fgColor="324860")
        for row in values:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str): cell.data_type="s"
        sheet.freeze_panes="A2";sheet.auto_filter.ref=sheet.dimensions
        for col in sheet.columns: sheet.column_dimensions[col[0].column_letter].width=min(55,max(14,max(len(str(x.value or "")) for x in col)+2))
    add(ws,["Карта","Дата","Операция","Водитель","Автомобиль","АЗС","Топливо","Цена","Литры","Рубли","Замечания"],[[r["card_number"],r["moment"],r["operation"],r["employee"],r["vehicle"],r["station"],r["product"],r["price"]/100,r["quantity"]/100,r["amount"]/100,"; ".join(r["flags"])] for r in rows])
    for name,key in (("Водители","employee"),("Автомобили","vehicle"),("Топливо","category")):
        groups=defaultdict(lambda:[0,0,0])
        for r in rows:
            g=groups[r[key] or "Не определено"];g[0]+=1;g[1]+=r["quantity"];g[2]+=r["amount"]
        add(wb.create_sheet(name),[name,"Операции","Литры","Рубли"],[[k,v[0],v[1]/100,v[2]/100] for k,v in sorted(groups.items())])
    out=BytesIO();wb.save(out);return out.getvalue()
