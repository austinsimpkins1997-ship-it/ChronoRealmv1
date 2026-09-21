# ChronoGit: source-first kb evidence release

ChronoGit contains the reviewed **kb document-indexing component** of ChronoRealm.
It is not the full ChronoRealm runtime or a claim of production readiness.

## What this demonstrates

kb indexes documents into SQLite and preserves source IDs, hashes, page/unit positions,
extraction status and separately labeled annotations. Extracted text is a representation;
a valid hash does not establish that a document or interpretation is true.

The reviewed changes address deterministic export order, stable search tie-breakers,
Unicode text recognition, explicit UTF-8 decoding errors, office extraction labeling,
and rejection of failed PDF extraction as successful text.

## Reproduce

Python with SQLite FTS5 is required. The recorded run used Python 3.14.7 on Windows.
From the repository root:

```sh
python -B -m unittest discover -s tests -v
python kb.py --help
```

Optional integration prerequisites: Poppler (`pdfinfo`, `pdftotext`, `pdftoppm`),
Tesseract, and Python reportlab/Pillow. Missing tools may skip integration tests.
Do not interpret skipped tests as passed. Other environments may produce different totals.

## Recorded evidence

The packaging run discovered **41 tests: 39 passed, 2 skipped, 0 failed**.
See [the complete log](PACKAGING_TESTS.log) and [environment/result metadata](PACKAGING_VERIFICATION.json).
Those files describe the historical local packaging run, before publication.

[evidence/](evidence/) includes six recorded controlled mutations: two ordering and
four parsing safeguards were removed in scratch copies and relevant assertions failed.
These are historical falsification logs, not an automated mutation runner. Sanitization
and original/published digests are recorded in [SANITIZATION.json](evidence/SANITIZATION.json).
No private source documents are included. Tests use synthetic fixtures.

See [EVIDENCE_AND_LIMITS.md](EVIDENCE_AND_LIMITS.md) for exact boundaries.
[PACKAGE_MANIFEST.json](PACKAGE_MANIFEST.json) pins the public files;
[SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json) pins the source revision.

## Ownership and license

Copyright (C) 2026 Austin Simpkins and Chandler Lee Middlebrooks.
Austin directs equal 50/50 project ownership; see [OWNERSHIP.md](OWNERSHIP.md).
The kb source retains **GPL-3.0-or-later**; [LICENSE](LICENSE) contains GPLv3.
AI assistants contributed implementation, tests and documentation under human direction.
This is not an independent professional audit or a provider endorsement.

## Contributors and release responsibility

According to Austin Simpkins, Chandler Lee Middlebrooks performed most of
ChronoRealm's early mathematical work and coded its initial framework. Austin
recalls the main early development period ending approximately August 8-12,
around the version 17b freeze. The exact date and its relationship to that freeze
remain unverified. Chandler contributed on a few occasions afterward; Austin
expects his involvement to increase. Future participation is an expectation,
not a completed contribution.

Austin Simpkins leads release coordination and publication, under the arrangement
he reports discussing with Chandler. This task responsibility does not change
the stated 50/50 project ownership. Early framework credit does not imply
authorship of every later implementation or of all code in this kb component.

## Reporting issues

Include the commit ID, Python/SQLite versions, command, expected result and actual result.
Use a minimal synthetic reproduction; do not post private documents, credentials or
personal databases. Do not infer source truth, identity authentication or permission
from passing tests or agreement between AI reviewers.
