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
            for n,day,first,last in (("1",m+"-01",0,50),("2",m+"-02",50,100)):
                operations.save_waybill(self.c,{"vehicle_id":1,"number":n,"day":day,"odo_first":first,"odo_last":last})
            writeoff.save(self.c,{"month":m,"vehicle_id":1,"revision":0,"reason":"Тест","payload":{
                "start":start,"end":end,"capacity":100,"odo_first":0,"odo_last":100,"first_ref":"1","last_ref":"2",
                "season":"summer","summer_norm":10,"winter_norm":12,"fuel_reviewed":True,"fuel_review_reason":"Нет заправок по проверенной выписке"}})
        self.assertTrue(automation.readiness(self.c,months[0])["ready"])
        writeoff.month_action(self.c,months[0],"close","Проверено")
        self.assertTrue(writeoff.report(self.c,months[0])["closed"])
        with self.assertRaises(ValueError):writeoff.save(self.c,{"month":months[0],"vehicle_id":1,"revision":1,"reason":"Тест","payload":{}})
        change(self.c,"vehicles",{"id":1,"make":"Test","model":"Car","plate":"A123AA","fuel_type":"Дизель","summer":12,"winter":14,"employee_id":1})
        self.assertEqual(writeoff.report(self.c,months[0])["rows"][0]["fuel_type"],"Бензин")
        change(self.c,"vehicles",{"id":1,"make":"Test","model":"Car","plate":"A123AA","fuel_type":"Бензин","summer":10,"winter":12,"employee_id":1})
        writeoff.month_action(self.c,months[0],"reopen","Сверка")
        self.assertFalse(writeoff.report(self.c,months[0])["closed"])
        self.assertEqual(automation.forecast(self.c,target)["rows"][0]["forecast"],7.5)
        self.assertIsNone(automation.forecast(self.c,months[1])["rows"][0]["forecast"])
        book=load_workbook(BytesIO(automation.export_review(self.c,target)))
        self.assertIn("НЕ ГОТОВО",book["Проверка готовности"]["B1"].value)
        self.assertIn("черновик",book["Списание ГСМ"]["A6"].value);book.close()

    def test_simple_monthly_entry_uses_card_and_no_waybill_registration(self):
        month="2024-10"
        rows=[["Карта № 0000000000000000001"],
            ["Отгрузка","01.10.2024","12:00:00","АЗС","АИ-95",70,40,2800],
            ["Возврат","02.10.2024","12:00:00","АЗС","АИ-95",70,-5,-350],
            ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,20,1400]]
        fuel.ingest(self.c,"test.xlsx",workbook(rows))
        self.assertEqual(writeoff.report(self.c,month)["unassigned"],2)
        operations.set_seasons(self.c,{"summer":"04-01","winter":"11-01"})
        p={"input_mode":"monthly","start":20,"end":5,"odo_first":1000,"odo_last":1500,
           "fuel_reviewed":True,"fuel_review_reason":"Сверено с выпиской"}
        d={"month":month,"vehicle_id":1,"revision":0,"monthly_card_id":1,"reason":"Показания месяца","payload":p}
        writeoff.save(self.c,d)
        report=writeoff.report(self.c,month);r=report["rows"][0]
        self.assertEqual(report["unassigned"],0)
        self.assertEqual((r["result"]["card"],r["result"]["km"],r["result"]["actual"],r["result"]["norm"],r["result"]["difference"]),(35,500,50,50,0))
        self.assertEqual(r["card_details"][0]["number"],"0000000000000000001")
        self.assertEqual(writeoff.report(self.c,"2024-11")["unassigned"],1)
        self.assertTrue(automation.readiness(self.c,month)["ready"])
        writeoff.save(self.c,{**d,"revision":1})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM fuel_periods").fetchone()[0],1)
        change(self.c,"employees",{"last_name":"Другой","first_name":"Водитель"})
        change(self.c,"vehicles",{"make":"Test","model":"Car","plate":"A124AA","fuel_type":"Бензин","summer":10,"winter":12,"employee_id":2})
        with self.assertRaises(ValueError):writeoff.save(self.c,{**d,"vehicle_id":2})
        self.assertEqual(writeoff.report(self.c,month)["rows"][0]["result"]["card"],35)
        self.assertEqual(writeoff.stored(self.c,month,2)[1],0)

    def test_simple_missing_ending_balance_does_not_invent_actual(self):
        writeoff.save(self.c,{"month":"2024-10","vehicle_id":1,"revision":0,"reason":"Ввод","payload":{
            "input_mode":"monthly","start":10,"odo_first":100,"odo_last":200,"season":"summer"}})
        r=writeoff.report(self.c,"2024-10")["rows"][0]["result"]
        self.assertEqual(r["km"],100)
        self.assertEqual(r["norm"],10)
        self.assertIsNone(r["actual"])
        self.assertFalse(automation.readiness(self.c,"2024-10")["ready"])

    def test_show_card_totals_before_confirming_historical_link(self):
        month="2024-10"
        rows=[["Карта № 0000000000000000001"],
            ["Отгрузка","01.10.2024","12:00:00","АЗС","АИ-95",70,40,2800],
            ["Возврат","02.10.2024","12:00:00","АЗС","АИ-95",70,-5,-350],
            ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,20,1400]]
        fuel.ingest(self.c,"test.xlsx",workbook(rows))
        writeoff.save(self.c,{"month":month,"vehicle_id":1,"revision":0,"reason":"Ввод","payload":{
            "input_mode":"monthly","start":97,"end":115,"odo_first":400705,"odo_last":404505,"season":"summer"}})
        r=writeoff.report(self.c,month)
        self.assertEqual(r["fuel_months"],["2024-11","2024-10"])
        x=r["rows"][0]
        self.assertEqual(x["current_cards"][0]["liters"]["Бензин"],35)
        self.assertEqual(x["current_cards"][0]["pending"],2)
        self.assertEqual(x["result"]["km"],3800)
        self.assertIsNone(x["result"]["actual"])
        self.assertEqual(x["revision"],1)
        writeoff.confirm_month_card(self.c,{"month":month,"vehicle_id":1,"card_id":1,"reason":"Подтверждено"})
        x=writeoff.report(self.c,month)["rows"][0]
        self.assertEqual(x["result"]["card"],35)
        self.assertEqual(x["result"]["actual"],17)
        self.assertEqual(x["current_cards"][0]["pending"],0)
        self.assertEqual(x["revision"],1)
        self.assertEqual(writeoff.report(self.c,"2024-11")["unassigned"],1)
        self.assertEqual(writeoff.report(self.c,"2024-11")["rows"][0]["current_cards"][0]["liters"]["Бензин"],20)
        with self.assertRaises(ValueError):
            writeoff.confirm_month_card(self.c,{"month":month,"vehicle_id":1,"card_id":1,"reason":""})

    def test_month_workspace_independent_statement_and_readiness(self):
        # Synthetic November statement; December is a separate period.
        rows=[["Карта № 0000000000000000001"],
            ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800],
            ["Возврат","02.11.2024","12:00:00","АЗС","АИ-95",70,-5,-350]]
        fuel.ingest(self.c,"december-in-filename.xlsx",workbook(rows))
        w=automation.month_workspace(self.c,"2024-12")
        self.assertEqual(len(w["months"]),12)
        self.assertEqual(w["report"]["month"],"2024-12")
        self.assertTrue(w["months"][10]["statement_loaded"])
        self.assertEqual(w["months"][10]["operations"],2)
        self.assertEqual(w["months"][10]["unassigned"],2)
        self.assertFalse(w["months"][10]["ready"])
        self.assertFalse(w["months"][11]["statement_loaded"])
        self.assertEqual(w["months"][11]["operations"],0)
        self.assertFalse(w["readiness"]["ready"])
        writeoff.save(self.c,{"month":"2024-11","vehicle_id":1,"revision":0,"monthly_card_id":1,"reason":"Показания","payload":{
            "input_mode":"monthly","start":20,"end":5,"odo_first":1000,"odo_last":1500,
            "season":"summer","fuel_reviewed":True,"fuel_review_reason":"Выписка сверена"}})
        w=automation.month_workspace(self.c,"2024-12")
        self.assertTrue(w["months"][10]["ready"])
        self.assertEqual(w["months"][10]["unassigned"],0)
        writeoff.month_action(self.c,"2024-11","close","Проверено")
        w=automation.month_workspace(self.c,"2024-11")
        self.assertTrue(w["months"][10]["closed"])
        self.assertTrue(w["months"][10]["statement_loaded"])
        self.assertFalse(w["months"][11]["statement_loaded"])
        fuel.ingest(self.c,"dec.xlsx",workbook([["Карта № 0000000000000000001"],
            ["Отгрузка","01.12.2024","12:00:00","АЗС","АИ-95",70,20,1400]]))
        w=automation.month_workspace(self.c,"2024-12")
        self.assertTrue(w["months"][11]["statement_loaded"])
        self.assertFalse(w["months"][11]["ready"])
        fuel.cancel_import(self.c,{"id":2,"reason":"Ошибочная выписка"})
        self.assertFalse(automation.month_workspace(self.c,"2024-12")["months"][11]["statement_loaded"])
        self.assertTrue(automation.month_workspace(self.c,"2024-12")["months"][10]["closed"])
        nextyear=automation.month_workspace(self.c,"2025-01")
        self.assertEqual(nextyear["months"][0]["month"],"2025-01")
        self.assertFalse(any(m["statement_loaded"] for m in nextyear["months"]))

    def test_inline_batch_atomic_and_carry_forward(self):
        writeoff.save(self.c,{"month":"2024-10","vehicle_id":1,"revision":0,"reason":"Октябрь","payload":{
            "input_mode":"monthly","start":20,"end":15,"odo_first":1000,"odo_last":1500,"season":"summer",
            "fuel_reviewed":True,"fuel_review_reason":"Нет заправок"}})
        r=writeoff.report(self.c,"2024-11")["rows"][0]
        self.assertEqual(r["previous"],15)
        self.assertEqual(r["previous_odo"],1500)
        entry={"vehicle_id":1,"revision":0,"values":{"start":"15","odo_first":"1500","odo_last":"1600","end":"5","season":"summer"}}
        with self.assertRaises(ValueError):
            writeoff.save_batch(self.c,{"month":"2024-11","entries":[entry,{"vehicle_id":999,"revision":0,"values":{"start":"1"}}]})
        self.assertEqual(writeoff.stored(self.c,"2024-11",1)[1],0)
        self.assertEqual(writeoff.save_batch(self.c,{"month":"2024-11","entries":[entry]})["saved"],1)
        r=writeoff.report(self.c,"2024-11")["rows"][0]
        self.assertEqual(r["result"]["km"],100)
        self.assertEqual(r["result"]["norm"],10)
        self.assertIsNone(r["result"]["actual"])
        with self.assertRaises(ValueError):
            writeoff.save_batch(self.c,{"month":"2024-11","entries":[entry]})
        self.assertEqual(writeoff.stored(self.c,"2024-11",1)[1],1)
        with self.assertRaises(ValueError):
            writeoff.save_batch(self.c,{"month":"2024-11","entries":[{"vehicle_id":1,"revision":1,"values":{"card":999}}]})
        self.assertEqual(writeoff.stored(self.c,"2024-11",1)[0]["end"],5)

    def test_vehicle_fuel_history_separate_months_and_frozen_snapshot(self):
        rows=[["Карта № 0000000000000000001"],
            ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800],
            ["Возврат","02.11.2024","12:00:00","АЗС","АИ-95",70,-5,-350],
            ["Отгрузка","01.12.2024","12:00:00","АЗС","АИ-95",70,20,1400]]
        fuel.ingest(self.c,"history.xlsx",workbook(rows))
        self.assertEqual(writeoff.report(self.c,"2024-11")["rows"][0]["fuel_history"],[])
        writeoff.save(self.c,{"month":"2024-11","vehicle_id":1,"revision":0,"monthly_card_id":1,"reason":"Сверка","payload":{
            "input_mode":"monthly","start":20,"end":5,"odo_first":1000,"odo_last":1500,
            "season":"summer","capacity":60,"fuel_reviewed":True,"fuel_review_reason":"Выписка сверена"}})
        r=writeoff.report(self.c,"2024-11")["rows"][0]
        self.assertEqual(len(r["fuel_history"]),2)
        self.assertEqual([x["quantity"] for x in r["fuel_history"]],[4000,-500])
        self.assertEqual(sum(x["amount"] for x in r["fuel_history"]),245000)
        self.assertEqual(r["result"]["card"],35)
        self.assertEqual(r["payload"]["capacity"],60)
        self.assertEqual(writeoff.report(self.c,"2024-12")["rows"][0]["fuel_history"],[])
        writeoff.month_action(self.c,"2024-11","close","Проверено")
        closed=writeoff.report(self.c,"2024-11")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["rows"][0]["fuel_history"],r["fuel_history"])
        self.assertEqual(closed["rows"][0]["payload"]["capacity"],60)

    def test_flat_card_statement_and_explicit_control_total(self):
        header=["Номер карты","Операция","Дата","Время","АЗС","Топливо","Цена","Литры","Сумма"]
        card="0000000000000000001"
        rows=[header,[card,"Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800],
              [card,"Возврат","02.11.2024","12:00:00","АЗС","АИ-95",70,-5,-350],
              ["","Итого по выписке нетто","","","","","",35,2450]]
        for name in ("flat.xls","flat.xlsx"):
            blob=workbook(rows,name.endswith(".xls"))
            p=automation.preview(self.c,name,blob)
            self.assertTrue(p["control_complete"])
            self.assertEqual(p["months"][0]["liters"],35)
            self.assertEqual(p["amount_errors"],0)
            self.assertEqual(p["cards"][0]["number"],card)
        rows[-1][-1]=2451
        with self.assertRaises(ValueError):automation.preview(self.c,"bad.xlsx",workbook(rows))
        with self.assertRaises(ValueError):fuel.ingest(self.c,"bad.xlsx",workbook(rows))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM fuel_operations").fetchone()[0],0)
        rows[1][0]=12345
        with self.assertRaises(ValueError):automation.preview(self.c,"numeric.xlsx",workbook(rows))

    def test_vehicle_analysis_purchase_cost_and_missing_history(self):
        rows=[["Карта № 0000000000000000001"],
              ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800],
              ["Возврат","02.11.2024","12:00:00","АЗС","АИ-95",70,-5,-350]]
        fuel.ingest(self.c,"purchases.xlsx",workbook(rows))
        x=automation.vehicle_analysis(self.c,"2024-11")["rows"][0]
        self.assertIsNone(x["purchase_amount"])
        writeoff.save(self.c,{"month":"2024-11","vehicle_id":1,"revision":0,"monthly_card_id":1,"reason":"Показания","payload":{
            "input_mode":"monthly","start":20,"end":5,"odo_first":1000,"odo_last":1500,
            "season":"summer","capacity":60,"fuel_reviewed":True,"fuel_review_reason":"Выписка сверена"}})
        x=automation.vehicle_analysis(self.c,"2024-11")["rows"][0]
        self.assertEqual(x["purchase_amount"],2450)
        self.assertEqual(x["purchase_per_km"],4.9)
        self.assertEqual(x["rate"],10)
        self.assertIsNone(x["annual_rate"])
        writeoff.month_action(self.c,"2024-11","close","Проверено")
        x=automation.vehicle_analysis(self.c,"2024-11")["rows"][0]
        self.assertEqual(x["annual_rate"],10)
        self.assertEqual(x["annual_months"],1)

    def test_incoming_scan_is_read_only(self):
        import maintenance
        db=Path(self.tmp.name)/"test.sqlite"
        root=db.parent/"incoming";root.mkdir()
        blob=workbook([["Карта № 0000000000000000001"],["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800]])
        (root/"valid.xlsx").write_bytes(blob)
        (root/"bad.xlsx").write_bytes(b"not excel")
        cache={}
        r=maintenance.inspect_inbox(db,cache)
        self.assertTrue(r["integrity_ok"])
        self.assertEqual({x["name"]:x["state"] for x in r["rows"]},{"valid.xlsx":"ready","bad.xlsx":"review"})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM fuel_operations").fetchone()[0],0)
        fuel.ingest(self.c,"valid.xlsx",blob)
        r=maintenance.inspect_inbox(db,cache)
        self.assertEqual(next(x["state"] for x in r["rows"] if x["name"]=="valid.xlsx"),"imported")
        self.assertEqual((root/"valid.xlsx").read_bytes(),blob)

    def test_vehicle_series_uses_closed_snapshots_only(self):
        result=automation.vehicle_series(self.c,"2024-11",1)
        self.assertEqual(len(result["rows"]),12)
        self.assertEqual(result["rows"][0]["month"],"2023-12")
        self.assertEqual(result["rows"][-1]["month"],"2024-11")
        self.assertTrue(all(x["actual"] is None for x in result["rows"]))
        rows=[["Карта № 0000000000000000001"],
              ["Отгрузка","01.11.2024","12:00:00","АЗС","АИ-95",70,40,2800]]
        fuel.ingest(self.c,"series.xlsx",workbook(rows))
        writeoff.save(self.c,{"month":"2024-11","vehicle_id":1,"revision":0,"monthly_card_id":1,"reason":"Показания","payload":{
            "input_mode":"monthly","start":20,"end":10,"odo_first":1000,"odo_last":1500,
            "season":"summer","capacity":60,"fuel_reviewed":True,"fuel_review_reason":"Выписка сверена"}})
        self.assertIsNone(automation.vehicle_series(self.c,"2024-11",1)["rows"][-1]["actual"])
        writeoff.month_action(self.c,"2024-11","close","Проверено")
        last=automation.vehicle_series(self.c,"2024-11",1)["rows"][-1]
        self.assertEqual((last["actual"],last["norm"],last["km"]),(50,50,500))
        self.assertIsNone(automation.vehicle_series(self.c,"2024-10",1)["rows"][-1]["actual"])
