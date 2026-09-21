"""Synthetic, offline regression coverage for page identity and output order."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'kb.py'
spec = importlib.util.spec_from_file_location('kb_page_order_under_test', SOURCE)
kb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb)


class PageOrderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'synthetic.db'
        self.con = sqlite3.connect(self.db)
        self.addCleanup(self.con.close)
        self.con.executescript(kb.SCHEMA)
        for sid in (1, 2):
            self.con.execute("INSERT INTO sources(source_id,collection,root_path,rel_path,depth,kind,status,page_count) VALUES(?,'fixture','none',?,0,'pdf','indexed',3)", (sid, f'{sid}.pdf'))
        # Arrival order intentionally differs from document/page order.
        for sid, page in [(2, 3), (1, 2), (2, 1), (1, 3), (1, 1), (2, 2)]:
            self.con.execute("INSERT INTO units(source_id,unit_type,unit_no,method,text) VALUES(?,'page',?,'synthetic','needle common')", (sid, page))
        for table in ('units_fts', 'units_tri'):
            self.con.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        self.con.commit()

    def call(self, func, **kwargs):
        # Use the production command, with a managed connection to our own fixture.
        from unittest.mock import patch
        out = io.StringIO()
        with patch.object(kb, 'connect', return_value=self.con), contextlib.redirect_stdout(out):
            func(SimpleNamespace(db=str(self.db), **kwargs))
        return out.getvalue()

    def test_export_groups_sources_then_numeric_pages(self):
        out = Path(self.temp.name) / 'export.jsonl'
        before = self.con.execute('SELECT * FROM units ORDER BY unit_id').fetchall()
        self.call(kb.cmd_export, out=str(out))
        rows = [json.loads(s) for s in out.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['source'], r['unit_no']) for r in rows],
                         [(f'fixture:{sid}.pdf', n) for sid in (1, 2) for n in (1, 2, 3)])
        self.assertEqual(len({r['unit_id'] for r in rows}), 6)
        self.assertEqual(self.con.execute('SELECT * FROM units ORDER BY unit_id').fetchall(), before)

    def test_equal_relevance_has_source_page_tiebreakers(self):
        expected = [f'[{sid}] fixture:{sid}.pdf · page {n} · synthetic'
                    for sid in (1, 2) for n in (1, 2, 3)]
        for substring in (False, True):
            with self.subTest(substring=substring):
                out = self.call(kb.cmd_search, query='needle', substring=substring, collection=None, limit=20)
                self.assertEqual([s for s in out.splitlines() if s.startswith('[')], expected)

    def test_relevance_still_precedes_reading_order(self):
        self.con.execute("UPDATE units SET text='needle ' || ? WHERE source_id=1", ('filler ' * 100,))
        for table in ('units_fts', 'units_tri'):
            self.con.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        self.con.commit()
        out = self.call(kb.cmd_search, query='needle', substring=False, collection=None, limit=20)
        self.assertTrue(out.splitlines()[0].startswith('[2] fixture:2.pdf'), out)

    def test_numeric_order_is_not_lexical_order(self):
        self.con.execute('DELETE FROM units')
        for n in (10, 2, 1):
            self.con.execute("INSERT INTO units(source_id,unit_type,unit_no,method,text) VALUES(1,'page',?,'synthetic','needle common')", (n,))
        for table in ('units_fts', 'units_tri'):
            self.con.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        out = Path(self.temp.name) / 'numeric.jsonl'
        self.call(kb.cmd_export, out=str(out))
        self.assertEqual([json.loads(s)['unit_no'] for s in out.read_text(encoding='utf-8').splitlines()], [1, 2, 10])
        for substring in (False, True):
            text = self.call(kb.cmd_search, query='needle', substring=substring, collection=None, limit=20)
            headers = [s for s in text.splitlines() if s.startswith('[')]
            self.assertEqual(headers, [f'[1] fixture:1.pdf · page {n} · synthetic' for n in (1, 2, 10)])

    def test_s4_rejects_invalid_ordinals_and_accepts_distinct_sources(self):
        # Extract the exact production predicate without invoking verify's report writes.
        tree = ast.parse(SOURCE.read_bytes())
        verify = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'verify')
        query = next(ast.literal_eval(n.value.args[0]) for n in ast.walk(verify)
                     if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'mism' for t in n.targets))
        self.assertEqual(self.con.execute(query).fetchall(), [])
        for pages in ([1, 1, 3], [1, 3], [0, 2, 3], [1, 2, 4], [1, 2, 2.5]):
            with self.subTest(pages=pages):
                self.con.execute('SAVEPOINT case_fixture')
                self.con.execute('DELETE FROM units WHERE source_id=1')
                for n in pages:
                    self.con.execute("INSERT INTO units(source_id,unit_type,unit_no,text) VALUES(1,'page',?,'synthetic')", (n,))
                self.assertEqual([r[0] for r in self.con.execute(query)], [1])
                self.con.execute('ROLLBACK TO case_fixture')
                self.con.execute('RELEASE case_fixture')


if __name__ == '__main__':
    unittest.main()
