from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from server import initialize, connect, change, snapshot

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"data.sqlite"
        initialize(self.path)
        self.c = connect(self.path)
        change(self.c,"employees",{"last_name":"Иванов","first_name":"Иван"})
    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()
    def test_links_archive_restore(self):
        change(self.c,"vehicles",{"make":"Toyota","model":"LC","plate":"a123aa125","employee_id":1,"summer":12,"winter":15})
        change(self.c,"cards",{"number":"00001","employee_id":1})
        s=snapshot(self.c)
        self.assertEqual(s["vehicles"][0]["plate"],"А123АА125")
        self.assertEqual(s["cards"][0]["number"],"00001")
        change(self.c,"employees",{"id":1,"action":"archive"})
        s=snapshot(self.c)
        self.assertIsNone(s["vehicles"][0]["employee_id"])
        self.assertIsNone(s["cards"][0]["employee_id"])
        with self.assertRaises(ValueError):
            change(self.c,"cards",{"number":"2","employee_id":1})
        change(self.c,"employees",{"id":1,"action":"restore"})
        change(self.c,"cards",{"id":1,"number":"00001","employee_id":1})
        self.assertEqual(snapshot(self.c)["cards"][0]["employee_id"],1)
    def test_duplicate_assignment_rolls_back(self):
        change(self.c,"cards",{"number":"001","employee_id":1})
        with self.assertRaises(sqlite3.IntegrityError):
            change(self.c,"cards",{"number":"002","employee_id":1})
        self.assertEqual(len(snapshot(self.c)["cards"]),1)
    def test_invalid_dates_and_norms(self):
        base={"make":"Toyota","model":"LC","plate":"x123xx"}
        for extra in ({"summer":-1},{"winter":"nan"},{"insurance_start":"2027-01-01","insurance_end":"2026-01-01"}):
            with self.assertRaises(ValueError):
                change(self.c,"vehicles",{**base,**extra})
        self.assertEqual(snapshot(self.c)["vehicles"],[])
    def test_edit_and_persistence(self):
        change(self.c,"employees",{"id":1,"last_name":"Петров","first_name":"Иван"})
        with closing(connect(self.path)) as c:
            self.assertEqual(snapshot(c)["employees"][0]["last_name"],"Петров")
