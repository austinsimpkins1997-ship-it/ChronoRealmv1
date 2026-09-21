# Where did the source stop and interpretation begin?

A small ChronoRealm study example, prepared with AI assistance under Austin's
direction. This is a prototype demonstration, not a deployed protection or an
independent audit. All input here is synthetic.

Source: **Python preferred, not required.**

Incorrect interpretation: **Python required.**

The program rejects that wording when submitted as an exact source observation.
It can retain it as an interpretation, explicitly NOT_SEMANTICALLY_VERIFIED.
A correction adds another claim rather than deleting the first. Review grants
no execution authority. The semantic correction is supplied by the example's
author; the program does not discover it independently.

## Run it

Python 3.10 or later; standard library only. From this directory:

```
python -B demo.py
python -B -m unittest test_reflection_workflow -v
```

See demo-output.txt, test-results.txt and verification.json for the actual local
run, interpreter, exit codes and file digests. A digest identifies bytes; it does
not authenticate the author or prove correctness.

## What to review

Can you distinguish original text, literal excerpt, interpretation, and correction?
Can a test demonstrate a regression if those distinctions are lost?
What is the smallest useful improvement to this example?

## Limits

Exact quotation does not prove source truth, contextual completeness, or semantic
entailment. Citing a span does not prove an interpretation. Direct dataclass
construction bypasses the supported methods' validation. There is no hostile-input
ingestion protection, authenticated authorship, durable history retention, concurrency
control, or execution integration. Python frozen dataclasses are not a security
boundary. Users can discard snapshots. No LLM evaluator is included.

The 14 tests exercise this narrow prototype only. They are not the broader
ChronoRealm suite. This packet does not change native canon or address its bypass.

## Context and credit

Dan Kornas's writing encouraged a bounded, reviewable learning example:
- https://dankornas.substack.com/p/hello-world
- https://dankornas.substack.com/p/the-prototype-loop-from-hell

These are contextual sources, not validation of this code. No article text,
personal documents, private conversation, or external source corpus is included.
Prepared for austinsimpkins1997-ship-it/ChronoRealmv1 under examples/source-interpretation.
The verification metadata records the local run before upload, not a deployment.
The repository license applies; this is an example, not part of the earlier kb
packaging manifest. See REVIEW_SCOPE.md in the published folder.
