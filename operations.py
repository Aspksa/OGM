"""Operational history, waybills, analytics and integrity diagnostics."""
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import sqlite3
import re
import fuel
import writeoff

SCHEMA="""
CREATE TABLE IF NOT EXISTS waybills(
 id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
 employee_id INTEGER REFERENCES employees(id), number TEXT NOT NULL, day TEXT NOT NULL,
 odo_first REAL, odo_last REAL, fuel_first REAL, fuel_last REAL, note TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), UNIQUE(vehicle_id,day,number));
CREATE TABLE IF NOT EXISTS vehicle_history(
 id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), at TEXT NOT NULL,
 employee_id INTEGER REFERENCES employees(id), summer REAL, winter REAL, status TEXT NOT NULL, fuel_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ogm_audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, entity TEXT NOT NULL, action TEXT NOT NULL, row_id TEXT, before_json TEXT, after_json TEXT);
CREATE TABLE IF NOT EXISTS ogm_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS waybill_month ON waybills(day,vehicle_id);
CREATE INDEX IF NOT EXISTS vehicle_history_idx ON vehicle_history(vehicle_id,at);
"""
def initialize(c):
    for table,col,definition in (("employees","department","TEXT NOT NULL DEFAULT ''"),("vehicles","status","TEXT NOT NULL DEFAULT 'Работает'")):
        if col not in {r[1] for r in c.execute("PRAGMA table_info("+table+")")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    c.executescript(SCHEMA)
    for table in ("employees","vehicles","cards","fuel_imports","fuel_operations","fuel_links","fuel_periods","writeoff_months","writeoff_sources","waybills","ogm_settings"):
        cols=[r[1] for r in c.execute("PRAGMA table_info("+table+")") if r[1]!="source"]
        def obj(prefix): return "json_object("+",".join("'"+k+"',"+prefix+"."+k for k in cols)+")"
        # Audit fuel imports at file level; operation rows remain available in the ledger.
        if table=="fuel_operations": continue
        idexpr=lambda prefix: prefix+(".id" if "id" in cols else ".key" if "key" in cols else ".month||':'||"+prefix+".vehicle_id")
        for action in ("INSERT","UPDATE","DELETE"):
            before="NULL" if action=="INSERT" else obj("OLD")
            after="NULL" if action=="DELETE" else obj("NEW")
            rowid=idexpr("OLD" if action=="DELETE" else "NEW")
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS ogm_audit_{table}_{action.lower()} AFTER {action} ON {table}
              BEGIN INSERT INTO ogm_audit(at,entity,action,row_id,before_json,after_json)
              VALUES(strftime('%Y-%m-%dT%H:%M:%f','now'),'{table}','{action}',{rowid},{before},{after}); END""")
    sync_history(c)

def sync_history(c):
    at=datetime.now().isoformat(timespec="microseconds")
    for v in c.execute("SELECT id,employee_id,summer,winter,status,fuel_type FROM vehicles").fetchall():
        old=c.execute("SELECT employee_id,summer,winter,status,fuel_type FROM vehicle_history WHERE vehicle_id=? ORDER BY id DESC LIMIT 1",(v[0],)).fetchone()
        if old is None or tuple(old)!=tuple(v)[1:]:
            c.execute("INSERT INTO vehicle_history(vehicle_id,at,employee_id,summer,winter,status,fuel_type) VALUES(?,?,?,?,?,?,?)",(v[0],at,*tuple(v)[1:]))

def setting(c,key): 
    r=c.execute("SELECT value FROM ogm_settings WHERE key=?",(key,)).fetchone()
    return r[0] if r else ""

def season_config(c):
    return {"summer":setting(c,"summer_start"),"winter":setting(c,"winter_start")}

def set_seasons(c,d):
    summer,winter=d.get("summer",""),d.get("winter","")
    if bool(summer)!=bool(winter) or (summer and summer==winter): raise ValueError("Укажите две разные даты или очистите обе")
    for v in (summer,winter):
        if v:
            if not re.fullmatch(r"\d{2}-\d{2}",v): raise ValueError("Формат ММ-ДД")
            date.fromisoformat("2025-"+v)
    with c:
        for key,val in (("summer_start",summer),("winter_start",winter)):
            c.execute("INSERT INTO ogm_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,val))

def season_on(day,cfg):
    s,w=cfg["summer"],cfg["winter"]
    if not s or not w:return ""
    mm=day[5:10]
    return "summer" if (s<=mm<w if s<w else mm>=s or mm<w) else "winter"

def save_waybill(c,d):
    if not isinstance(d,dict):raise ValueError("Некорректный лист")
    ident=d.get("id")
    if ident is not None and type(ident) is not int:raise ValueError("Некорректный ID")
    with c:
        if d.get("action")=="archive":
            if not ident or not c.execute("UPDATE waybills SET archived=1 WHERE id=?",(ident,)).rowcount:raise ValueError("Лист не найден")
            return
        vid,employee=d.get("vehicle_id"),d.get("employee_id")
        if type(vid) is not int or not c.execute("SELECT 1 FROM vehicles WHERE id=?",(vid,)).fetchone():raise ValueError("Автомобиль не найден")
        if employee is not None and (type(employee) is not int or not c.execute("SELECT 1 FROM employees WHERE id=?",(employee,)).fetchone()):raise ValueError("Сотрудник не найден")
        number=writeoff.text(d.get("number"),100)
        if not number:raise ValueError("Введите номер путевого листа")
        day=date.fromisoformat(d.get("day","")).isoformat()
        values=(vid,employee,number,day,*[writeoff.number(d.get(k)) for k in ("odo_first","odo_last","fuel_first","fuel_last")],writeoff.text(d.get("note")))
        if ident:
            if not c.execute("UPDATE waybills SET vehicle_id=?,employee_id=?,number=?,day=?,odo_first=?,odo_last=?,fuel_first=?,fuel_last=?,note=? WHERE id=? AND archived=0",(*values,ident)).rowcount:raise ValueError("Лист не найден")
        else:c.execute("INSERT INTO waybills(vehicle_id,employee_id,number,day,odo_first,odo_last,fuel_first,fuel_last,note) VALUES(?,?,?,?,?,?,?,?,?)",values)

def waybills(c,month="",vid=None):
    q="SELECT * FROM waybills WHERE archived=0";params=[]
    if month:
        writeoff.month_bounds(month);q+=" AND substr(day,1,7)=?";params.append(month)
    if vid is not None:q+=" AND vehicle_id=?";params.append(vid)
    return [dict(r) for r in c.execute(q+" ORDER BY day,id",params)]

def suggestion(c,month,vid,p):
    sheets=waybills(c,month,vid);v=c.execute("SELECT * FROM vehicles WHERE id=?",(vid,)).fetchone()
    if not v:raise ValueError("Автомобиль не найден")
    result={};flags=[]
    if sheets:
        first,last=sheets[0],sheets[-1]
        for key,val in (("first_ref",first["number"]),("first_date",first["day"]),("last_ref",last["number"]),("last_date",last["day"]),("odo_first",first["odo_first"]),("odo_last",last["odo_last"]),("start",first["fuel_first"]),("end",last["fuel_last"])):
            if p.get(key) in (None,"") and val is not None:result[key]=val
        if len({x["day"] for x in sheets})!=len(sheets):flags.append("Несколько листов за один день: проверьте порядок")
        for i,s in enumerate(sheets):
            if s["odo_first"] is not None and s["odo_last"] is not None and s["odo_last"]<s["odo_first"]:flags.append("Обратный одометр: "+s["number"])
            if i and sheets[i-1]["odo_last"] is not None and s["odo_first"] is not None and sheets[i-1]["odo_last"]!=s["odo_first"]:flags.append("Разрыв одометра: "+s["number"])
    else:flags.append("Нет путевых листов за месяц")
    prev=writeoff.previous(c,month,vid)
    if p.get("start") is None and "start" not in result and prev is not None:result["start"]=prev
    cfg=season_config(c);start,end=writeoff.month_bounds(month)
    seasons={season_on((start+timedelta(days=i)).isoformat(),cfg) for i in range((end-start).days)}
    if not p.get("season"):
        if len(seasons)==1 and "" not in seasons:result["season"]=next(iter(seasons))
        elif "" not in seasons and sheets:
            sums={"summer":0,"winter":0}
            valid=not flags and all(s["odo_first"] is not None and s["odo_last"] is not None and s["odo_last"]>=s["odo_first"] for s in sheets)
            if valid:
                for s in sheets:sums[season_on(s["day"],cfg)]+=s["odo_last"]-s["odo_first"]
                km=p.get("km_override")
                if km is None:km=(last["odo_last"]-first["odo_first"])
                if abs(sum(sums.values())-km)<.005:result.update(season="split",summer_km=round(sums["summer"],2),winter_km=round(sums["winter"],2))
            if "season" not in result:flags.append("Переходный месяц: подтвердите сезонные пробеги вручную")
        else:flags.append("Не настроены даты сезонных норм")
    for k,col in (("summer_norm","summer"),("winter_norm","winter")):
        if p.get(k) is None and v[col] is not None:result[k]=v[col]
    return {"fields":result,"flags":flags,"waybills":len(sheets)}

def month_list(c,year):
    if not re.fullmatch(r"\d{4}",year) or not 1<=int(year)<=9998:raise ValueError("Некорректный год")
    return [year+"-"+str(i).zfill(2) for i in range(1,13)]

def analytics(c,year):
    months=month_list(c,year);out=[];vehicle_rows=[];missing=[];over=[]
    for m in months:
        r=writeoff.report(c,m)
        # Do not convert absent monthly records to zero.
        rows=[x for x in r["rows"] if x["revision"] or x["result"]["card"] or waybills(c,m,x["id"])]
        def sumknown(key):
            vals=[x["result"][key] for x in rows if x["result"][key] is not None]
            return round(sum(vals),2) if vals else None
        out.append({"month":m,"vehicles":len(rows),"actual":sumknown("actual"),"norm":sumknown("norm"),"km":sumknown("km"),"complete":sum(x["result"]["actual"] is not None and x["result"]["norm"] is not None for x in rows),"unassigned":r["unassigned"]})
        for x in rows:
            z=x["result"];entry={"month":m,"vehicle_id":x["id"],"plate":x["plate"],"model":x["model"],"km":z["km"],"actual":z["actual"],"norm":z["norm"],"difference":z["difference"]}
            vehicle_rows.append(entry)
            if z["difference"] is not None and z["difference"]>0:over.append(entry)
    current=date.today().strftime("%Y-%m")
    # Missing data view for current month is separate from historical totals.
    if current.startswith(year):
        for x in writeoff.report(c,current)["rows"]:
            z=x["result"]
            if z["flags"]:missing.append({"month":current,"plate":x["plate"],"model":x["model"],"flags":z["flags"]})
    return {"months":out,"vehicles":vehicle_rows,"over":over,"missing":missing}

def fuel_stats(c,month):
    rows=fuel.report(c,month)["rows"];stations=defaultdict(lambda:[0,0,0]);prices=defaultdict(lambda:[0,0]);alerts=[];perday=Counter()
    for r in rows:
        if r["quantity"]>0:perday[(r["card_id"],r["moment"][:10])]+=1
    for r in rows:
        g=stations[r["station"]];g[0]+=1;g[1]+=r["quantity"];g[2]+=r["amount"]
        if r["quantity"]>0:
            key=(r["moment"][:10],r["product"]);p=prices[key];p[0]+=r["quantity"];p[1]+=r["amount"]
        tags=list(r["flags"])
        if r["quantity"]>0 and date.fromisoformat(r["moment"][:10]).weekday()>=5:tags.append("Заправка в субботу/воскресенье (не обязательно выходной по графику)")
        if r["quantity"]>0 and perday[(r["card_id"],r["moment"][:10])]>=3:tags.append("Три и более заправки карты за день")
        if r["quantity"]>0:
            if r["vehicle_id"]:
                p,_=writeoff.stored(c,r["moment"][:7],r["vehicle_id"])
                cap=p.get("capacity")
                if cap and r["quantity"]/100>cap:tags.append("Заправка выше вместимости бака")
            if r["quantity"]>=20000:tags.append("Объём от 200 л — проверить, не нарушение автоматически")
        if tags:alerts.append({**r,"alerts":tags})
    return {"stations":[{"station":k,"operations":v[0],"liters":v[1]/100,"amount":v[2]/100} for k,v in sorted(stations.items())],"prices":[{"day":k[0],"product":k[1],"price":round(v[1]/v[0],2),"liters":v[0]/100} for k,v in sorted(prices.items())],"alerts":alerts}

def vehicle_detail(c,vid):
    v=c.execute("SELECT * FROM vehicles WHERE id=?",(vid,)).fetchone()
    if not v:raise ValueError("Автомобиль не найден")
    history=[dict(r) for r in c.execute("SELECT * FROM vehicle_history WHERE vehicle_id=? ORDER BY id DESC",(vid,))]
    months=[r[0] for r in c.execute("SELECT month FROM writeoff_months WHERE vehicle_id=? ORDER BY month DESC",(vid,))]
    balances=[{"month":m,**x["result"]} for m in months for x in writeoff.report(c,m)["rows"] if x["id"]==vid]
    operations=[r for r in fuel.report(c)["rows"] if r["vehicle_id"]==vid]
    sources=[{"id":r["id"],"month":r["month"],"name":r["name"]} for r in c.execute("SELECT id,month,name,rows_json FROM writeoff_sources") if any(x.get("vehicle_id")==vid for x in json.loads(r["rows_json"]))]
    return {"vehicle":dict(v),"history":history,"balances":balances,"waybills":waybills(c,vid=vid),"operations":operations,"sources":sources}

def card_detail(c,ident):
    r=c.execute("SELECT * FROM cards WHERE id=?",(ident,)).fetchone()
    if not r:raise ValueError("Карта не найдена")
    report=fuel.report(c)
    return {"card":dict(r),"operations":[x for x in report["rows"] if x["card_id"]==ident],"history":[x for x in report["history"] if x["card_id"]==ident],"periods":[x for x in report["periods"] if x["card_id"]==ident]}

def diagnostics(c):
    checks=[r[0] for r in c.execute("PRAGMA quick_check")]
    links=[list(r) for r in c.execute("PRAGMA foreign_key_check")]
    duplicates={}
    for table,expr in (("employees","lower(trim(last_name))||' '||lower(trim(first_name))||' '||lower(trim(middle_name))"),("vehicles","plate"),("cards","number"),("fuel_operations","signature")):
        # SQLite lower() is ASCII only; normalize employee names in Python.
        if table=="employees":
            groups=defaultdict(list)
            for r in c.execute("SELECT id,last_name,first_name,middle_name FROM employees WHERE archived=0"):groups[" ".join(str(x).strip().casefold() for x in tuple(r)[1:])].append(r[0])
            duplicates[table]=[{"key":k,"ids":v} for k,v in groups.items() if len(v)>1]
        else:duplicates[table]=[dict(r) for r in c.execute(f"SELECT {expr} key,COUNT(*) count FROM {table} GROUP BY {expr} HAVING COUNT(*)>1")]
    return {"sqlite":checks,"foreign_keys":links,"duplicates":duplicates,"unassigned_cards":c.execute("SELECT COUNT(*) FROM cards WHERE archived=0 AND employee_id IS NULL").fetchone()[0],"audit_count":c.execute("SELECT COUNT(*) FROM ogm_audit").fetchone()[0]}
