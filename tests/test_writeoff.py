import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from io import BytesIO
from openpyxl import Workbook, load_workbook
from server import initialize, connect, change
import writeoff
import fuel

class WriteoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"test.sqlite"
        initialize(self.path)
        self.c=connect(self.path)
        change(self.c,"employees",{"last_name":"Тест","first_name":"Водитель"})
        change(self.c,"vehicles",{"make":"Test","model":"A","plate":"A123AA","employee_id":1,"fuel_type":"Дизель","summer":10,"winter":12})
        change(self.c,"cards",{"number":"0000000000000000001","employee_id":1})
    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()
    def save(self,month,p,revision=0):
        writeoff.save(self.c,{"month":month,"vehicle_id":1,"revision":revision,"reason":"Тест","payload":p})
    def test_month_balance_and_history(self):
        wb=Workbook()
        for r in [["Карта № 0000000000000000001"],["Отгрузка","01.10.2026","12:00:00","АЗС","Дизель",70,40,2800],["Возврат","02.10.2026","12:00:00","АЗС","Дизель",70,-5,-350]]: wb.active.append(r)
        buf=BytesIO();wb.save(buf)
        fuel.ingest(self.c,"test.xlsx",buf.getvalue())
        fuel.add_period(self.c,{"card_id":1,"employee_id":1,"vehicle_id":1,"start":"2026-10-01","end":"2026-10-31"})
        self.save("2026-09",{"end":20})
        p={"end":10,"odo_first":100,"odo_last":400,"season":"split","summer_norm":10,"winter_norm":12,"summer_km":200,"winter_km":100,"capacity":100,"first_ref":"1","last_ref":"2","manual":[{"date":"2026-10-03","kind":"in","liters":10,"fuel_type":"Дизель","organization":"Фирма","confirmed":False},{"date":"2026-10-04","kind":"out","liters":5,"fuel_type":"Дизель","document":"Акт","confirmed":True}]}
        self.save("2026-10",p)
        r=writeoff.report(self.c,"2026-10")["rows"][0]["result"]
        self.assertEqual((r["start"],r["card"],r["actual"],r["norm"],r["difference"]),(20,35,50,32,18))
        self.assertEqual(r["unconfirmed"],10)
        self.assertEqual(writeoff.report(self.c,"2026-11")["rows"][0]["result"]["start"],10)
        with self.assertRaises(ValueError): self.save("2026-10",p)
        with self.assertRaises(ValueError): self.save("2026-10",{**p,"km_override":250},1)
        self.save("2026-10",{**p,"km_override":250,"km_reason":"Сломан одометр"},1)
        self.assertEqual(len(writeoff.report(self.c,"2026-10")["audit"]),2)
        self.assertIsNone(writeoff.report(self.c,"2026-10")["rows"][0]["result"]["norm"])
        wb=load_workbook(BytesIO(writeoff.export(self.c,"2026-10")))
        self.assertEqual(wb.active["J9"].value,50)
        self.assertEqual(wb.active["P9"].value,5)
        self.assertEqual(wb.active.page_setup.orientation,"landscape")
        self.assertEqual(wb["Операции вручную"].max_row,3)
        wb.close()
    def test_flags(self):
        result=writeoff.calculate({"start":90,"end":110,"capacity":100,"odo_first":200,"odo_last":100},0,"Дизель",50)
        self.assertEqual(result["actual"],-20)
        self.assertIsNone(result["km"])
        self.assertTrue(any("Отрицательный" in x for x in result["flags"]))
        self.assertTrue(any("вместимости" in x for x in result["flags"]))
    def test_archive_import(self):
        wb=Workbook();s=wb.active;s["A7"]="гос номер";s["E7"]="Пробег (км)";s["A9"]="А 123 АА";s["B9"]="Test";s["E9"]=123;s["F9"]=20;s["N9"]=10;s["G9"]=1;s["A10"]="ИТОГО"
        out=BytesIO();wb.save(out);blob=out.getvalue()
        result=writeoff.import_old(self.c,"2026-10","old.xlsx",blob)
        self.assertEqual((result["rows"],result["matched"]),(1,1))
        self.assertTrue(writeoff.import_old(self.c,"2026-10","again.xlsx",blob)["duplicate"])
        r=writeoff.report(self.c,"2026-10")
        self.assertEqual(r["sources"][0]["rows"][0]["values"][2],1)
        self.assertIsNone(r["rows"][0]["result"]["actual"])
