#!/usr/bin/env python3
# kb.py — provenance-first personal knowledge base
# Copyright (C) 2026 Austin Simpkins and Chandler Lee Middlebrooks
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
kb.py — provenance-preserving personal knowledge base (SQLite + FTS5).

Every file under every input root is registered — including archive members,
macOS metadata files, empty and corrupt files. Nothing is dropped: files that
cannot be read are recorded with a status and the reason. Text is extracted
(native PDF text, OCR for image-only pages, plain text, office formats) and
indexed for word search (porter/unicode61) and substring search (trigram).

Commands:  build | verify | search | show | sources | cards | stats | note | export
Run `python3 kb.py <command> -h` for options.

Requirements: Python 3.9+ with sqlite3 FTS5 (trigram tokenizer needs SQLite >= 3.34),
poppler-utils (pdfinfo, pdftotext, pdftoppm), tesseract (for OCR).
Optional: extract-text (office formats).
"""
import argparse, csv, datetime, hashlib, json, os, re, shutil, sqlite3, subprocess
import sys, tempfile, time, zipfile
from contextlib import contextmanager, closing
from concurrent.futures import ProcessPoolExecutor, as_completed

VERSION = "1.0.1"
OCR_MIN_ALNUM = 300      # a page with fewer native alphanumerics than this is OCR'd
NO_TEXT_ALNUM = 20       # below this after all extraction -> flagged no_text
LOW_CONF = 60.0          # mean tesseract word confidence below this -> flagged
CHUNK_CHARS = 4000       # text files are split into units of about this size
MAX_UNCOMPRESSED = 2 * 1024**3
MAX_RATIO = 200
MAX_DEPTH = 3
TEXT_EXT = {".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".xml",
            ".py", ".js", ".ts", ".yaml", ".yml", ".log", ".ini", ".cfg", ".toml", ".sql"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".rtf", ".ipynb"}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY, kb_version TEXT, started_utc TEXT, finished_utc TEXT,
  status TEXT, params_json TEXT, tools_json TEXT, roots_json TEXT, counts_json TEXT);
CREATE TABLE IF NOT EXISTS sources(
  source_id INTEGER PRIMARY KEY,
  collection TEXT NOT NULL, root_path TEXT NOT NULL, rel_path TEXT NOT NULL,
  parent_id INTEGER REFERENCES sources(source_id), member_path TEXT, depth INTEGER NOT NULL,
  kind TEXT NOT NULL, size_bytes INTEGER, compressed_bytes INTEGER, sha256 TEXT,
  mtime_utc TEXT, staged_path TEXT, page_count INTEGER,
  status TEXT NOT NULL, status_detail TEXT, duplicate_of INTEGER REFERENCES sources(source_id));
CREATE INDEX IF NOT EXISTS ix_sources_sha ON sources(sha256);
CREATE TABLE IF NOT EXISTS meta(source_id INTEGER REFERENCES sources(source_id), key TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS units(
  unit_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(source_id),
  unit_type TEXT NOT NULL, unit_no INTEGER NOT NULL, char_offset INTEGER,
  method TEXT, text_native TEXT, text_ocr TEXT, text TEXT,
  alnum_native INTEGER, alnum_ocr INTEGER, ocr_needed INTEGER DEFAULT 0,
  ocr_conf REAL, ocr_words INTEGER, ocr_seconds REAL, ocr_error TEXT);
CREATE INDEX IF NOT EXISTS ix_units_src ON units(source_id, unit_no);
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(text, content='units', content_rowid='unit_id', tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS units_tri USING fts5(text, content='units', content_rowid='unit_id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS markings(unit_id INTEGER REFERENCES units(unit_id), type TEXT, value TEXT);
CREATE INDEX IF NOT EXISTS ix_mark ON markings(type, value);
CREATE TABLE IF NOT EXISTS flags(source_id INTEGER, unit_id INTEGER, flag TEXT, severity TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS annotations(
  annotation_id INTEGER PRIMARY KEY, source_id INTEGER, unit_id INTEGER, author TEXT, created_utc TEXT,
  kind TEXT, evidence_class TEXT, body TEXT, basis TEXT,
  source_binding_json TEXT, unit_binding_json TEXT, binding_status TEXT);
CREATE TABLE IF NOT EXISTS build_manifest(manifest_json TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS source_labels AS
  SELECT s.source_id, s.collection || ':' || s.rel_path || COALESCE('#' || s.member_path, '') AS label FROM sources s;
"""

MARK_RX = {
    "classification": re.compile(r"\b(?:TOP SECRET|SECRET|CONFIDENTIAL|UNCLASSIFIED)(?:\s*//\s*[A-Z][A-Z0-9 ,/-]{1,60})?\b"),
    "declassification": re.compile(r"(?i)\bDE\s?-?CLASSIFIED\s+(?:BY|ON)\b[^\n]{0,100}"),
    "fbi_file_number": re.compile(r"\b\d{1,4}[A-Z]{0,2}-[A-Z]{2}-\d{4,8}\b"),
    "usc_citation": re.compile(r"\b\d{1,2}\s+U\.\s?S\.\s?C\.\s*§*\s*\d[0-9a-z()\-–]*"),
    "date": re.compile(r"\b(?:\d{1,2}/\d{1,2}/(?:19|20)\d{2}|(?:19|20)\d{2}-\d{2}-\d{2}|"
                       r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+(?:19|20)\d{2}|"
                       r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:19|20)\d{2})\b"),
    "url": re.compile(r"https?://[^\s<>\"')\]]+"),
}
EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PII_RX = {
    "ssn_like": re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "phone_like": re.compile(r"(?<!\d)\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"),
}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def alnum(s):
    # Count Unicode letters/numbers without normalizing or interpreting the text.
    return sum(ch.isalnum() for ch in (s or ""))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalized_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def under(path, directory):
    try:
        return os.path.commonpath([normalized_path(path), normalized_path(directory)]) == normalized_path(directory)
    except ValueError:
        return False


def parse_roots(specs):
    roots, names = [], set()
    for spec in specs:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path or name in names:
            raise ValueError("each root must have a unique nonempty name=directory: " + spec)
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise ValueError("input root does not exist or is not a directory: " + path)
        names.add(name)
        roots.append((name, path))
    return roots


@contextmanager
def database_writer(db):
    """Serialize cooperating builders/note writers. Never remove an existing lock."""
    lock = os.path.abspath(db) + ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "created_utc": utcnow()}, f)
        yield
    finally:
        os.remove(lock)


