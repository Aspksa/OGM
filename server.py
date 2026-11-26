from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import math
import sqlite3
import webbrowser
import threading
from urllib.parse import urlsplit, parse_qs, quote
import fuel
import writeoff
import operations
import automation
import maintenance
import os
import logging
from logging.handlers import RotatingFileHandler

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "ogm.sqlite3"
INBOX_STATE = {"rows":[],"scanned_at":None,"integrity_ok":None,"folder":str(DB.parent/"incoming")}
INBOX_CACHE = {}
KINDS = {
    "employees": ("last_name", "first_name", "middle_name", "department"),
    "vehicles": ("make", "model", "plate", "employee_id", "summer", "winter", "insurance_start", "insurance_end", "fuel_type", "status"),
    "cards": ("number", "employee_id"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
 id INTEGER PRIMARY KEY, last_name TEXT NOT NULL, first_name TEXT NOT NULL,
 middle_name TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
CREATE TABLE IF NOT EXISTS vehicles (
 id INTEGER PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL, plate TEXT NOT NULL UNIQUE,
 employee_id INTEGER UNIQUE REFERENCES employees(id), summer REAL CHECK(summer>=0), winter REAL CHECK(winter>=0),
 insurance_start TEXT, insurance_end TEXT, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
CREATE TABLE IF NOT EXISTS cards (
 id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, employee_id INTEGER UNIQUE REFERENCES employees(id),
 archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
"""

def connect(path=DB):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def initialize(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as c, c:
        c.executescript(SCHEMA)
        fuel.initialize(c)
        writeoff.initialize(c)
        operations.initialize(c)
        c.execute("PRAGMA user_version=7")

def snapshot(c):
    return {kind: [dict(r) for r in c.execute(f"SELECT * FROM {kind} ORDER BY id")] for kind in KINDS}

def clean(kind, data):
    out = {}
    required = {"last_name", "first_name", "make", "model", "plate", "number"}
    for key in KINDS[kind]:
        val = data.get(key)
        if key == "employee_id":
            if val in ("", None):
                val = None
            elif type(val) is not int or val < 1:
                raise ValueError("Некорректный сотрудник")
        elif key in ("summer", "winter"):
            if val in ("", None):
                val = None
            else:
                if isinstance(val, bool):
                    raise ValueError("Некорректная норма")
                val = float(val)
                if not math.isfinite(val) or val < 0:
                    raise ValueError("Норма должна быть неотрицательной")
        elif key == "status":
            val = val or "Работает"
            if val not in ("Работает","Ремонт","Консервация","Списан"):
                raise ValueError("Некорректный статус")
        elif key == "fuel_type":
            val = val or ""
            if val not in ("", "Бензин", "Дизель"):
                raise ValueError("Выберите бензин или дизель")
        elif key.startswith("insurance_"):
            val = date.fromisoformat(val).isoformat() if val else None
        else:
            if val is None:
                val = ""
            if not isinstance(val, str):
                raise ValueError("Ожидается текст")
            val = val.strip()
            if len(val) > 200 or (key in required and not val):
                raise ValueError("Заполните обязательные поля")
            if key == "plate":
                val = "".join(val.upper().split()).translate(str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ"))
            if key == "number":
                val = "".join(val.split())
        out[key] = val
    if kind == "vehicles" and out["insurance_start"] and out["insurance_end"] and out["insurance_start"] > out["insurance_end"]:
        raise ValueError("Дата окончания страховки раньше начала")
    return out

def change(c, kind, data):
    if kind not in KINDS or not isinstance(data, dict):
        raise ValueError("Неизвестный справочник или формат")
    ident = data.get("id")
    if ident is not None and (type(ident) is not int or ident < 1):
        raise ValueError("Некорректный идентификатор")
    c.execute("BEGIN IMMEDIATE")
    try:
        old = c.execute(f"SELECT * FROM {kind} WHERE id=?", (ident,)).fetchone() if ident else None
        if ident and not old:
            raise ValueError("Запись не найдена")
        action = data.get("action", "save")
        if action in ("archive", "restore"):
            if not old:
                raise ValueError("Запись не найдена")
            archived = int(action == "archive")
            c.execute(f"UPDATE {kind} SET archived=? WHERE id=?", (archived, ident))
            if archived and kind == "employees":
                for table in ("vehicles", "cards"):
                    c.execute(f"UPDATE {table} SET employee_id=NULL WHERE employee_id=?", (ident,))
            elif archived and kind != "employees":
                c.execute(f"UPDATE {kind} SET employee_id=NULL WHERE id=?", (ident,))
        elif action == "save":
            if old and old["archived"]:
                raise ValueError("Сначала восстановите запись")
            values = clean(kind, data)
            employee = values.get("employee_id")
            if employee and not c.execute("SELECT 1 FROM employees WHERE id=? AND archived=0", (employee,)).fetchone():
                raise ValueError("Сотрудник отсутствует или архивирован")
            if kind == 'cards' and old and old['number'] != values['number'] and c.execute('SELECT 1 FROM fuel_operations WHERE card_id=?',(ident,)).fetchone():
                raise ValueError('Нельзя изменить номер карты с операциями')
            keys = KINDS[kind]
            if old:
                c.execute(f"UPDATE {kind} SET " + ",".join(k+"=?" for k in keys) + " WHERE id=?", (*values.values(), ident))
            else:
                c.execute(f"INSERT INTO {kind} (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")", tuple(values.values()))
        else:
            raise ValueError("Неизвестное действие")
        fuel.sync_links(c)
        operations.sync_history(c)
        c.commit()
    except BaseException:
        c.rollback()
        raise

class Handler(BaseHTTPRequestHandler):
    def send_data(self, status, payload, content_type="application/json; charset=utf-8", filename=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename,safe=""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def local_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.local_host():
            return self.send_data(403, {"error":"Недопустимый адрес"})
        if self.path == "/api/data":
            with closing(connect()) as c:
                c.execute("BEGIN")
                return self.send_data(200, snapshot(c))
        parts=urlsplit(self.path)
        args=parse_qs(parts.query)
        if parts.path.startswith("/api/automation/"):
            try:
                with closing(connect()) as c:
                    c.execute("BEGIN")
                    kind=parts.path.split("/")[-1]
                    month=args.get("month",[""])[0]
                    if kind=="readiness": result=automation.readiness(c,month)
                    elif kind=="workspace": result=automation.month_workspace(c,month)
                    elif kind=="forecast": result=automation.forecast(c,month)
                    elif kind=="prices": result=automation.price_trend(c,month)
                    elif kind=="vehicle-analysis": result=automation.vehicle_analysis(c,month)
                    elif kind=="inbox": result=INBOX_STATE
                    elif kind=="mileage":
                        vid=int(args.get("id",["0"])[0])
                        writeoff.month_bounds(month)
                        result=automation.mileage_proposal(c,month,vid,writeoff.stored(c,month,vid)[0])
                    elif kind=="export":
                        return self.send_data(200,automation.export_review(c,month),"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","OGM-review-"+month+".xlsx")
                    else:return self.send_data(404,{"error":"Не найдено"})
                    return self.send_data(200,result)
            except (ValueError,TypeError,KeyError,OverflowError) as e:return self.send_data(400,{"error":str(e)})
            except (sqlite3.Error,ImportError):return self.send_data(503,{"error":"Ошибка базы или зависимостей"})
        if parts.path=="/api/ops/backup-download":
            try:
                name=args.get("name",[""])[0]
                if Path(name).name!=name or not name.endswith(".sqlite3"):raise ValueError("Некорректное имя файла")
                path=DB.parent/"backups"/name
                if not path.is_file() or path.is_symlink():return self.send_data(404,{"error":"Копия не найдена"})
                with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as c:maintenance.validate(c)
                return self.send_data(200,path.read_bytes(),"application/octet-stream",name)
            except (ValueError,OSError,sqlite3.Error):return self.send_data(400,{"error":"Не удалось проверить резервную копию"})
        if parts.path.startswith("/api/ops/"):
            try:
                with closing(connect()) as c:
                    c.execute("BEGIN")
                    kind=parts.path.split("/")[-1]
                    month=args.get("month",[""])[0]
                    if kind=="waybills": result=operations.waybills(c,month)
                    elif kind=="suggest":
                        vid=int(args.get("id",["0"])[0])
                        result=operations.suggestion(c,month,vid,writeoff.stored(c,month,vid)[0])
                    elif kind=="analytics":result=operations.analytics(c,args.get("year",[str(date.today().year)])[0])
                    elif kind=="fuel":result=operations.fuel_stats(c,month)
                    elif kind=="insurance":result=operations.insurance_calendar(c)
                    elif kind=="vehicle":result=operations.vehicle_detail(c,int(args.get("id",["0"])[0]))
                    elif kind=="card":result=operations.card_detail(c,int(args.get("id",["0"])[0]))
                    elif kind=="settings":result=operations.season_config(c)
                    elif kind=="audit":result=[dict(r) for r in c.execute("SELECT * FROM ogm_audit ORDER BY id DESC LIMIT 500")]
                    elif kind=="diagnostics":
                        result=operations.diagnostics(c)
                        result["server"]="OK"
                        result["ui"]={name:(ROOT/"static"/name).is_file() for name in ("app.js","fuel-ui.js","writeoff-ui.js","operations-ui.js")}
                        result["backups"]=[p.name for p in sorted((DB.parent/"backups").glob("*.sqlite3"),reverse=True)]
                        result["schema_version"]=c.execute("PRAGMA user_version").fetchone()[0]
                        result["last_backup"]=result["backups"][0] if result["backups"] else None
                    else:return self.send_data(404,{"error":"Не найдено"})
                    return self.send_data(200,result)
            except (ValueError,TypeError,KeyError,OverflowError) as e:return self.send_data(400,{"error":str(e)})
            except sqlite3.Error:return self.send_data(503,{"error":"Ошибка базы"})
        if parts.path in ("/api/writeoff","/api/writeoff/export","/api/writeoff/source"):
            try:
                with closing(connect()) as c:
                    c.execute("BEGIN")
                    month=args.get("month",[""])[0]
                    if parts.path=="/api/writeoff": return self.send_data(200,writeoff.report(c,month))
                    if parts.path=="/api/writeoff/export": return self.send_data(200,writeoff.export(c,month),"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","OGM-writeoff-"+month+".xlsx")
                    source=c.execute("SELECT name,source FROM writeoff_sources WHERE id=?",(int(args.get("id",["0"])[0]),)).fetchone()
                    if not source: return self.send_data(404,{"error":"Файл не найден"})
                    return self.send_data(200,source["source"],"application/octet-stream",source["name"])
            except (ValueError,TypeError): return self.send_data(400,{"error":"Некорректный запрос"})
            except ImportError: return self.send_data(503,{"error":"Установите зависимости"})
            except sqlite3.Error: return self.send_data(503,{"error":"Ошибка базы данных"})
        if parts.path in ("/api/fuel","/api/fuel/export","/api/fuel/source"):
            try:
                with closing(connect()) as c:
                    c.execute("BEGIN")
                    month=args.get("month",[""])[0]
                    if parts.path=="/api/fuel": return self.send_data(200,fuel.report(c,month))
                    if parts.path=="/api/fuel/export": return self.send_data(200,fuel.export(c,month),"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","OGM-GSM.xlsx")
                    source=c.execute("SELECT filename,source FROM fuel_imports WHERE id=?",(int(args.get("id",["0"])[0]),)).fetchone()
                    if not source: return self.send_data(404,{"error":"Файл не найден"})
                    return self.send_data(200,source["source"],"application/octet-stream",source["filename"])
            except (ValueError,TypeError): return self.send_data(400,{"error":"Некорректный запрос"})
            except ImportError: return self.send_data(503,{"error":"Установите зависимости из requirements.txt"})
        assets = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"), "/app.js": ("app.js", "text/javascript"), "/fuel-ui.js": ("fuel-ui.js","text/javascript"), "/writeoff-ui.js": ("writeoff-ui.js","text/javascript"), "/operations-ui.js": ("operations-ui.js","text/javascript")}
        if self.path not in assets:
            return self.send_data(404, {"error":"Не найдено"})
        name, mime = assets[self.path]
        try:
            return self.send_data(200, (ROOT/"static"/name).read_bytes(), mime+"; charset=utf-8")
        except FileNotFoundError:
            return self.send_data(503, {"error":"Сначала соберите интерфейс: npm run build"})

    def do_POST(self):
        if not self.local_host() or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
            return self.send_data(403, {"error":"Недопустимый источник"})
        parts=urlsplit(self.path)
        if parts.path=="/api/automation/preview":
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=fuel.MAX_FILE:raise ValueError("Недопустимый размер файла")
                body=self.rfile.read(size)
                if len(body)!=size:raise ValueError("Файл получен не полностью")
                name=parse_qs(parts.query).get("name",[""])[0]
                with closing(connect()) as c:
                    c.execute("BEGIN")
                    return self.send_data(200,automation.preview(c,name,body))
            except ValueError as e:return self.send_data(400,{"error":str(e)})
            except (sqlite3.Error,ImportError):return self.send_data(503,{"error":"Ошибка базы или зависимостей"})
            except Exception:return self.send_data(400,{"error":"Не удалось распознать книгу Excel"})
        if parts.path in ("/api/ops/waybill","/api/ops/settings","/api/ops/backup"):
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=65536 or self.headers.get_content_type()!="application/json":raise ValueError("Некорректный запрос")
                d=json.loads(self.rfile.read(size))
                if not isinstance(d,dict):raise ValueError("Некорректный запрос")
                with closing(connect()) as c:
                    if parts.path.endswith("/waybill"):operations.save_waybill(c,d)
                    elif parts.path.endswith("/settings"):operations.set_seasons(c,d)
                    else:
                        filename=maintenance.backup(DB).name
                        with c:c.execute("INSERT INTO ogm_audit(at,entity,action,row_id) VALUES(?,?,?,?)",(writeoff.now(),"backup","CREATE",filename))
                        return self.send_data(200,{"filename":filename})
                return self.send_data(200,{"ok":True})
            except (ValueError,TypeError,KeyError,OverflowError) as e:return self.send_data(400,{"error":str(e)})
            except (sqlite3.Error,OSError):return self.send_data(409,{"error":"Ошибка записи или резервного копирования"})
        if parts.path in ("/api/writeoff/save","/api/writeoff/import","/api/writeoff/month-action","/api/writeoff/link-card","/api/writeoff/batch"):
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=(fuel.MAX_FILE if parts.path.endswith("import") else 1024*1024): raise ValueError("Недопустимый размер запроса")
                body=self.rfile.read(size)
                with closing(connect()) as c:
                    if parts.path.endswith("import"):
                        args=parse_qs(parts.query)
                        return self.send_data(200,writeoff.import_old(c,args.get("month",[""])[0],args.get("name",[""])[0],body))
                    d=json.loads(body)
                    if not isinstance(d,dict):raise ValueError("Некорректный запрос")
                    if parts.path.endswith("/month-action"):writeoff.month_action(c,d.get("month"),d.get("action"),d.get("reason"))
                    elif parts.path.endswith("/link-card"):writeoff.confirm_month_card(c,d)
                    elif parts.path.endswith("/batch"):return self.send_data(200,writeoff.save_batch(c,d))
                    else:writeoff.save(c,d)
                    return self.send_data(200,{"ok":True})
            except (ValueError,TypeError,KeyError,OverflowError) as e: return self.send_data(400,{"error":str(e)})
            except sqlite3.Error: return self.send_data(409,{"error":"Ошибка записи SQLite"})
            except ImportError: return self.send_data(503,{"error":"Установите зависимости"})
            except Exception: return self.send_data(400,{"error":"Не удалось обработать запрос или файл"})
        if parts.path in ("/api/fuel/import","/api/fuel/period","/api/fuel/cancel"):
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=(fuel.MAX_FILE if parts.path.endswith("import") else 16384): raise ValueError("Недопустимый размер запроса")
                body=self.rfile.read(size)
                with closing(connect()) as c:
                    if parts.path.endswith("import"):
                        name=parse_qs(parts.query).get("name",[""])[0]
                        automation.file_kind(name,body)
                        return self.send_data(200,fuel.ingest(c,name,body))
                    d=json.loads(body)
                    if not isinstance(d,dict):raise ValueError("Некорректный запрос")
                    if parts.path.endswith("/cancel"):fuel.cancel_import(c,d)
                    else:fuel.add_period(c,d)
                    return self.send_data(200,{"ok":True})
            except ImportError: return self.send_data(503,{"error":"Установите зависимости из requirements.txt"})
            except (ValueError,TypeError,KeyError,OverflowError) as e: return self.send_data(400,{"error":str(e)})
            except sqlite3.Error: return self.send_data(409,{"error":"Ошибка записи в SQLite"})
            except Exception: return self.send_data(400,{"error":"Файл не удалось прочитать. Проверьте формат ведомости."})
        kind = self.path.removeprefix("/api/")
        if self.path != "/api/"+kind or kind not in KINDS:
            return self.send_data(404, {"error":"Не найдено"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384 or self.headers.get_content_type() != "application/json":
                raise ValueError("Некорректный запрос")
            data = json.loads(self.rfile.read(size))
            with closing(connect()) as c:
                change(c, kind, data)
                c.execute("BEGIN")
                self.send_data(200, snapshot(c))
        except sqlite3.IntegrityError:
            self.send_data(409, {"error":"Дубликат номера или сотрудник уже закреплён. Снимите прежнее закрепление."})
        except (ValueError, TypeError, OverflowError):
            self.send_data(400, {"error":"Проверьте поля, даты, нормы и закрепление."})
        except sqlite3.Error:
            self.send_data(503, {"error":"Ошибка базы данных. Повторите попытку."})

def main():
    with maintenance.instance_lock(DB.parent):
        log=RotatingFileHandler(DB.parent/"ogm.log",maxBytes=2*1024*1024,backupCount=3,encoding="utf-8")
        logging.basicConfig(level=logging.WARNING,handlers=[log],format="%(asctime)s %(levelname)s %(message)s")
        try:run_server()
        except Exception:
            logging.exception("Server startup/runtime failure")
            raise

def run_server():
    if DB.exists():
        try: maintenance.daily(DB)
        except (ValueError,sqlite3.Error,OSError) as e: logging.warning('Backup failure: %s',type(e).__name__);print('Backup warning:',e,flush=True)
    initialize()
    try: maintenance.daily(DB)
    except (ValueError,sqlite3.Error,OSError) as e: print('Backup warning:',e,flush=True)
    if not all((ROOT/"static"/name).is_file() for name in ("app.js","fuel-ui.js","writeoff-ui.js","operations-ui.js")):
        raise SystemExit("Build UI first: npm run build")
    # Stable origin keeps browser preferences; fallback if the port is occupied.
    try: server=ThreadingHTTPServer(("127.0.0.1",8765),Handler)
    except OSError: server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    with server:
        stop=threading.Event()
        def backup_loop():
            while not stop.wait(3600):
                try: maintenance.daily(DB)
                except (ValueError,sqlite3.Error,OSError) as e: logging.warning("Backup failure: %s",type(e).__name__);print("Backup warning:",e,flush=True)
        worker=threading.Thread(target=backup_loop,daemon=True)
        worker.start()
        def inbox_loop():
            global INBOX_STATE
            while not stop.is_set():
                try:INBOX_STATE=maintenance.inspect_inbox(DB,INBOX_CACHE)
                except (OSError,ValueError,sqlite3.Error) as e:
                    INBOX_STATE={"rows":[],"scanned_at":None,"integrity_ok":None,"folder":str(DB.parent/"incoming"),"error":str(e)}
                if stop.wait(60):break
        scanner=threading.Thread(target=inbox_loop,daemon=True)
        scanner.start()
        url = f"http://127.0.0.1:{server.server_port}"
        print("OGM:", url, flush=True)
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            worker.join()
            scanner.join()

if __name__ == "__main__":
    main()
