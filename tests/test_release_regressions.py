# Copyright (C) 2026 Austin Simpkins and Chandler Lee Middlebrooks
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch

from test_kb import KbFixture, KB, FILLER, sha256

spec = importlib.util.spec_from_file_location("kb_release", KB)
kb_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb_release)


class ReleaseRegressionTest(KbFixture):
    def change(self, *statements):
        with closing(sqlite3.connect(self.db)) as con:
            for sql in statements:
                con.execute(sql)
            con.commit()

    def add_note(self, *extra):
        r = self.kb("note", "add", "--source-id", "1", "--body", "original human note", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_or_malformed_root_never_replaces_database(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        before = sha256(self.db)
        for root in ("docs=" + os.path.join(self.tmp, "absent"), "malformed"):
            r = self.kb("build", "--root", root, "--no-ocr")
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(sha256(self.db), before)

    def test_empty_root_requires_explicit_choice(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        before = sha256(self.db)
        os.remove(os.path.join(self.docs, "a.txt"))
        self.assertNotEqual(self.build("--no-ocr", ok=False).returncode, 0)
        self.assertEqual(sha256(self.db), before)
        self.build("--no-ocr", "--allow-empty")
        self.assertEqual(self.q("SELECT COUNT(*) FROM sources"), [(0,)])

    def test_default_output_parent_does_not_exclude_docs(self):
        self.write("a.txt", FILLER)
        self.write("nested/b.txt", "second distinct document " + FILLER)
        self.db = os.path.join(self.tmp, "knowledge.db")
        r = self.kb("build", "--root", "docs=" + self.docs, "--no-ocr")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM sources"), [(2,)])
        self.assertEqual(self.kb("verify").returncode, 0)

    def test_precise_exclusions_do_not_hide_sibling_prefix(self):
        self.write("outdoors/a.txt", FILLER)
        self.write("b.txt", "another " + FILLER)
        self.build("--no-ocr", "--exclude-under", os.path.join(self.docs, "out"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM sources"), [(2,)])
        r = self.build("--no-ocr", "--exclude-under", self.docs, ok=False)
        self.assertNotEqual(r.returncode, 0)

    def test_notes_survive_new_numeric_source_ids(self):
        self.write("z.txt", FILLER)
        self.build("--no-ocr")
        self.add_note("--unit", "1")
        before = self.q("SELECT annotation_id,body,source_binding_json,unit_binding_json FROM annotations")
        self.write("a.txt", "new earlier file " + FILLER)
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT annotation_id,body,source_binding_json,unit_binding_json FROM annotations"), before)
        self.assertEqual(self.q("SELECT a.binding_status,s.rel_path FROM annotations a JOIN sources s USING(source_id)"), [("BOUND", "z.txt")])

    def test_changed_source_retains_visible_unresolved_note(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.add_note()
        before = self.q("SELECT source_binding_json FROM annotations")
        self.write("a.txt", "changed bytes " + FILLER)
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT source_id,binding_status FROM annotations"), [(None, "SOURCE_UNRESOLVED")])
        self.assertEqual(self.q("SELECT source_binding_json FROM annotations"), before)
        self.assertIn("original human note", self.kb("note", "list").stdout)
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT source_binding_json FROM annotations"), before)

    def test_changed_extraction_does_not_rebind_unit_note(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.change("UPDATE units SET text='earlier extraction interpretation'")
        self.add_note("--unit", "1")
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT unit_id,binding_status FROM annotations"), [(None, "UNIT_UNRESOLVED")])

    def test_legacy_annotation_schema_migrates_on_rebuild(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.change("DROP TABLE annotations",
                    "CREATE TABLE annotations(annotation_id INTEGER PRIMARY KEY,source_id INTEGER,unit_id INTEGER,author TEXT,created_utc TEXT,kind TEXT,evidence_class TEXT,body TEXT,basis TEXT)",
                    "INSERT INTO annotations VALUES(42,1,1,'human','2026-01-01','note','USER_ASSERTED','legacy note','source')")
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT annotation_id,body,binding_status FROM annotations"), [(42, "legacy note", "BOUND")])

    def test_import_replay_does_not_duplicate_preserved_note(self):
        self.write("a.txt", FILLER)
        path = os.path.join(self.tmp, "notes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([dict(match="a.txt",author="human",kind="note",evidence_class="USER_ASSERTED",body="imported",basis="source")], f)
        self.build("--no-ocr", "--annotations", path)
        self.build("--no-ocr", "--annotations", path)
        self.assertEqual(self.q("SELECT COUNT(*) FROM annotations"), [(1,)])

    def test_note_invalid_source_or_unit_is_rejected(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        for args in (("--source-id", "999"), ("--source-id", "1", "--unit", "999")):
            self.assertNotEqual(self.kb("note", "add", *args, "--body", "invalid").returncode, 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM annotations"), [(0,)])

    def test_s4_rejects_duplicate_and_missing_page_numbers(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.change("UPDATE sources SET kind='pdf',page_count=2",
                    "UPDATE units SET unit_type='page',unit_no=1",
                    "INSERT INTO units(source_id,unit_type,unit_no,text) SELECT source_id,'page',1,text FROM units",
                    "INSERT INTO units_fts(units_fts) VALUES('rebuild')",
                    "INSERT INTO units_tri(units_tri) VALUES('rebuild')")
        self.assertNotEqual(self.kb("verify").returncode, 0)
        self.assertEqual(next(c["result"] for c in self.report()["checks"] if c["id"] == "S4"), "FAIL")

    def test_s5_rejects_stale_content_indexes(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.change("UPDATE units SET text='replacementzeta content'")
        self.assertNotEqual(self.kb("verify").returncode, 0)
        self.assertEqual(next(c["result"] for c in self.report()["checks"] if c["id"] == "S5"), "FAIL")

    def test_manifest_is_part_of_database_not_late_side_effect(self):
        self.write("a.txt", FILLER)
        os.mkdir(os.path.join(self.out, "manifest.json"))
        self.build("--no-ocr")
        manifest = json.loads(self.q("SELECT manifest_json FROM build_manifest")[0][0])
        self.assertEqual(manifest["counts"]["sources"], 1)
        before = sha256(self.db)
        r = self.kb("manifest", "--out", os.path.join(self.out, "manifest.json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sha256(self.db), before)

    def test_commit_failure_preserves_previous_database(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        before = sha256(self.db)
        argv = [KB, "--db", self.db, "build", "--root", "docs=" + self.docs, "--stage", os.path.join(self.tmp,"stage"),
                "--ocr-cache",os.path.join(self.out,"ocr.sqlite"),"--log",os.path.join(self.out,"build.log"),"--no-ocr"]
        with patch.object(sys,"argv",argv), patch.object(kb_release.os,"replace",side_effect=PermissionError("simulated commit failure")):
            with self.assertRaises(PermissionError):
                kb_release.main()
        self.assertEqual(sha256(self.db), before)
        self.assertFalse(os.path.exists(self.db + ".lock"))

    def test_existing_writer_lock_blocks_build_and_note(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        before = sha256(self.db)
        with open(self.db + ".lock", "w") as f:f.write("another writer")
        self.assertNotEqual(self.build("--no-ocr",ok=False).returncode,0)
        self.assertNotEqual(self.kb("note","add","--source-id","1","--body","x").returncode,0)
        self.assertEqual(sha256(self.db),before)
        self.assertTrue(os.path.exists(self.db + ".lock"))

    def test_office_extraction_does_not_write_beside_input(self):
        path = self.write("a.docx", b"synthetic office input")
        class Result:
            returncode=0
            stdout=FILLER.encode()
        builder = object.__new__(kb_release.Builder)
        observed=[]
        builder.store_text=lambda sid,text,method="text":observed.append((sid,text,method))
        with patch.object(kb_release.shutil,"which",return_value="extract-text"), patch.object(kb_release.subprocess,"run",return_value=Result()):
            builder.do_office(1,path)
        self.assertEqual(observed,[(1,FILLER,"office-extracted")])
        self.assertFalse(os.path.exists(path+".extracted.txt"))
