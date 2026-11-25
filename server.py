from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import math
import sqlite3
import webbrowser
import threading

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "ogm.sqlite3"
KINDS = {
    "employees": ("last_name", "first_name", "middle_name"),
    "vehicles": ("make", "model", "plate", "employee_id", "summer", "winter", "insurance_start", "insurance_end"),
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
            keys = KINDS[kind]
            if old:
                c.execute(f"UPDATE {kind} SET " + ",".join(k+"=?" for k in keys) + " WHERE id=?", (*values.values(), ident))
            else:
                c.execute(f"INSERT INTO {kind} (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")", tuple(values.values()))
        else:
            raise ValueError("Неизвестное действие")
        c.commit()
    except BaseException:
        c.rollback()
        raise

class Handler(BaseHTTPRequestHandler):
    def send_data(self, status, payload, content_type="application/json; charset=utf-8"):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
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
        assets = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
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
    initialize()
    if not (ROOT/"static"/"app.js").is_file():
        raise SystemExit("Build UI first: npm run build")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print("OGM:", url, flush=True)
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
