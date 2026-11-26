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

    def test_cancel_and_restore_last_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"test.db";initialize(path)
            with closing(connect(path)) as c:
                blob=fixture("xlsx")
                fuel.ingest(c,"one.xlsx",blob)
                fuel.ingest(c,"two.xls",fixture("xls"))
                with self.assertRaises(ValueError):fuel.cancel_import(c,{"id":1,"reason":"test"})
                fuel.cancel_import(c,{"id":2,"reason":"test"})
                self.assertEqual(len(fuel.report(c)["rows"]),2)
                fuel.cancel_import(c,{"id":1,"reason":"test"})
                self.assertEqual(len(fuel.report(c)["rows"]),0)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM fuel_imports").fetchone()[0],2)
                self.assertEqual(fuel.ingest(c,"one.xlsx",blob)["inserted"],2)
                self.assertEqual(len(fuel.report(c)["rows"]),2)

    def test_edit_and_delete_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"test.db";initialize(path)
            with closing(connect(path)) as c:
                fuel.ingest(c,"one.xlsx",fixture("xlsx"))
                change(c,"employees",{"first_name":"Тест","last_name":"Тест"})
                change(c,"vehicles",{"make":"Test","model":"A","plate":"A123AA","fuel_type":"Бензин","employee_id":1})
                d={"card_id":1,"employee_id":1,"vehicle_id":1,"start":"2026-10-01","end":"2026-10-31"}
                fuel.add_period(c,d)
                fuel.add_period(c,{**d,"id":1,"start":"2026-10-02","reason":"Сверка"})
                self.assertEqual(fuel.report(c,"2026-10")["rows"][0]["vehicle_id"],None)
                fuel.add_period(c,{"id":1,"action":"delete","reason":"Сверка"})
                self.assertEqual(c.execute("SELECT COUNT(*) FROM fuel_periods").fetchone()[0],0)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM fuel_corrections").fetchone()[0],2)

class LegacyEncodingTests(unittest.TestCase):
    def test_missing_codepage_reopens_with_windows_1251(self):
        from unittest.mock import Mock
        library=Mock()
        first=Mock(biff_version=50,codepage=None)
        repaired=Mock(biff_version=50,codepage=None)
        library.open_workbook.side_effect=[first,repaired]
        self.assertIs(fuel.open_legacy_xls(library,b"legacy"),repaired)
        first.release_resources.assert_called_once()
        self.assertEqual(library.open_workbook.call_args.kwargs["encoding_override"],"cp1251")

    def test_explicit_codepage_and_unicode_are_preserved(self):
        from unittest.mock import Mock
        for version,codepage in ((50,1251),(50,1252),(80,1200),(80,None)):
            with self.subTest(version=version,codepage=codepage):
                library=Mock()
                book=Mock(biff_version=version,codepage=codepage)
                library.open_workbook.return_value=book
                self.assertIs(fuel.open_legacy_xls(library,b"book"),book)
                library.open_workbook.assert_called_once()
                self.assertNotIn("encoding_override",library.open_workbook.call_args.kwargs)
                book.release_resources.assert_not_called()

    def test_real_legacy_biff5_without_codepage(self):
        import struct
        from xlwt.CompoundDoc import XlsDoc
        def record(kind,data=b""):
            return struct.pack("<HH",kind,len(data))+data
        def bof(kind):
            return record(0x0809,struct.pack("<HHHH",0x0500,kind,0x540A,0x07CD))
        def label(row,col,value):
            raw=str(value).encode("cp1251")
            return record(0x0204,struct.pack("<HHHH",row,col,0,len(raw))+raw)
        sheetname=b"Data"
        def bounds(offset):
            return record(0x0085,struct.pack("<IBBB",offset,0,0,len(sheetname))+sheetname)
        globals_start=bof(0x0005)+record(0x0022,b"\x00\x00")
        offset=len(globals_start)+len(bounds(0))+len(record(0x000A))
        globals_stream=globals_start+bounds(offset)+record(0x000A)
        sheet=bof(0x0010)
        for i,row in enumerate(ROWS):
            for j,value in enumerate(row):sheet+=label(i,j,value)
        sheet+=record(0x000A)
        out=BytesIO()
        XlsDoc().save(out,globals_stream+sheet)
        rows=fuel.parse_file("legacy.xls",out.getvalue())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0][0],"0000000000000000001")
        self.assertEqual(rows[0][2],"отгрузка")
        self.assertEqual(rows[0][4],"Бензин АИ-95")
        self.assertEqual(sum(row[7] for row in rows),3800)
