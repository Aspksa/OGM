import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from openpyxl import Workbook, load_workbook
import xlwt
import fuel
from server import initialize, connect, change

ROWS=[
    ["Карта №  0000000000000000001       Авто: "],
    ["Операция","Дата","Время","Азс","Топливо","Цена","Кол-во","Сумма"],
    ["Отгрузка","01.10.2026","08:06:08","АЗС 1","Бензин АИ-95",70,40,2800],
    ["Возврат","02.10.2026","08:06:08","АЗС 1","Бензин АИ-95",70,-2,-140],
]
def fixture(ext):
    out=BytesIO()
    if ext=="xlsx":
        wb=Workbook()
        for row in ROWS: wb.active.append(row)
        wb.save(out)
    else:
        wb=xlwt.Workbook();s=wb.add_sheet("Sheet1")
        for i,row in enumerate(ROWS):
            for j,value in enumerate(row): s.write(i,j,value)
        wb.save(out)
    return out.getvalue()

class FuelTests(unittest.TestCase):
    def test_ingest_history_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"test.db";initialize(path)
            with closing(connect(path)) as c:
                blob=fixture("xlsx")
                result=fuel.ingest(c,"test.xlsx",blob)
                self.assertEqual(result["inserted"],2)
                self.assertEqual(result["new_cards"],1)
                self.assertTrue(fuel.ingest(c,"renamed.xlsx",blob)["duplicate"])
                self.assertEqual(fuel.ingest(c,"test.xls",fixture("xls"))["skipped"],2)
                self.assertEqual(c.execute("SELECT number FROM cards").fetchone()[0],"0000000000000000001")
                self.assertEqual(sum(r["quantity"] for r in fuel.report(c,"2026-10")["rows"]),3800)
                self.assertTrue(all(r["flags"] for r in fuel.report(c,"2026-10")["rows"]))
                change(c,"employees",{"first_name":"Тест","last_name":"Тест"})
                change(c,"vehicles",{"make":"Test","model":"A","plate":"A123AA","fuel_type":"Дизель","employee_id":1})
                fuel.add_period(c,{"card_id":1,"employee_id":1,"vehicle_id":1,"start":"2026-10-01","end":"2026-10-31"})
                self.assertTrue(all("Несоответствие топлива" in r["flags"] for r in fuel.report(c,"2026-10")["rows"]))
                with self.assertRaises(ValueError):
                    fuel.add_period(c,{"card_id":1,"employee_id":1,"vehicle_id":1,"start":"2026-10-02","end":"2026-11-01"})
                wb=load_workbook(BytesIO(fuel.export(c,"2026-10")));self.assertEqual(wb["Операции"]["A2"].value,"0000000000000000001");wb.close()
                change(c,"cards",{"id":1,"number":"0000000000000000001","employee_id":1})
                self.assertEqual(c.execute("SELECT employee_id FROM fuel_links WHERE card_id=1 ORDER BY at DESC LIMIT 1").fetchone()[0],1)
                change(c,"employees",{"id":1,"action":"archive"})
                self.assertIsNone(c.execute("SELECT employee_id FROM fuel_links WHERE card_id=1 ORDER BY at DESC LIMIT 1").fetchone()[0])
                self.assertEqual(fuel.report(c,"2026-10")["rows"][0]["employee_id"],1)
    def test_return_sign(self):
        with self.assertRaises(ValueError):
            fuel.parse_rows([["Карта № 0001"],["Возврат","01.10.2026","12:00:00","АЗС","Дизель",70,2,140]])