def source_binding(con, sid):
    if sid is None:
        return None
    chain, visited = [], set()
    while sid is not None:
        if sid in visited:
            raise ValueError("cyclic archive parent binding")
        visited.add(sid)
        row = con.execute("SELECT collection,root_path,rel_path,member_path,sha256,parent_id FROM sources WHERE source_id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("annotation source no longer exists")
        chain.append(list(row[:5]))
        sid = row[5]
    return canonical_json(chain)


def unit_binding(con, uid, sid):
    if uid is None:
        return None
    row = con.execute("SELECT unit_type,unit_no,method,text,source_id FROM units WHERE unit_id=?", (uid,)).fetchone()
    if row is None or row[4] != sid:
        raise ValueError("annotation unit does not belong to its source")
    return canonical_json([row[0], row[1], row[2], hashlib.sha256((row[3] or "").encode("utf-8")).hexdigest()])


def preserve_annotations(old_path, new_con):
    """Retain notes and original bindings; never attach a note to changed bytes."""
    if not os.path.exists(old_path):
        return
    # Read the existing database without creating or upgrading it.
    from pathlib import Path
    with closing(sqlite3.connect(Path(old_path).resolve().as_uri() + "?mode=ro", uri=True)) as old:
        old.row_factory = sqlite3.Row
        if not old.execute("SELECT 1 FROM sqlite_master WHERE name='annotations'").fetchone():
            raise ValueError("existing database has no annotations table; refusing replacement")
        sources = {}
        for (sid,) in new_con.execute("SELECT source_id FROM sources"):
            sources.setdefault(source_binding(new_con, sid), []).append(sid)
        for note in old.execute("SELECT * FROM annotations ORDER BY annotation_id"):
            n = dict(note)
            sb = n.get("source_binding_json") or source_binding(old, n["source_id"])
            ub = n.get("unit_binding_json") or unit_binding(old, n["unit_id"], n["source_id"])
            matches = sources.get(sb, []) if sb else []
            sid = matches[0] if len(matches) == 1 else None
            uid = None
            status = "BOUND" if sid is not None else "SOURCE_UNRESOLVED"
            if sid is not None and ub:
                um = [u for (u,) in new_con.execute("SELECT unit_id FROM units WHERE source_id=?", (sid,))
                      if unit_binding(new_con, u, sid) == ub]
                uid = um[0] if len(um) == 1 else None
                if uid is None:
                    status = "UNIT_UNRESOLVED"
            new_con.execute("INSERT INTO annotations(annotation_id,source_id,unit_id,author,created_utc,kind,evidence_class,body,basis,source_binding_json,unit_binding_json,binding_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (n["annotation_id"], sid, uid, n["author"], n["created_utc"], n["kind"], n["evidence_class"], n["body"], n["basis"], sb, ub, status))


def sniff(name, head, size):
    if size == 0:
        return "empty"
    ext = os.path.splitext(name.lower())[1]
    base = os.path.basename(name)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x00\x05\x16\x07") or (base.startswith("._") and head[:4] == b"\x00\x05\x16\x07"):
        return "appledouble"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "office" if ext in OFFICE_EXT else "zip"
    if ext == ".zip":
        return "zip"   # claimed zip without a zip signature -> recorded as corrupt by the zip handler
    if (head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] == b"GIF8"
            or head[:4] in (b"II*\x00", b"MM\x00*") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        return "image"
    if ext in OFFICE_EXT:
        return "office"
    if ext in TEXT_EXT:
        return "text"
    if b"\x00" not in head[:4096]:
        try:
            head[:4096].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "binary"


def tool_versions():
    def first_line(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            return (r.stdout or r.stderr).strip().splitlines()[0]
        except Exception as e:
            return f"unavailable ({type(e).__name__})"
    return {
        "kb": VERSION, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
        "pdftotext": first_line(["pdftotext", "-v"]), "pdfinfo": first_line(["pdfinfo", "-v"]),
        "pdftoppm": first_line(["pdftoppm", "-v"]), "tesseract": first_line(["tesseract", "--version"]),
        "extract-text": "present" if shutil.which("extract-text") else "absent",
    }


# ---------------------------------------------------------------- OCR worker (top level for pickling)
def ocr_task(job):
    kind, path, page, dpi, lang, psm = job
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        if kind == "pdf":
            base = os.path.join(td, "pg")
            r = subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-f", str(page), "-l", str(page),
                                "-singlefile", path, base], capture_output=True)
            img = base + ".png"
            if r.returncode != 0 or not os.path.exists(img):
                return {"error": "pdftoppm: " + r.stderr.decode("utf-8", "replace")[:300]}
        else:
            img = path
        out = os.path.join(td, "o")
        r = subprocess.run(["tesseract", img, out, "-l", lang, "--psm", str(psm), "txt", "tsv"], capture_output=True)
        if r.returncode != 0 or not os.path.exists(out + ".txt"):
            return {"error": "tesseract: " + r.stderr.decode("utf-8", "replace")[:300]}
        with open(out + ".txt", encoding="utf-8", errors="replace") as f:
            text = f.read()
        confs = []
        with open(out + ".tsv", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                try:
                    c = float(row.get("conf") or -1)
                except ValueError:
                    continue
                if c >= 0 and (row.get("text") or "").strip():
                    confs.append(c)
    return {"text": text, "conf": round(sum(confs) / len(confs), 2) if confs else None,
            "words": len(confs), "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- builder
class Builder:
    def __init__(self, args):
        self.a = args
        self.roots = parse_roots(args.root)
        self.stage = os.path.abspath(args.stage)
        self.exclude = [os.path.abspath(p) for p in (args.exclude_under or [])] + [self.stage]
        for name, root in self.roots:
            if any(under(root, x) for x in self.exclude):
                raise ValueError("input root is covered by a stage/exclusion directory: " + root)
        self.original_db_hash = sha256_file(args.db) if os.path.exists(args.db) else None
        artifacts = [os.path.abspath(args.db), os.path.abspath(args.ocr_cache)]
        self.exclude_files = {normalized_path(p + suffix) for p in artifacts
                              for suffix in ("", "-wal", "-shm", ".building", ".building-wal", ".building-shm", ".lock")}
        out = os.path.dirname(os.path.abspath(args.db))
        self.exclude_files.update(normalized_path(p) for p in [args.log, os.path.join(out, "manifest.json"),
                                  os.path.join(out, "validation_report.json"), os.path.join(out, "validation_report.md")])
        os.makedirs(self.stage, exist_ok=True)
        self.tmp_db = args.db + ".building"
        for p in (self.tmp_db, self.tmp_db + "-wal", self.tmp_db + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        self.con = sqlite3.connect(self.tmp_db)
        self.con.executescript(SCHEMA)
        self.cache = sqlite3.connect(args.ocr_cache)
        self.cache.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, text TEXT, conf REAL, words INTEGER, seconds REAL, created_utc TEXT)")
        self.tools = tool_versions()
        self.seen_sha = {}
        self.ocr_jobs = []   # (unit_id, sha256, kind, path, page)
        self.log = open(args.log, "a", encoding="utf-8")

    def event(self, **kw):
        kw["t"] = utcnow()
        self.log.write(json.dumps(kw, ensure_ascii=False) + "\n")
        self.log.flush()

    def flag(self, source_id, unit_id, flag, severity, detail):
        self.con.execute("INSERT INTO flags VALUES(?,?,?,?,?)", (source_id, unit_id, flag, severity, detail))

    def add_unit(self, source_id, unit_type, unit_no, text, method, offset=None):
        cur = self.con.execute(
            "INSERT INTO units(source_id,unit_type,unit_no,char_offset,method,text_native,text,alnum_native) VALUES(?,?,?,?,?,?,?,?)",
            (source_id, unit_type, unit_no, offset, method, text, text, alnum(text)))
        return cur.lastrowid

    def register(self, collection, root, rel, parent_id, member, depth, kind, size, csize, sha, mtime, staged):
        cur = self.con.execute(
            "INSERT INTO sources(collection,root_path,rel_path,parent_id,member_path,depth,kind,size_bytes,compressed_bytes,sha256,mtime_utc,staged_path,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (collection, root, rel, parent_id, member, depth, kind, size, csize, sha, mtime, staged, "pending"))
        return cur.lastrowid

    def set_status(self, sid, status, detail=None, **extra):
        sets = ["status=?", "status_detail=?"] + [f"{k}=?" for k in extra]
        self.con.execute(f"UPDATE sources SET {', '.join(sets)} WHERE source_id=?",
                         [status, detail] + list(extra.values()) + [sid])

    # ------------------------------------------------------------ discovery
    def run(self):
        roots = self.roots
        cur = self.con.execute("INSERT INTO runs(kb_version,started_utc,status,params_json,tools_json,roots_json) VALUES(?,?,?,?,?,?)",
                               (VERSION, utcnow(), "running",
                                json.dumps({"ocr_dpi": self.a.ocr_dpi, "ocr_lang": self.a.ocr_lang, "ocr_psm": self.a.ocr_psm,
                                            "ocr_min_alnum": OCR_MIN_ALNUM, "low_conf": LOW_CONF, "chunk_chars": CHUNK_CHARS,
                                            "workers": self.a.workers, "ocr": not self.a.no_ocr, "exclude_under": self.exclude,
                                            "exclude_files": sorted(self.exclude_files)}),
                                json.dumps(self.tools), json.dumps(roots)))
        self.run_id = cur.lastrowid
        for name, root in roots:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda error: (_ for _ in ()).throw(error)):
                dirnames.sort()
                if any(under(dirpath, x) for x in self.exclude):
                    self.event(level="info", msg="skipped own output folder", path=dirpath)
                    dirnames[:] = []
                    continue
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    if normalized_path(full) in self.exclude_files:
                        continue
                    self.ingest_file(name, root, os.path.relpath(full, root), full)
        if not self.con.execute("SELECT 1 FROM sources LIMIT 1").fetchone() and not self.a.allow_empty:
            raise ValueError("no input files were registered; use --allow-empty only for an intentional empty build")
        self.con.commit()
        remaining = self.run_ocr()
        if self.a.ocr_only:
            self.con.close()
            for p in (self.tmp_db, self.tmp_db + "-wal", self.tmp_db + "-shm"):
                if os.path.exists(p):
                    os.remove(p)
            print(f"OCR_REMAINING {remaining}", flush=True)
            return
        if remaining:
            print(f"warning: {remaining} OCR units not run (time budget); they will be flagged", flush=True)
        self.finalize()

    def ingest_file(self, collection, root, rel, full):
        st = os.stat(full)
        with open(full, "rb") as f:
            head = f.read(8192)
        kind = sniff(full, head, st.st_size)
        sha = sha256_file(full)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
        sid = self.register(collection, root, rel, None, None, 0, kind, st.st_size, None, sha, mtime, full)
        self.process(sid, collection, root, rel, kind, full, sha, depth=0)

    def process(self, sid, collection, root, rel, kind, path, sha, depth):
        if kind == "empty":
            self.set_status(sid, "empty", "0 bytes; no content to extract")
            self.flag(sid, None, "empty_file", "warn", "file has zero bytes")
            return
        if kind not in ("zip",) and sha in self.seen_sha:
            first = self.seen_sha[sha]
            self.set_status(sid, "duplicate", f"identical bytes to source {first}; content indexed once", duplicate_of=first)
            self.flag(sid, None, "duplicate_content", "info", f"duplicate of source {first}")
            return
        self.seen_sha.setdefault(sha, sid)
        try:
            if kind == "zip":
                self.do_zip(sid, collection, root, rel, path, depth)
            elif kind == "pdf":
                self.do_pdf(sid, path, sha)
            elif kind == "image":
                uid = self.add_unit(sid, "image", 1, "", "ocr-pending")
                self.con.execute("UPDATE units SET ocr_needed=1 WHERE unit_id=?", (uid,))
                self.ocr_jobs.append((uid, sha, "image", path, 1))
                self.set_status(sid, "indexed", "image; text from OCR")
            elif kind == "text":
                self.do_text(sid, path)
            elif kind == "office":
                self.do_office(sid, path)
            elif kind == "appledouble":
                self.do_appledouble(sid, path)
            else:
                self.do_strings(sid, path)
        except Exception as e:
            self.set_status(sid, "error", f"{type(e).__name__}: {e}")
            self.flag(sid, None, "extraction_error", "error", f"{type(e).__name__}: {e}")
            self.event(level="error", source_id=sid, error=f"{type(e).__name__}: {e}")

    def do_zip(self, sid, collection, root, rel, path, depth):
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            self.set_status(sid, "error", f"unreadable archive: {e}")
            self.flag(sid, None, "corrupt_archive", "error", str(e))
            return
        infos = z.infolist()
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            self.set_status(sid, "error", f"archive expands to {total} bytes, over the {MAX_UNCOMPRESSED}-byte guard")
            self.flag(sid, None, "archive_guard", "error", "uncompressed size guard")
            return
        for info in infos:
            member = info.filename
            unsafe = member.startswith(("/", "\\")) or ".." in member.replace("\\", "/").split("/")
            if info.is_dir():
                cid = self.register(collection, root, rel, sid, member, depth + 1, "directory", 0, 0, None, None, None)
                self.set_status(cid, "container", "archive directory entry")
                continue
            if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO and info.file_size > 50 * 1024**2:
                cid = self.register(collection, root, rel, sid, member, depth + 1, "unknown", info.file_size, info.compress_size, None, None, None)
                self.set_status(cid, "error", "compression-ratio guard tripped; member not expanded")
                self.flag(cid, None, "archive_guard", "error", "compression ratio guard")
                continue
            data = z.read(info)
            csha = hashlib.sha256(data).hexdigest()
            ext = os.path.splitext(member)[1].lower()[:10]
            staged = os.path.join(self.stage, csha + ext)
            if not os.path.exists(staged):
                with open(staged, "wb") as f:
                    f.write(data)
            kind = sniff(member, data[:8192], len(data))
            mtime = datetime.datetime(*info.date_time, tzinfo=datetime.timezone.utc).isoformat(timespec="seconds") if info.date_time else None
            cid = self.register(collection, root, rel, sid, member, depth + 1, kind, info.file_size, info.compress_size, csha, mtime, staged)
            if unsafe:
                self.flag(cid, None, "unsafe_member_path", "warn", "path contains traversal; staged by hash, never by name")
            if kind == "zip" and depth + 1 >= MAX_DEPTH:
                self.set_status(cid, "metadata_only", "nested archive beyond depth guard; registered, not expanded")
                continue
            self.process(cid, collection, root, rel, kind, staged, csha, depth + 1)
        self.set_status(sid, "container", f"archive with {len(infos)} entries")

    def do_pdf(self, sid, path, sha):
        r = subprocess.run(["pdfinfo", path], capture_output=True, text=True, errors="replace")
        meta = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        for k, v in meta.items():
            self.con.execute("INSERT INTO meta VALUES(?,?,?)", (sid, k, v))
        n = int(meta.get("Pages", "0") or 0)
        if r.returncode != 0 or n == 0:
            self.set_status(sid, "error", "pdfinfo failed: " + r.stderr.strip()[:300])
            self.flag(sid, None, "pdf_error", "error", r.stderr.strip()[:300])
            return
        t = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"], capture_output=True)
        if t.returncode != 0:
            raise RuntimeError("pdftotext failed: " + t.stderr.decode("utf-8", "replace")[:200])
        pages = self.decode_text(sid, t.stdout).split("\f")
        if len(pages) == n + 1 and not pages[-1].strip():
            pages = pages[:n]
        if len(pages) != n:
            self.flag(sid, None, "page_split_fallback", "info", f"form-feed split gave {len(pages)} of {n}; used per-page extraction")
            pages = []
            for i in range(1, n + 1):
                rr = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-f", str(i), "-l", str(i), path, "-"], capture_output=True)
                if rr.returncode != 0:
                    raise RuntimeError(f"pdftotext page {i} failed: " + rr.stderr.decode("utf-8", "replace")[:200])
                page_text = self.decode_text(sid, rr.stdout)
                pages.append(page_text.removesuffix("\f"))
        for i, txt in enumerate(pages, 1):
            uid = self.add_unit(sid, "page", i, txt, "native")
            if alnum(txt) < OCR_MIN_ALNUM and not self.a.no_ocr:
                self.con.execute("UPDATE units SET ocr_needed=1 WHERE unit_id=?", (uid,))
                self.ocr_jobs.append((uid, sha, "pdf", path, i))
        self.set_status(sid, "indexed", f"PDF, {n} pages", page_count=n)

    def do_text(self, sid, path):
        with open(path, "rb") as f:
            raw = f.read()
        s = self.decode_text(sid, raw)
        self.store_text(sid, s)

    def decode_text(self, sid, raw):
        """Decode a declared UTF-8 channel; do not guess another encoding or intent."""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self.flag(sid, None, "decode_replacement", "warn",
                      "invalid UTF-8 bytes replaced in derived text; original file/hash remains the source")
            return raw.decode("utf-8", "replace")

    def store_text(self, sid, s, method="text"):
        i, n, k = 0, len(s), 0
        while i < n or k == 0:
            j = min(n, i + CHUNK_CHARS)
            if j < n:
                cut = s.rfind("\n", i + CHUNK_CHARS // 2, j)
                if cut > i:
                    j = cut + 1
            k += 1
            self.add_unit(sid, "chunk", k, s[i:j], method, offset=i)
            i = j
            if n == 0:
                break
        self.set_status(sid, "indexed", f"text, {n} characters in {k} chunks")

    def do_office(self, sid, path):
        if not shutil.which("extract-text"):
            self.set_status(sid, "metadata_only", "office format; extract-text not installed")
            return
        r = subprocess.run(["extract-text", path], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("extract-text failed: " + r.stderr.decode("utf-8", "replace")[:200])
        self.store_text(sid, self.decode_text(sid, r.stdout), method="office-extracted")

    def do_strings(self, sid, path):
        with open(path, "rb") as f:
            b = f.read(50 * 1024**2)
        asc = [x.decode("ascii") for x in re.findall(rb"[ -~]{5,}", b)]
        wide = [x.decode("utf-16-le", "replace") for x in re.findall(rb"(?:[ -~]\x00){5,}", b)]
        self.add_unit(sid, "metadata", 1, "Binary file; printable strings (ASCII and UTF-16):\n" + "\n".join(dict.fromkeys(asc + wide)), "strings")
        self.set_status(sid, "metadata_only", "binary format with no text extractor; printable strings indexed")
        self.flag(sid, None, "no_extractor", "info", "binary content; only printable strings indexed")

    def do_appledouble(self, sid, path):
        with open(path, "rb") as f:
            b = f.read()
        strings = sorted(set(x.decode("ascii") for x in re.findall(rb"[ -~]{6,}", b)))
        parts = ["macOS AppleDouble metadata (extended attributes of the neighbouring file)."]
        q = re.search(rb"q/([0-9a-fA-F]{4});([0-9a-fA-F]{8});([^;\x00]*);([0-9A-Fa-f-]{36})?", b)
        if q:
            ts = datetime.datetime.fromtimestamp(int(q.group(2), 16), datetime.timezone.utc).isoformat(timespec="seconds")
            agent = q.group(3).decode("ascii", "replace")
            parts.append(f"Quarantine record: downloaded by {agent or 'unknown agent'} at {ts} (flags {q.group(1).decode()}).")
            self.con.execute("INSERT INTO meta VALUES(?,?,?)", (sid, "quarantine_download_utc", ts))
            self.con.execute("INSERT INTO meta VALUES(?,?,?)", (sid, "quarantine_agent", agent))
        urls = sorted(set(u.decode("ascii", "replace") for u in re.findall(rb"https?://[ -~]+?(?=[\x00-\x1f]|$)", b)))
        for u in urls:
            self.con.execute("INSERT INTO meta VALUES(?,?,?)", (sid, "where_from_url", u))
        if urls:
            parts.append("Where-from URLs: " + ", ".join(urls))
        parts.append("Printable strings: " + " | ".join(strings))
        self.add_unit(sid, "metadata", 1, "\n".join(parts), "strings")
        self.set_status(sid, "metadata_only", "AppleDouble resource-fork file; strings and quarantine record indexed")
        self.flag(sid, None, "appledouble_metadata", "info", "macOS metadata companion file")

    # ------------------------------------------------------------ OCR
    def run_ocr(self):
        if self.a.no_ocr or not self.ocr_jobs:
            return 0
        tv = self.tools["tesseract"]
        pending, done = [], 0
        for uid, sha, kind, path, page in self.ocr_jobs:
            key = f"{sha}|{page}|{self.a.ocr_dpi}|{self.a.ocr_lang}|{self.a.ocr_psm}|{tv}"
            row = self.cache.execute("SELECT text,conf,words,seconds FROM cache WHERE key=?", (key,)).fetchone()
            if row:
                self.apply_ocr(uid, {"text": row[0], "conf": row[1], "words": row[2], "seconds": row[3]})
                done += 1
            else:
                pending.append((uid, key, (kind, path, page, self.a.ocr_dpi, self.a.ocr_lang, self.a.ocr_psm)))
        self.con.commit()
        self.event(level="info", msg="ocr start", total=len(self.ocr_jobs), cached=done, pending=len(pending))
        print(f"OCR: {len(self.ocr_jobs)} units, {done} from cache, {len(pending)} to run", flush=True)
        t0 = time.time()
        budget = self.a.ocr_budget or 0
        over = lambda: budget and (time.time() - t0) > budget
        remaining, n = len(pending), 0
        if self.a.workers <= 1:
            batches = [[job] for job in pending]
        else:
            size = self.a.workers * 4
            batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        ex = ProcessPoolExecutor(max_workers=self.a.workers) if self.a.workers > 1 else None
        try:
            for batch in batches:
                if over():
                    break
                if ex is None:
                    results = []
                    for uid, key, job in batch:
                        try:
                            results.append((uid, key, ocr_task(job)))
                        except Exception as e:
                            results.append((uid, key, {"error": f"{type(e).__name__}: {e}"}))
                else:
                    futs = {ex.submit(ocr_task, job): (uid, key) for uid, key, job in batch}
                    results = []
                    for fut in as_completed(futs):
                        uid, key = futs[fut]
                        try:
                            results.append((uid, key, fut.result()))
                        except Exception as e:
                            results.append((uid, key, {"error": f"{type(e).__name__}: {e}"}))
                for uid, key, res in results:
                    if "error" not in res:
                        self.cache.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)",
                                           (key, res["text"], res["conf"], res["words"], res["seconds"], utcnow()))
                        self.cache.commit()
                    self.apply_ocr(uid, res)
                    n += 1
                    remaining -= 1
                    if n % 10 == 0 or remaining == 0:
                        self.con.commit()
                        rate = (time.time() - t0) / n
                        print(f"  OCR {n}/{len(pending)}  ~{rate:.1f}s/unit  eta {rate * remaining / 60:.1f} min", flush=True)
        finally:
            if ex is not None:
                ex.shutdown(cancel_futures=True)
        self.con.commit()
        return remaining

    def apply_ocr(self, uid, res):
        if "error" in res:
            self.con.execute("UPDATE units SET ocr_error=? WHERE unit_id=?", (res["error"], uid))
            sid = self.con.execute("SELECT source_id FROM units WHERE unit_id=?", (uid,)).fetchone()[0]
            self.flag(sid, uid, "ocr_error", "error", res["error"])
            return
        native = self.con.execute("SELECT text_native FROM units WHERE unit_id=?", (uid,)).fetchone()[0] or ""
        ocr = res["text"] or ""
        method = "ocr" if alnum(native) == 0 else "native+ocr"
        combined = (native.strip() + "\n\n[OCR]\n" + ocr) if native.strip() else ocr
        self.con.execute("UPDATE units SET text_ocr=?, text=?, method=?, alnum_ocr=?, ocr_conf=?, ocr_words=?, ocr_seconds=? WHERE unit_id=?",
                         (ocr, combined, method, alnum(ocr), res["conf"], res["words"], res["seconds"], uid))

    # ------------------------------------------------------------ post-processing
    def finalize(self):
        c = self.con
        for uid, sid, text, conf, needed, err in c.execute(
                "SELECT unit_id, source_id, text, ocr_conf, ocr_needed, ocr_error FROM units").fetchall():
            text = text or ""
            if alnum(text) < NO_TEXT_ALNUM:
                c.execute("INSERT INTO flags VALUES(?,?,?,?,?)", (sid, uid, "no_text", "warn",
                          "fewer than %d alphanumeric characters after all extraction" % NO_TEXT_ALNUM))
            if conf is not None and conf < LOW_CONF:
                c.execute("INSERT INTO flags VALUES(?,?,?,?,?)", (sid, uid, "ocr_low_confidence", "warn",
                          f"mean word confidence {conf}; handwriting, stamps or redaction boxes likely — check the page image"))
            seen = set()
            for mtype, rx in MARK_RX.items():
                for m in rx.finditer(text):
                    v = m.group(0).strip()[:200]
                    if (mtype, v) not in seen:
                        seen.add((mtype, v))
                        c.execute("INSERT INTO markings VALUES(?,?,?)", (uid, mtype, v))
            for dom in set(m.group(1).lower() for m in EMAIL_RX.finditer(text)):
                c.execute("INSERT INTO markings VALUES(?,?,?)", (uid, "email_domain", dom))
            counts = {k: len(rx.findall(text)) for k, rx in PII_RX.items()}
            counts = {k: v for k, v in counts.items() if v}
            if counts:
                c.execute("INSERT INTO flags VALUES(?,?,?,?,?)", (sid, uid, "pii_pattern", "review",
                          "pattern counts only (values not copied): " + json.dumps(counts)))
        for (sid,) in c.execute("SELECT source_id FROM sources WHERE collection='transcripts'").fetchall():
            c.execute("INSERT INTO flags VALUES(?,?,?,?,?)", (sid, None, "personal_transcript", "review",
                      "private session record with platform-injected text; review before sharing this database"))
        c.execute("INSERT INTO units_fts(units_fts) VALUES('rebuild')")
        c.execute("INSERT INTO units_tri(units_tri) VALUES('rebuild')")
        preserve_annotations(self.a.db, c)
        if self.a.annotations:
            with open(self.a.annotations, encoding="utf-8") as f:
                for n in json.load(f):
                    rows = c.execute("SELECT source_id FROM sources WHERE (member_path IS NULL AND rel_path=?) OR member_path=?",
                                     (n["match"], n["match"])).fetchall()
                    if len(rows) != 1:
                        raise ValueError("annotation target missing or ambiguous: " + n["match"])
                    sb = source_binding(c, rows[0][0])
                    values = (sb, n["author"], n["kind"], n["evidence_class"], n["body"], n["basis"])
                    if c.execute("SELECT 1 FROM annotations WHERE source_binding_json=? AND unit_binding_json IS NULL AND author=? AND kind=? AND evidence_class=? AND body=? AND basis=?", values).fetchone():
                        continue
                    c.execute("INSERT INTO annotations(source_id,unit_id,author,created_utc,kind,evidence_class,body,basis,source_binding_json,binding_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (rows[0][0], None, n["author"], n.get("created_utc", utcnow()), n["kind"], n["evidence_class"], n["body"], n["basis"], sb, "BOUND"))
        counts = {
            "sources": c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "by_status": dict(c.execute("SELECT status, COUNT(*) FROM sources GROUP BY status").fetchall()),
            "by_kind": dict(c.execute("SELECT kind, COUNT(*) FROM sources GROUP BY kind").fetchall()),
            "units": c.execute("SELECT COUNT(*) FROM units").fetchone()[0],
            "by_method": dict(c.execute("SELECT method, COUNT(*) FROM units GROUP BY method").fetchall()),
            "flags": dict(c.execute("SELECT flag, COUNT(*) FROM flags GROUP BY flag").fetchall()),
        }
        manifest = {"kb_version": VERSION, "built_utc": utcnow(), "db": os.path.basename(self.a.db),
                    "tools": self.tools, "counts": counts,
                    "inputs": []}
        for row in c.execute("SELECT collection, rel_path, size_bytes, sha256, status FROM sources WHERE parent_id IS NULL ORDER BY source_id"):
            manifest["inputs"].append(dict(zip(("collection", "path", "size_bytes", "sha256", "status"), row)))
        # The database and its manifest have one publication boundary. Exporting a
        # JSON manifest is a separate command and cannot make a build fail late.
        c.execute("INSERT INTO build_manifest VALUES(?)", (canonical_json(manifest),))
        c.execute("UPDATE runs SET finished_utc=?, status=?, counts_json=? WHERE run_id=?", (utcnow(), "complete", json.dumps(counts), self.run_id))
        for table in ("units_fts", "units_tri"):
            c.execute(f"INSERT INTO {table}({table},rank) VALUES('integrity-check',1)")
        if c.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("candidate SQLite integrity check failed")
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("PRAGMA journal_mode=DELETE")
        c.close()
        current_hash = sha256_file(self.a.db) if os.path.exists(self.a.db) else None
        if current_hash != self.original_db_hash or any(os.path.exists(self.a.db + x) for x in ("-wal", "-shm")):
            raise ValueError("existing database changed or has active WAL sidecars; refusing replacement")
        os.replace(self.tmp_db, self.a.db)
        try:
            print(json.dumps(counts, indent=2))
        except OSError:
            pass  # publication succeeded; a closed output stream cannot roll it back

    def close(self):
        for handle in (self.con, self.cache, self.log):
            try:
                handle.close()
            except Exception:
                pass


# ---------------------------------------------------------------- verification
def verify(args):
    con = sqlite3.connect(args.db)
    q = lambda sql, p=(): con.execute(sql, p).fetchall()
    checks = []

    def check(cid, desc, ok, detail, level="FAIL"):
        checks.append({"id": cid, "check": desc, "result": "PASS" if ok else level, "detail": detail})

    run = q("SELECT run_id, started_utc, finished_utc, status, roots_json, params_json FROM runs ORDER BY run_id DESC LIMIT 1")[0]
    roots = json.loads(run[4])
    build_params = json.loads(run[5] or "{}")
    excl = build_params.get("exclude_under", [])
    excluded_files = set(build_params.get("exclude_files", []))
    check("S0", "latest build completed", run[3] == "complete", f"run {run[0]} {run[1]} -> {run[2]}")

    db_top = {(r[0], r[1]): (r[2], r[3]) for r in q("SELECT collection, rel_path, sha256, size_bytes FROM sources WHERE parent_id IS NULL")}
    disk, new, changed = {}, [], []
    out_dir = os.path.abspath(os.path.dirname(args.db) or ".")
    for name, root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            excluded_dirs = excl if "exclude_files" in build_params else [out_dir] + excl
            if any(under(dirpath, x) for x in excluded_dirs if x != root):
                dirnames[:] = []
                continue
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if normalized_path(full) in excluded_files:
                    continue
                disk[(name, os.path.relpath(full, root))] = full
    for key, full in disk.items():
        if key not in db_top:
            new.append("/".join(key))
        elif sha256_file(full) != db_top[key][0]:
            changed.append("/".join(key))
    absent_roots = [name for name, root in roots if not os.path.isdir(root)]
    missing = ["/".join(k) for k in db_top if k not in disk and k[0] not in absent_roots]
    check("S1", "every file currently on disk is registered (no new, changed, or removed inputs since build)",
          not (new or changed or missing or absent_roots),
          {"new": new, "changed": changed, "removed": missing, "roots_not_on_this_machine": absent_roots}, level="WARN")

    pending = q("SELECT COUNT(*) FROM sources WHERE status='pending'")[0][0]
    check("S2", "every source has a final status", pending == 0, f"{pending} pending")

    bad = []
    for sid, zpath, nmembers in q("SELECT s.source_id, s.staged_path, (SELECT COUNT(*) FROM sources c WHERE c.parent_id=s.source_id) "
                                  "FROM sources s WHERE s.kind='zip' AND s.status='container'"):
        try:
            with zipfile.ZipFile(zpath) as z:
                if len(z.infolist()) != nmembers:
                    bad.append((sid, len(z.infolist()), nmembers))
        except Exception as e:
            bad.append((sid, str(e), nmembers))
    check("S3", "every archive entry (including folders and macOS metadata) is registered", not bad, bad or "all archive entry counts match")

    mism = q("SELECT s.source_id,s.page_count,COUNT(u.unit_id) FROM sources s LEFT JOIN units u ON u.source_id=s.source_id "
             "WHERE s.kind='pdf' AND s.status='indexed' GROUP BY s.source_id "
             "HAVING typeof(s.page_count)!='integer' OR s.page_count<1 OR COUNT(u.unit_id)!=s.page_count "
             "OR COUNT(DISTINCT u.unit_no)!=s.page_count "
             "OR COUNT(CASE WHEN u.unit_type='page' AND typeof(u.unit_no)='integer' AND u.unit_no BETWEEN 1 AND s.page_count THEN 1 END)!=s.page_count")
    check("S4", "every indexed PDF has exactly one unit per page", not mism, mism or "page counts match")

    nu = q("SELECT COUNT(*) FROM units")[0][0]
    try:
        con.execute("INSERT INTO units_fts(units_fts,rank) VALUES('integrity-check',1)")
        con.execute("INSERT INTO units_tri(units_tri,rank) VALUES('integrity-check',1)")
        fts_ok, fts_detail = True, f"{nu} units; both indexes consistent with content"
    except sqlite3.DatabaseError as e:
        fts_ok, fts_detail = False, str(e)
    check("S5", "full-text indexes are consistent with stored text", fts_ok, fts_detail)

    unattempted = q("SELECT COUNT(*) FROM units WHERE ocr_needed=1 AND text_ocr IS NULL AND ocr_error IS NULL")[0][0]
    check("S6", "every unit that needed OCR was attempted (result or recorded error)", unattempted == 0, f"{unattempted} unattempted")

    unflagged = 0
    for uid, text in q("SELECT unit_id, text FROM units"):
        if alnum(text) < NO_TEXT_ALNUM and not q("SELECT 1 FROM flags WHERE unit_id=? AND flag='no_text'", (uid,)):
            unflagged += 1
    check("S7", "every near-empty unit is flagged rather than silently empty", unflagged == 0, f"{unflagged} unflagged")

    rehash_bad = []
    for sid, staged, sha in q("SELECT source_id, staged_path, sha256 FROM sources WHERE parent_id IS NOT NULL AND sha256 IS NOT NULL"):
        if not staged or not os.path.exists(staged) or sha256_file(staged) != sha:
            rehash_bad.append(sid)
    check("S8", "staged archive members still match their recorded SHA-256", not rehash_bad,
          rehash_bad or "all staged members verified", level="WARN")

    ic = q("PRAGMA integrity_check")[0][0]
    check("S9", "SQLite integrity check", ic == "ok", ic)

    probes = []
    for p in args.probe or []:
        rows = q("SELECT l.label, u.unit_type, u.unit_no FROM units_tri t JOIN units u ON u.unit_id=t.rowid "
                 "JOIN source_labels l ON l.source_id=u.source_id WHERE units_tri MATCH ? LIMIT 5", ('"' + p.replace('"', '""') + '"',))
        probes.append({"phrase": p, "hits": [f"{r[0]} {r[1]} {r[2]}" for r in rows]})
    report = {"db": os.path.abspath(args.db), "verified_utc": utcnow(), "checks": checks, "probes": probes,
              "summary": {r: sum(1 for c in checks if c["result"] == r) for r in ("PASS", "WARN", "FAIL")}}
    base = os.path.join(os.path.dirname(os.path.abspath(args.db)), "validation_report")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(f"# Validation report\n\nDatabase: `{os.path.basename(args.db)}`  \nVerified: {report['verified_utc']}  \n"
                f"Result: {report['summary']['PASS']} pass, {report['summary']['WARN']} warn, {report['summary']['FAIL']} fail\n\n")
        for c in checks:
            f.write(f"- **{c['id']} {c['result']}** — {c['check']}. Detail: `{json.dumps(c['detail'])[:600]}`\n")
        if probes:
            f.write("\n## Probe phrases (substring search)\n\n")
            for p in probes:
                f.write(f"- “{p['phrase']}” → {', '.join(p['hits']) or 'no hits'}\n")
    for c in checks:
        print(f"{c['id']} {c['result']:4} {c['check']}")
    for p in probes:
        print(f"probe {p['phrase']!r}: {p['hits'] or 'no hits'}")
    con.close()
    return 1 if report["summary"]["FAIL"] else 0


# ---------------------------------------------------------------- query commands
def connect(db):
    if not os.path.exists(db):
        sys.exit(f"database not found: {db}")
    return sqlite3.connect(db)


def cmd_search(a):
    con = connect(a.db)
    table = "units_tri" if a.substring else "units_fts"
    query = ('"' + a.query.replace('"', '""') + '"') if a.substring else a.query
    sql = (f"SELECT l.label, u.unit_type, u.unit_no, u.method, snippet({table}, 0, '[', ']', ' … ', 16), u.source_id "
           f"FROM {table} JOIN units u ON u.unit_id={table}.rowid JOIN sources s ON s.source_id=u.source_id "
           f"JOIN source_labels l ON l.source_id=u.source_id WHERE {table} MATCH ?")
    params = [query]
    if a.collection:
        sql += " AND s.collection=?"
        params.append(a.collection)
    sql += f" ORDER BY bm25({table}), s.source_id, u.unit_no, u.unit_id LIMIT ?"
    params.append(a.limit)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"query error: {e}  (tip: use --substring for exact phrases with punctuation)")
    for label, ut, un, method, snip, sid in rows:
        print(f"[{sid}] {label} · {ut} {un} · {method}\n    {' '.join(snip.split())}\n")
    if not rows:
        print("no hits")


def cmd_show(a):
    con = connect(a.db)
    src = con.execute("SELECT label FROM source_labels WHERE source_id=?", (a.source_id,)).fetchone()
    if not src:
        sys.exit("no such source")
    print(f"== [{a.source_id}] {src[0]}")
    sql, p = "SELECT unit_type, unit_no, method, ocr_conf, text FROM units WHERE source_id=?", [a.source_id]
    if a.unit:
        sql += " AND unit_no=?"
        p.append(a.unit)
    for ut, un, method, conf, text in con.execute(sql + " ORDER BY unit_no", p):
        print(f"\n--- {ut} {un} · {method}" + (f" · OCR confidence {conf}" if conf is not None else ""))
        print(text)
    for kind, body, author, ev, basis in con.execute("SELECT kind, body, author, evidence_class, basis FROM annotations WHERE source_id=?", (a.source_id,)):
        print(f"\n### annotation ({kind}; {author}; {ev})\n{body}\nbasis: {basis}")


def cmd_sources(a):
    con = connect(a.db)
    sql = "SELECT s.source_id, l.label, s.kind, s.status, COALESCE(s.page_count,''), s.size_bytes FROM sources s JOIN source_labels l ON l.source_id=s.source_id WHERE 1=1"
    p = []
    for col in ("collection", "status", "kind"):
        if getattr(a, col):
            sql += f" AND s.{col}=?"
            p.append(getattr(a, col))
    for row in con.execute(sql + " ORDER BY s.source_id", p):
        print("%5s  %-9s %-13s %4s pp %12s B  %s" % (row[0], row[2], row[3], row[4], row[5], row[1]))


def cmd_cards(a):
    con = connect(a.db)
    for sid, label, pages in con.execute("SELECT s.source_id, l.label, s.page_count FROM sources s JOIN source_labels l ON l.source_id=s.source_id "
                                         "WHERE s.status='indexed' ORDER BY s.source_id"):
        meta = dict(con.execute("SELECT key, value FROM meta WHERE source_id=?", (sid,)).fetchall())
        first = con.execute("SELECT text FROM units WHERE source_id=? ORDER BY unit_no LIMIT 1", (sid,)).fetchone()
        line = next((ln.strip() for ln in (first[0] or "").splitlines() if alnum(ln) >= 8), "") if first else ""
        marks = {}
        for t, v in con.execute("SELECT m.type, m.value FROM markings m JOIN units u ON u.unit_id=m.unit_id WHERE u.source_id=?", (sid,)):
            marks.setdefault(t, [])
            if v not in marks[t] and len(marks[t]) < 5:
                marks[t].append(v)
        print(f"[{sid}] {label}\n    pages: {pages or '-'} · pdf title: {meta.get('Title', '-')} · created: {meta.get('CreationDate', '-')}"
              f"\n    first line (heuristic): {line[:120]}")
        for t in ("declassification", "classification", "fbi_file_number", "usc_citation", "date"):
            if t in marks:
                print(f"    {t}: {' | '.join(marks[t])}")
        print()


def cmd_stats(a):
    con = connect(a.db)
    run = con.execute("SELECT run_id, started_utc, finished_utc, counts_json, tools_json FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    print(f"run {run[0]}: {run[1]} -> {run[2]}")
    print(json.dumps(json.loads(run[3]), indent=2))
    o = con.execute("SELECT COUNT(*), ROUND(AVG(ocr_conf),1), ROUND(SUM(ocr_seconds)/60,1) FROM units WHERE ocr_conf IS NOT NULL").fetchone()
    print(f"OCR units: {o[0]}, mean confidence {o[1]}, total OCR time {o[2]} min")


def cmd_note(a):
    con = connect(a.db)
    if a.action == "add":
        try:
            sb = source_binding(con, a.source_id)
            if sb is None or not a.body.strip() or not a.author.strip() or not a.evidence_class.strip():
                raise ValueError("note requires an existing --source-id, body, author and evidence class")
            uid = None
            if a.unit is not None:
                matches = con.execute("SELECT unit_id FROM units WHERE source_id=? AND unit_no=?", (a.source_id, a.unit)).fetchall()
                if len(matches) != 1:
                    raise ValueError("note --unit must identify exactly one page/chunk number in its source")
                uid = matches[0][0]
            ub = unit_binding(con, uid, a.source_id)
            columns = {r[1] for r in con.execute("PRAGMA table_info(annotations)")}
            for col in ("source_binding_json", "unit_binding_json", "binding_status"):
                if col not in columns:
                    con.execute(f"ALTER TABLE annotations ADD COLUMN {col} TEXT")
            con.execute("INSERT INTO annotations(source_id,unit_id,author,created_utc,kind,evidence_class,body,basis,source_binding_json,unit_binding_json,binding_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (a.source_id, uid, a.author, utcnow(), a.kind, a.evidence_class, a.body, a.basis, sb, ub, "BOUND"))
            con.commit()
            print("annotation added")
        finally:
            con.close()
    else:
        columns = {r[1] for r in con.execute("PRAGMA table_info(annotations)")}
        status = "COALESCE(a.binding_status,'LEGACY_UNCHECKED')" if "binding_status" in columns else "'LEGACY_UNCHECKED'"
        for row in con.execute(f"SELECT a.annotation_id,COALESCE(l.label,'[source unresolved]'),a.kind,a.author,a.evidence_class,a.body,{status} FROM annotations a LEFT JOIN source_labels l ON l.source_id=a.source_id"):
            print(f"#{row[0]} {row[1]} ({row[2]}; {row[3]}; {row[4]}; {row[6]})\n    {row[5][:300]}\n")
        con.close()


def cmd_manifest(a):
    with closing(connect(a.db)) as con:
        row = con.execute("SELECT manifest_json FROM build_manifest LIMIT 1").fetchone()
        if row is None:
            raise ValueError("no embedded build manifest")
        manifest = json.loads(row[0])
    manifest["db_sha256"] = sha256_file(a.db)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print("manifest exported to " + a.out)


def cmd_export(a):
    con = connect(a.db)
    n = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for row in con.execute("SELECT u.unit_id, l.label, s.sha256, u.unit_type, u.unit_no, u.method, u.ocr_conf, u.text "
                               "FROM units u JOIN sources s ON s.source_id=u.source_id JOIN source_labels l ON l.source_id=u.source_id ORDER BY s.source_id, u.unit_no, u.unit_id"):
            f.write(json.dumps(dict(zip(("unit_id", "source", "sha256", "unit_type", "unit_no", "method", "ocr_conf", "text"), row)), ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} units to {a.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="knowledge.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="register and index every file under the given roots")
    b.add_argument("--root", action="append", required=True, help="name=path (repeatable)")
    b.add_argument("--stage", default="stage", help="where archive members are staged, named by SHA-256")
    b.add_argument("--ocr-cache", default="ocr_cache.sqlite")
    b.add_argument("--log", default="build.log")
    b.add_argument("--ocr-dpi", type=int, default=300)
    b.add_argument("--ocr-lang", default="eng")
    b.add_argument("--ocr-psm", type=int, default=3)
    b.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    b.add_argument("--no-ocr", action="store_true")
    b.add_argument("--ocr-budget", type=float, default=0, help="stop starting new OCR work after this many seconds (0 = no limit); results so far stay cached")
    b.add_argument("--ocr-only", action="store_true", help="fill the OCR cache and exit without writing the database (for chunked runs)")
    b.add_argument("--annotations", help="JSON list of annotations to attach")
    b.add_argument("--exclude-under", action="append", help="path whose contents are build artifacts, never inputs")
    b.add_argument("--allow-empty", action="store_true", help="explicitly permit an empty corpus; never permits missing roots")
    v = sub.add_parser("verify", help="check the database against explicit success criteria and the current disk state")
    v.add_argument("--probe", action="append", help="exact phrase expected somewhere in the corpus")
    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--substring", action="store_true", help="exact substring match (trigram index)")
    s.add_argument("--collection")
    s.add_argument("--limit", type=int, default=20)
    sh = sub.add_parser("show", help="print the text of a source")
    sh.add_argument("source_id", type=int)
    sh.add_argument("--unit", type=int)
    so = sub.add_parser("sources", help="list registered sources")
    so.add_argument("--collection")
    so.add_argument("--status")
    so.add_argument("--kind")
    sub.add_parser("cards", help="per-document cards: metadata and detected markings (heuristic)")
    sub.add_parser("stats", help="counts from the latest build")
    n = sub.add_parser("note", help="add or list annotations")
    n.add_argument("action", choices=["add", "list"])
    n.add_argument("--source-id", type=int)
    n.add_argument("--unit", type=int)
    n.add_argument("--author", default="user")
    n.add_argument("--kind", default="note")
    n.add_argument("--evidence-class", default="USER_ASSERTED")
    n.add_argument("--body", default="")
    n.add_argument("--basis", default="")
    e = sub.add_parser("export", help="write every unit with provenance to JSONL")
    e.add_argument("--out", default="knowledge_units.jsonl")
    m = sub.add_parser("manifest", help="export the manifest embedded in a completed 1.0.1 build")
    m.add_argument("--out", default="manifest.json")
    a = ap.parse_args()
    if a.cmd == "build":
        with database_writer(a.db):
            builder = Builder(a)
            try:
                builder.run()
            finally:
                builder.close()
    elif a.cmd == "verify":
        sys.exit(verify(a))
    elif a.cmd == "note" and a.action == "add":
        with database_writer(a.db):
            cmd_note(a)
    else:
        {"search": cmd_search, "show": cmd_show, "sources": cmd_sources, "cards": cmd_cards,
         "stats": cmd_stats, "note": cmd_note, "export": cmd_export, "manifest": cmd_manifest}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # output was piped into a command that stopped reading early (e.g. `| head`)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
