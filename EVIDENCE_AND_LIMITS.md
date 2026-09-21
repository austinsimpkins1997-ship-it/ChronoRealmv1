# Evidence scope and limits

- The packaged source hash is 4a9d998099f141676dd30afe82c806c96b99e644aaafd1b2cfb826cfe03a4a2d.
- 41 discovered / 39 passed / 2 skipped applies to the recorded local packaging run.
- Skips: native PDF integration and OCR integration. Mocked extraction tests verify
  subprocess handling, not Poppler/Tesseract accuracy.
- Six recorded mutations were detected by assertion failures, not syntax failures.
  Repeated test invocations do not count as additional distinct coverage.
- Separate AI review is not an independent human audit. There is no verified dataset
  or count of cross-model disagreements in this release.
- Hashing before later extraction reopens an input leaves a possible source-snapshot
  race. No protection against arbitrary concurrent writers is claimed.
- UTF-16/32 and non-UTF-8 decoding remain outside the revised UTF-8 contract.
- Physical page positions are not printed page labels. OCR and native text may disagree;
  their combined search representation is not independent corroboration.
- No claim of full-runtime acceptance, deployment, certification or universal correctness.
- Private archives, live databases, browser state, resumes and Drive inventories are excluded.
- The original local packages remain historical snapshots; this public repository's
  documentation and evidence additions do not silently update those archives.
