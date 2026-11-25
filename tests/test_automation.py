import tempfile
import unittest
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import xlwt
from openpyxl import Workbook, load_workbook
from server import initialize, connect, change
import automation
import fuel
import operations
import writeoff

def workbook(rows,xls=False):
    out=BytesIO()
    if xls:
        book=xlwt.Workbook();s=book.add_sheet("Data")
        for i,row in enumerate(rows):
            for j,v in enumerate(row):s.write(i,j,v)
        book.save(out)
    else:
        book=Workbook()
        for row in rows:book.active.append(row)
        book.save(out);book.close()
    return out.getvalue()

class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        path=Path(self.tmp.name)/"test.sqlite"
        initialize(path);self.c=connect(path)
        change(self.c,"employees",{"last_name":"Тест","first_name":"Водитель"})
        change(self.c,"vehicles",{"make":"Test","model":"Car","plate":"A123AA","fuel_type":"Бензин","summer":10,"winter":12,"employee_id":1})
        change(self.c,"cards",{"number":"0000000000000000001","employee_id":1})
    def tearDown(self):
        self.c.close();self.tmp.cleanup()

    def test_preview_multimonth_legacy_dedup(self):
        rows=[["Карта № 0000000000000000001"],["Отгрузка","01.10.2026","12:00:00","АЗС","АИ-95",70,40,2800],
          ["Возврат","02.10.2026","12:00:00","АЗС","АИ-95",70,-5,-350],
          ["Карта № 0000000000000000002"],["Отгрузка","01.11.2026","12:00:00","АЗС","ДТ зимнее",80,10,800]]
        fuel.add_period(self.c,{"card_id":1,"employee_id":1,"vehicle_id":1,"start":"2026-10-01","end":"2026-10-31"})
        blob=workbook(rows)
        p=automation.preview(self.c,"test.xlsx",blob)
        self.assertEqual((p["operations"],p["new_cards"],p["unassigned"]),(3,1,1))
        self.assertEqual(p["months"][0]["liters"],35)
        self.assertEqual(p["cards"][0]["number"],"0000000000000000001")
        self.assertEqual(p["products"][0]["category"],"Бензин")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM fuel_operations").fetchone()[0],0)
        row=fuel.parse_file("test.xlsx",blob)[0]
        old=(*row[:5],"Другое",*row[6:])
        self.assertEqual(fuel.signature(row),sha256(json.dumps(old,ensure_ascii=False,separators=(",",":")).encode()).hexdigest())
        self.assertEqual(fuel.ingest(self.c,"test.xlsx",blob)["inserted"],3)
        other=workbook(rows,xls=True)
        self.assertEqual(automation.preview(self.c,"test.xls",other)["duplicates"],3)
        self.assertEqual(fuel.ingest(self.c,"test.xls",other)["skipped"],3)
        self.assertEqual(fuel.report(self.c,"2026-10")["rows"][0]["category"],"Бензин")
        self.assertEqual(automation.price_trend(self.c,"2026-10")["rows"][0]["current"],70)
        with self.assertRaises(ValueError):automation.preview(self.c,"test.xls",blob)

    def test_mileage_proposal_does_not_change(self):
        for n,day,first,last in (("1","2026-10-01",10,110),("2","2026-10-02",210,260)):
            operations.save_waybill(self.c,{"vehicle_id":1,"employee_id":1,"number":n,"day":day,"odo_first":first,"odo_last":last})
        p=automation.mileage_proposal(self.c,"2026-10",1,{})
        self.assertEqual(p["km"],150)
        self.assertIn("разрывы",p["message"])
        self.assertEqual(writeoff.stored(self.c,"2026-10",1)[1],0)
        operations.save_waybill(self.c,{"vehicle_id":1,"number":"3","day":"2026-10-03","odo_first":200,"odo_last":100})
        self.assertIsNone(automation.mileage_proposal(self.c,"2026-10",1,{})["km"])

    def test_readiness_forecast_and_export(self):
        target=date.today().strftime("%Y-%m")
        months=list(automation.history_months(target,2))
        self.assertFalse(automation.readiness(self.c,months[0])["ready"])
        for m,start,end in ((months[1],20,10),(months[0],10,5)):
            writeoff.save(self.c,{"month":m,"vehicle_id":1,"revision":0,"reason":"Тест","payload":{
                "start":start,"end":end,"capacity":100,"odo_first":0,"odo_last":100,"first_ref":"1","last_ref":"2",
                "season":"summer","summer_norm":10,"winter_norm":12}})
        self.assertTrue(automation.readiness(self.c,months[0])["ready"])
        self.assertEqual(automation.forecast(self.c,target)["rows"][0]["forecast"],7.5)
        self.assertIsNone(automation.forecast(self.c,months[1])["rows"][0]["forecast"])
        book=load_workbook(BytesIO(automation.export_review(self.c,target)))
        self.assertIn("НЕ ГОТОВО",book["Проверка готовности"]["B1"].value)
        self.assertIn("черновик",book["Списание ГСМ"]["A6"].value);book.close()
