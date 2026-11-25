import json
import tempfile
import threading
import unittest
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import server
import maintenance
import fuel

class ApiAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/"data"/"ogm.sqlite3"
        server.initialize(self.db)
        original=server.connect
        self.patches=[patch.object(server,"DB",self.db),patch.object(server,"connect",side_effect=lambda:original(self.db))]
        for p in self.patches:p.start()
        self.http=ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def request(self,method,path,data=None,origin=True,host=None):
        port=self.http.server_port
        headers={"Host":host or f"127.0.0.1:{port}"}
        if origin:headers["Origin"]=f"http://127.0.0.1:{port}"
        body=None
        if data is not None:
            body=json.dumps(data).encode();headers["Content-Type"]="application/json"
        with closing(HTTPConnection("127.0.0.1",port,timeout=10)) as conn:
            conn.request(method,path,body,headers);r=conn.getresponse();return r.status,r.read()
    def test_http_guards_and_json(self):
        self.assertEqual(self.request("GET","/api/data",host="example.com")[0],403)
        self.assertEqual(self.request("POST","/api/employees",{"last_name":"Тест","first_name":"Водитель"},origin=False)[0],403)
        self.assertEqual(self.request("POST","/api/employees",{"last_name":"Тест","first_name":"Водитель"})[0],200)
        code,raw=self.request("GET","/api/data")
        self.assertEqual(code,200)
        self.assertEqual(len(json.loads(raw)["employees"]),1)
        self.assertEqual(self.request("GET","/api/automation/readiness?month=invalid")[0],400)
        self.assertEqual(self.request("POST","/api/writeoff/month-action",{"month":"2026-10","action":"close","reason":"test"})[0],400)
    def test_verified_backup_download(self):
        p=maintenance.backup(self.db)
        code,raw=self.request("GET","/api/ops/backup-download?name="+p.name)
        self.assertEqual(code,200)
        self.assertEqual(raw[:16],b"SQLite format 3\x00")
        self.assertEqual(self.request("GET","/api/ops/backup-download?name=../ogm.sqlite3")[0],400)
        external=Path(self.tmp.name)/"external";external.mkdir()
        copy=maintenance.backup(self.db,"external",external)
        self.assertEqual(copy.parent,external.resolve())

class HeaderAuditTests(unittest.TestCase):
    def test_reordered_header(self):
        rows=[["Карта № 000123"],["Дата","АЗС","Сумма","Операция","Топливо","Количество","Время","Цена"],["01.10.2026","АЗС","2800","Отгрузка","АИ-95","40","12:00:00","70"]]
        self.assertEqual(fuel.parse_rows(rows)[0][0],"000123")
        self.assertEqual(fuel.parse_rows(rows)[0][7],4000)
        rows[2][3]="Неизвестно"
        with self.assertRaises(ValueError):fuel.parse_rows(rows)
