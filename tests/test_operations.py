import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from server import initialize, connect, change
import operations
import maintenance
import writeoff
import fuel

class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"ogm.sqlite3"
        initialize(self.path)
        self.c=connect(self.path)
        change(self.c,"employees",{"last_name":"Тест","first_name":"Водитель","department":"Гараж"})
        change(self.c,"vehicles",{"make":"Test","model":"A","plate":"A123AA","employee_id":1,"fuel_type":"Дизель","summer":10,"winter":12,"insurance_end":"2027-01-01"})
    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()
    def test_waybill_suggestion_and_seasons(self):
        operations.set_seasons(self.c,{"summer":"04-01","winter":"11-01"})
        for n,day,start,end,initial,final in (("1","2026-10-01",100,200,40,30),("2","2026-10-31",200,400,30,10)):
            operations.save_waybill(self.c,{"vehicle_id":1,"employee_id":1,"number":n,"day":day,"odo_first":start,"odo_last":end,"fuel_first":initial,"fuel_last":final})
        s=operations.suggestion(self.c,"2026-10",1,{})
        self.assertEqual(s["fields"]["start"],40)
        self.assertEqual(s["fields"]["end"],10)
        self.assertEqual(s["fields"]["odo_last"],400)
        self.assertEqual(s["fields"]["season"],"summer")
        self.assertNotIn("start",operations.suggestion(self.c,"2026-10",1,{"start":55})["fields"])
        self.assertEqual(operations.season_on("2026-12-01",operations.season_config(self.c)),"winter")
        self.assertEqual(operations.analytics(self.c,"2026")["months"][9]["vehicles"],1)
    def test_history_and_diagnostics(self):
        change(self.c,"vehicles",{"id":1,"make":"Test","model":"A","plate":"A123AA","fuel_type":"Дизель","summer":11,"winter":13,"status":"Ремонт"})
        r=operations.vehicle_detail(self.c,1)
        self.assertEqual(len(r["history"]),2)
        self.assertEqual(r["history"][0]["status"],"Ремонт")
        self.assertEqual(r["history"][1]["employee_id"],1)
        change(self.c,"employees",{"last_name":"ТЕСТ","first_name":"водитель"})
        d=operations.diagnostics(self.c)
        self.assertEqual(d["sqlite"],["ok"])
        self.assertEqual(d["foreign_keys"],[])
        self.assertTrue(d["duplicates"]["employees"])
        self.assertGreater(d["audit_count"],0)
    def test_backup_restore(self):
        self.c.close()
        with maintenance.instance_lock(self.path.parent):
            backup=maintenance.backup(self.path)
            with closing(connect(self.path)) as c:
                change(c,"employees",{"last_name":"После","first_name":"Копии"})
            maintenance.restore(self.path,backup.name)
        self.c=connect(self.path)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM employees").fetchone()[0],1)
        self.assertTrue(list((self.path.parent/"backups").glob("*before-restore.sqlite3")))
    def test_insurance_and_empty_annual(self):
        self.assertEqual(operations.insurance_calendar(self.c)[0]["insurance_end"],"2027-01-01")
        self.assertTrue(all(r["actual"] is None for r in operations.analytics(self.c,"2026")["months"]))
