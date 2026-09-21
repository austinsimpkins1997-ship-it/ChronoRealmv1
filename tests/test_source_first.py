"""Source-first parsing: no inferred intent, and transformations stay labeled."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from test_page_order import kb


class SourceFirstTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        self.con.executescript(kb.SCHEMA)
        self.con.execute("INSERT INTO sources(source_id,collection,root_path,rel_path,depth,kind,status) VALUES(1,'fixture','none','input',0,'text','pending')")
        self.builder = kb.Builder.__new__(kb.Builder)
        self.builder.con = self.con
        self.builder.a = SimpleNamespace(no_ocr=True)
        self.builder.ocr_jobs = []

    def test_unicode_letters_are_not_empty(self):
        self.assertEqual(kb.alnum('κόσμος世界１２'), 10)
        self.assertEqual(kb.alnum('  !?\n'), 0)

    def test_literal_replacement_character_is_not_decode_evidence(self):
        raw = 'may not happen\n� explicitly present'.encode('utf-8')
        from unittest.mock import mock_open
        with patch('builtins.open', mock_open(read_data=raw)):
            self.builder.do_text(1, 'synthetic.txt')
        self.assertEqual(self.con.execute('SELECT text FROM units').fetchone()[0], raw.decode('utf-8'))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM flags WHERE flag='decode_replacement'").fetchone()[0], 0)

    def test_invalid_utf8_is_explicitly_flagged(self):
        from unittest.mock import mock_open
        with patch('builtins.open', mock_open(read_data=b'not certain\xff')):
            self.builder.do_text(1, 'synthetic.txt')
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM flags WHERE flag='decode_replacement'").fetchone()[0], 1)
        self.assertEqual(self.con.execute('SELECT text FROM units').fetchone()[0], 'not certain�')

    def test_chunk_roundtrip_preserves_literal_ambiguity(self):
        text = ('may decline; do not infer certainty\n' * 200) + '\n  unresolved?  '
        self.builder.store_text(1, text)
        rows = self.con.execute('SELECT char_offset,text FROM units ORDER BY unit_no').fetchall()
        self.assertEqual(''.join(r[1] for r in rows), text)
        for offset, value in rows:
            self.assertEqual(text[offset:offset + len(value)], value)

    def test_office_output_has_extraction_method(self):
        result = SimpleNamespace(returncode=0, stdout=b'perhaps, not proven', stderr=b'')
        with patch.object(kb.shutil, 'which', return_value='synthetic-extractor'), patch.object(kb.subprocess, 'run', return_value=result):
            self.builder.do_office(1, 'synthetic.docx')
        self.assertEqual(self.con.execute('SELECT method,text FROM units').fetchone(), ('office-extracted', 'perhaps, not proven'))

    def test_failed_pdf_text_is_not_indexed_as_success(self):
        info = SimpleNamespace(returncode=0, stdout='Pages: 1\n', stderr='')
        failed = SimpleNamespace(returncode=1, stdout=b'partial text', stderr=b'extraction failed')
        with patch.object(kb.subprocess, 'run', side_effect=[info, failed]):
            with self.assertRaisesRegex(RuntimeError, 'pdftotext'):
                self.builder.do_pdf(1, 'synthetic.pdf', 'synthetic-hash')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)

    def test_failed_fallback_page_is_not_accepted(self):
        info = SimpleNamespace(returncode=0, stdout='Pages: 2\n', stderr='')
        split = SimpleNamespace(returncode=0, stdout=b'no form feed', stderr=b'')
        good = SimpleNamespace(returncode=0, stdout=b'first\f', stderr=b'')
        bad = SimpleNamespace(returncode=1, stdout=b'partial', stderr=b'page failed')
        with patch.object(kb.subprocess, 'run', side_effect=[info, split, good, bad]):
            with self.assertRaisesRegex(RuntimeError, 'pdftotext'):
                self.builder.do_pdf(1, 'synthetic.pdf', 'synthetic-hash')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)

    def test_successful_unicode_pdf_preserves_text_without_unneeded_ocr(self):
        text = 'κόσμος世界 ' * 60
        self.builder.a.no_ocr = False
        results = [SimpleNamespace(returncode=0, stdout='Pages: 1\n', stderr=''),
                   SimpleNamespace(returncode=0, stdout=(text + '\f').encode('utf-8'), stderr=b'')]
        with patch.object(kb.subprocess, 'run', side_effect=results):
            self.builder.do_pdf(1, 'synthetic.pdf', 'synthetic-hash')
        self.assertEqual(self.con.execute('SELECT unit_no,text_native,ocr_needed FROM units').fetchall(), [(1, text, 0)])
        self.assertEqual(self.builder.ocr_jobs, [])

    def test_fallback_removes_only_terminal_page_separator(self):
        results = [SimpleNamespace(returncode=0, stdout='Pages: 1\n', stderr=''),
                   SimpleNamespace(returncode=0, stdout=b'a\fb\fc', stderr=b''),
                   SimpleNamespace(returncode=0, stdout=b'a\fb\f', stderr=b'')]
        with patch.object(kb.subprocess, 'run', side_effect=results):
            self.builder.do_pdf(1, 'synthetic.pdf', 'synthetic-hash')
        self.assertEqual(self.con.execute('SELECT text FROM units').fetchone()[0], 'a\fb')


if __name__ == '__main__':
    unittest.main()
