# tests/test_kb.py — checks each public claim about kb against real behavior.
# Copyright (C) 2026 Austin Simpkins and Chandler Lee Middlebrooks
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Run from the repository root:  python3 -m unittest -v tests/test_kb.py
# Needs poppler-utils. Optional: reportlab (PDF tests), Pillow + tesseract (OCR tests).
import hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(os.path.dirname(HERE), "kb.py")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
try:
    from reportlab.pdfgen import canvas as rl_canvas
    HAVE_RL = True
except ImportError:
    HAVE_RL = False
try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

APPLEDOUBLE = (b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02"
               b"com.apple.quarantine\x00q/0083;6a7455aa;Chrome;4DF9325F-1D0D-4CB5-A033-C5FB0FE1CB95\x00")
FILLER = ("Provenance means every passage keeps a record of where it came from, which file, "
          "which page, and which exact bytes. ") * 4


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class KbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.docs = os.path.join(self.tmp, "docs")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.docs)
        os.makedirs(self.out)
        self.db = os.path.join(self.out, "knowledge.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def write(self, name, data):
        path = os.path.join(self.docs, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def kb(self, *args):
        return subprocess.run([sys.executable, KB, "--db", self.db, *args], cwd=self.tmp,
                              capture_output=True, text=True)

    def build(self, *extra, ok=True):
        r = self.kb("build", "--root", f"docs={self.docs}", "--stage", os.path.join(self.tmp, "stage"),
                    "--ocr-cache", os.path.join(self.out, "ocr.sqlite"), "--log", os.path.join(self.out, "build.log"),
                    *extra)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        return r

    def q(self, sql, params=()):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def status(self, rel, member=None):
        if member is None:
            rows = self.q("SELECT status, status_detail FROM sources WHERE rel_path=? AND member_path IS NULL", (rel,))
        else:
            rows = self.q("SELECT status, status_detail FROM sources WHERE rel_path=? AND member_path=?", (rel, member))
        self.assertEqual(len(rows), 1, f"{rel}#{member} not registered exactly once")
        return rows[0]

    def report(self):
        with open(os.path.join(self.out, "validation_report.json"), encoding="utf-8") as f:
            return json.load(f)

    # claim: every file is registered, including archive members, macOS metadata, empty and corrupt files
    def test_every_file_registered_with_a_final_status_and_reason(self):
        self.write("notes.txt", "hello provenance " + FILLER)
        self.write("empty.txt", b"")
        self.write("corrupt.zip", b"this is not a zip archive")
        buf = os.path.join(self.docs, "archive.zip")
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(zipfile.ZipInfo("inner/"), b"")
            z.writestr("inner/readme.md", "# Inner readme\n" + FILLER)
            z.writestr("__MACOSX/inner/._readme.md", APPLEDOUBLE)
        self.build("--no-ocr")
        self.assertEqual(self.status("notes.txt")[0], "indexed")
        self.assertEqual(self.status("empty.txt")[0], "empty")
        self.assertEqual(self.status("corrupt.zip")[0], "error")
        self.assertEqual(self.status("archive.zip")[0], "container")
        self.assertEqual(self.status("archive.zip", "inner/")[0], "container")
        self.assertEqual(self.status("archive.zip", "inner/readme.md")[0], "indexed")
        self.assertEqual(self.status("archive.zip", "__MACOSX/inner/._readme.md")[0], "metadata_only")
        self.assertEqual(self.q("SELECT COUNT(*) FROM sources WHERE status='pending' OR status_detail IS NULL OR status_detail=''")[0][0], 0)
        self.assertIn(("Chrome",), self.q("SELECT value FROM meta WHERE key='quarantine_agent'"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM flags WHERE flag='corrupt_archive'")[0][0], 1)

    # claim: ten checks, all passing on a clean build
    def test_verify_runs_ten_checks_and_passes_clean_build(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        r = self.kb("verify")
        self.assertEqual(r.returncode, 0, r.stdout)
        checks = self.report()["checks"]
        self.assertEqual([c["id"] for c in checks], [f"S{i}" for i in range(10)])
        self.assertTrue(all(c["result"] == "PASS" for c in checks), checks)

    # claim: identical files are indexed once, every location kept
    def test_duplicates_indexed_once_and_linked(self):
        self.write("a.txt", FILLER)
        self.write("b.txt", FILLER)
        self.build("--no-ocr")
        a = self.q("SELECT source_id FROM sources WHERE rel_path='a.txt'")[0][0]
        status, dup_of = self.q("SELECT status, duplicate_of FROM sources WHERE rel_path='b.txt'")[0]
        self.assertEqual((status, dup_of), ("duplicate", a))
        self.assertEqual(self.q("SELECT COUNT(*) FROM units u JOIN sources s USING(source_id) WHERE s.rel_path='b.txt'")[0][0], 0)

    # claim: archives open with guards against unsafe paths and zip bombs; members staged by hash, never by name
    def test_path_traversal_member_is_flagged_and_never_written_by_name(self):
        with zipfile.ZipFile(os.path.join(self.docs, "evil.zip"), "w") as z:
            z.writestr("../escaped.txt", "should never land outside the stage folder " + FILLER)
        self.build("--no-ocr")
        self.assertEqual(self.q("SELECT COUNT(*) FROM flags WHERE flag='unsafe_member_path'")[0][0], 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.docs, "escaped.txt")))
        staged = self.q("SELECT staged_path FROM sources WHERE member_path='../escaped.txt'")[0][0]
        self.assertTrue(staged.startswith(os.path.join(self.tmp, "stage")))

    def test_zip_bomb_guard_refuses_to_expand(self):
        with zipfile.ZipFile(os.path.join(self.docs, "bomb.zip"), "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", b"\x00" * (51 * 1024 * 1024))
        self.build("--no-ocr")
        status, detail = self.status("bomb.zip", "zeros.bin")
        self.assertEqual(status, "error")
        self.assertIn("ratio", detail)
        self.assertEqual(self.q("SELECT COUNT(*) FROM flags WHERE flag='archive_guard'")[0][0], 1)

    def test_nested_archives_stop_at_depth_guard(self):
        inner = os.path.join(self.tmp, "z1.zip")
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep.txt", FILLER)
        for level in (2, 3, 4):
            outer = os.path.join(self.tmp, f"z{level}.zip")
            with zipfile.ZipFile(outer, "w") as z:
                z.write(inner, arcname=os.path.basename(inner))
            inner = outer
        shutil.copy(inner, os.path.join(self.docs, "z4.zip"))
        self.build("--no-ocr")
        status, detail = self.q("SELECT status, status_detail FROM sources WHERE member_path='z1.zip'")[0]
        self.assertEqual(status, "metadata_only")
        self.assertIn("depth guard", detail)
        self.assertEqual(self.q("SELECT COUNT(*) FROM sources WHERE member_path='deep.txt'")[0][0], 0)

    # claim: every passage traces back to its file, page, and SHA-256; two kinds of search
    @unittest.skipUnless(HAVE_RL and shutil.which("pdftotext"), "needs reportlab and poppler-utils")
    def test_pdf_pages_hash_search_and_markings(self):
        path = os.path.join(self.docs, "report.pdf")
        c = rl_canvas.Canvas(path)
        pages = [FILLER, "FBI file 56D-SF-4662117, dated March 13, 2020. " + FILLER, FILLER]
        for text in pages:
            t = c.beginText(40, 780)
            for i in range(0, len(text), 90):
                t.textLine(text[i:i + 90])
            c.drawText(t)
            c.showPage()
        c.save()
        self.build("--no-ocr")
        sid, stored_sha, pages_n = self.q("SELECT source_id, sha256, page_count FROM sources WHERE rel_path='report.pdf'")[0]
        self.assertEqual(stored_sha, sha256(path))
        self.assertEqual(pages_n, 3)
        self.assertEqual([r[0] for r in self.q("SELECT unit_no FROM units WHERE source_id=? ORDER BY unit_no", (sid,))], [1, 2, 3])
        r = self.kb("search", "--substring", "56D-SF-4662117")
        self.assertIn("report.pdf · page 2", r.stdout)
        r = self.kb("search", "provenance")
        self.assertIn("report.pdf", r.stdout)
        kinds = {t for (t,) in self.q("SELECT type FROM markings")}
        self.assertTrue({"fbi_file_number", "date"} <= kinds, kinds)

    # claim: personal-data patterns are counted, and the values are never copied
    def test_personal_data_patterns_counted_not_copied(self):
        self.write("contacts.txt", "Call 555-123-4567 or 555-987-6543 today. " + FILLER)
        self.build("--no-ocr")
        details = [d for (d,) in self.q("SELECT detail FROM flags WHERE flag='pii_pattern'")]
        self.assertEqual(len(details), 1)
        self.assertIn('"phone_like": 2', details[0])
        self.assertNotIn("555-123-4567", details[0])

    # claim: a failed build never replaces a working database
    def test_failed_build_leaves_previous_database_untouched(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        before = sha256(self.db)
        bad = os.path.join(self.tmp, "bad_annotations.json")
        with open(bad, "w") as f:
            f.write("{ this is not valid json")
        r = self.build("--no-ocr", "--annotations", bad, ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sha256(self.db), before)
        self.assertEqual(self.kb("verify").returncode, 0)

    # claim: verify warns when files on disk changed since the build
    def test_verify_warns_on_new_and_changed_files(self):
        self.write("a.txt", FILLER)
        self.build("--no-ocr")
        self.write("a.txt", FILLER + " edited")
        self.write("new.txt", FILLER)
        r = self.kb("verify")
        self.assertEqual(r.returncode, 0, "a warning must not fail the run")
        s1 = [c for c in self.report()["checks"] if c["id"] == "S1"][0]
        self.assertEqual(s1["result"], "WARN")
        self.assertIn("docs/a.txt", s1["detail"]["changed"])
        self.assertIn("docs/new.txt", s1["detail"]["new"])

    # claim: image-only content is OCR'd with a confidence score, and results are cached
    @unittest.skipUnless(HAVE_PIL and shutil.which("tesseract") and os.path.exists(FONT), "needs Pillow, tesseract and the DejaVu font")
    def test_ocr_reads_image_and_rebuild_uses_cache(self):
        img = Image.new("RGB", (1400, 300), "white")
        ImageDraw.Draw(img).text((40, 100), "PROVENANCE LEDGER 2026", fill="black", font=ImageFont.truetype(FONT, 72))
        img.save(os.path.join(self.docs, "scan.png"))
        first = self.build()
        text, conf = self.q("SELECT text, ocr_conf FROM units WHERE unit_type='image'")[0]
        self.assertIn("PROVENANCE", text.upper())
        self.assertIsNotNone(conf)
        self.assertIn("0 from cache", first.stdout)
        second = self.build()
        self.assertIn("1 from cache, 0 to run", second.stdout)


if __name__ == "__main__":
    unittest.main()
