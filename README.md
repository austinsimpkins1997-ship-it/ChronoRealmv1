# kb — provenance-first personal knowledge base

Point it at folders of documents. It registers every file — including files inside zip archives, macOS metadata files, empty and corrupt files — and builds a searchable SQLite database where every piece of text traces back to its file, page, and SHA-256 hash. Nothing is silently dropped: anything it can't read is recorded with a status and the reason.

## What it does
- **Extracts text** from PDFs page by page, and OCRs pages that have no usable text layer (tesseract, 300 dpi). OCR results are cached, so rebuilds are fast.
- **Opens archives** (nested up to three levels) with path-traversal and zip-bomb guards; members are staged under their hash, never their name.
- **Indexes two ways:** ranked word search, and exact substring search that keeps punctuation (useful for file numbers and citations).
- **Flags what needs a human:** low OCR confidence, near-empty pages, duplicates, empty files, personal-data patterns (counts only — values aren't copied).
- **Detects markings** as heuristics, not facts: dates, classification and declassification lines, FBI file numbers, U.S. Code citations, URLs.
- **Keeps notes separate from sources:** annotations carry an author and an evidence class, so your notes and model output never pass for the documents themselves.
- **Verifies itself:** ten checks, plus exact-phrase probes, and a warning whenever files on disk have changed since the build.

## Requirements
Python 3.9+ (standard library only; SQLite 3.34+ for substring search), poppler-utils (`pdfinfo`, `pdftotext`, `pdftoppm`), tesseract. Optional: `extract-text` for office formats.

## Quick start
```
python3 kb.py --db knowledge.db build --root docs=/path/to/documents --root notes=/path/to/notes
python3 kb.py --db knowledge.db verify --probe "an exact phrase you expect"
python3 kb.py --db knowledge.db search "query words"
python3 kb.py --db knowledge.db search --substring "56D-SF-4662117"
python3 kb.py --db knowledge.db sources
python3 kb.py --db knowledge.db show SOURCE_ID --unit PAGE
python3 kb.py --db knowledge.db cards
python3 kb.py --db knowledge.db note add --source-id 3 --body "..." --basis "..."
python3 kb.py --db knowledge.db export --out units.jsonl
```
A build writes to a temporary file and swaps it in only on success, so a failed build never replaces a working database.

## Validation checks
S0 build completed · S1 disk matches the database (new, changed, removed files) · S2 every source has a final status · S3 every archive entry registered · S4 one unit per PDF page · S5 search indexes consistent with stored text · S6 every page needing OCR was attempted · S7 near-empty pages flagged · S8 staged archive copies match their hashes · S9 SQLite integrity.

## Tests

```
python3 -m unittest -v tests/test_kb.py
```

Eleven tests check the claims above against real behavior: every file gets a final status and reason (including empty files, corrupt archives, folders and macOS metadata), duplicates are indexed once, path-traversal and zip-bomb archives are caught, nested archives stop at the depth limit, PDF pages keep their page numbers and hashes, both searches work, personal-data patterns are counted without copying values, a failed build leaves the previous database untouched, `verify` runs its ten checks and warns when files change, and OCR reads images and reuses its cache. PDF tests need `reportlab`; OCR tests need Pillow and tesseract. Without them, those tests are skipped.

## Limits
OCR is weak on handwriting and heavily redacted scans — check the page image there. Markings are pattern matches. The personal-data scan finds patterns, not names. It indexes text; it doesn't judge whether a document's claims are true.

## About

kb is my first public project. It applies ideas from **ChronoRealm**, an AI governance framework I co-own with **Chandler Lee Middlebrooks**. I came to ChronoRealm in July 2026 through my thinking about depth in sound, wrote *Resonance of Soul*, and then asked Chandler to join. Chandler built the mathematics and most of the code behind ChronoRealm's framework; I worked on the philosophy, with him and with AI. ChronoRealm's core rules — keep a source separate from any summary of it, record what couldn't be read instead of hiding it, and label who said what — are the rules kb applies to document archives. ChronoRealm wouldn't exist without Chandler, and kb wouldn't exist without ChronoRealm.

I designed and directed kb; the code was written with Claude (Anthropic). I'm new to this field, learning as I go, and looking for a career in AI — especially reliability and data provenance. Feedback and issues are welcome, and you can reach me on LinkedIn: https://www.linkedin.com/in/austin-simpkins-692a533b2/

## Related project

**The Ledger** — a satirical civic journal that catalogs the legal loopholes behind modern wealth accumulation. Each entry lays out the gap, how it works, why it's broken, and the fix, with its source. Built by Austin Simpkins: https://ledger-loop-logic.base44.app

## Support future research

kb is free and will stay free. If it's useful to you, you can help fund the next round of research through PayPal: https://paypal.me/DevLoreApps. Contributions aren't tax-deductible.

## License

Copyright (C) 2026 Austin Simpkins and Chandler Lee Middlebrooks.

kb is free software under the GNU General Public License, version 3 or later (GPL-3.0-or-later). You can use, study, share and change it. Anyone who distributes a modified version must share its source under the same license, so every version stays open to inspection. See LICENSE for the full terms.
