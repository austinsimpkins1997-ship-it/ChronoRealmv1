"""In-memory source/correction prototype. No network, persistence, or execution API."""
from dataclasses import dataclass, replace
from typing import Literal

Kind = Literal['OBSERVATION', 'INTERPRETATION', 'PROPOSED_RESPONSE']


@dataclass(frozen=True)
class Source:
    source_id: str
    text: str


@dataclass(frozen=True)
class Claim:
    claim_id: str
    source_id: str
    kind: Kind
    content: str
    evidence_spans: tuple[tuple[int, int], ...] = ()
    supersedes: str | None = None
    correction_reason: str | None = None


@dataclass(frozen=True)
class Review:
    claim_id: str
    span_status: str
    meaning_status: str
    authority: str = 'NONE'
    execution_status: str = 'NOT_EXECUTED'


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class Workflow:
    sources: tuple[Source, ...] = ()
    history: tuple[Claim, ...] = ()

    def add_source(self, source_id: str, text: str):
        if not _nonblank(source_id) or not isinstance(text, str):
            raise ValueError('INVALID_SOURCE')
        if any(s.source_id == source_id for s in self.sources):
            raise ValueError('DUPLICATE_SOURCE_ID')
        return replace(self, sources=self.sources + (Source(source_id, text),))

    def add_claim(self, claim_id, source_id, kind, content, evidence_spans=(),
                  supersedes=None, correction_reason=None):
        if not _nonblank(claim_id) or not _nonblank(content):
            raise ValueError('INVALID_CLAIM')
        if any(c.claim_id == claim_id for c in self.history):
            raise ValueError('DUPLICATE_CLAIM_ID')
        source = next((s for s in self.sources if s.source_id == source_id), None)
        if source is None:
            raise ValueError('UNKNOWN_SOURCE')
        if kind not in ('OBSERVATION', 'INTERPRETATION', 'PROPOSED_RESPONSE'):
            raise ValueError('INVALID_KIND')
        # Copy incoming containers so later caller mutations cannot alter history.
        try:
            spans = tuple(tuple(span) for span in evidence_spans)
        except TypeError as exc:
            raise ValueError('INVALID_SPAN') from exc
        for span in spans:
            if (len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= len(source.text)):
                raise ValueError('INVALID_SPAN')
        # Observation means a literal excerpt, not an automated semantic verdict.
        if kind == 'OBSERVATION' and (len(spans) != 1 or
                source.text[spans[0][0]:spans[0][1]] != content):
            raise ValueError('OBSERVATION_MUST_BE_EXACT_EXCERPT')
        if supersedes is not None:
            prior = next((c for c in self.history if c.claim_id == supersedes), None)
            if prior is None or prior.source_id != source_id:
                raise ValueError('INVALID_CORRECTION_TARGET')
            if not _nonblank(correction_reason):
                raise ValueError('CORRECTION_REASON_REQUIRED')
            if any(c.supersedes == supersedes for c in self.history):
                raise ValueError('CORRECTION_TARGET_ALREADY_SUPERSEDED')
        elif correction_reason is not None:
            raise ValueError('CORRECTION_WITHOUT_TARGET')
        claim = Claim(claim_id, source_id, kind, content, spans,
                      supersedes, correction_reason)
        return replace(self, history=self.history + (claim,))

    def current_claims(self, source_id):
        if not any(s.source_id == source_id for s in self.sources):
            raise ValueError('UNKNOWN_SOURCE')
        replaced = {c.supersedes for c in self.history if c.source_id == source_id}
        return tuple(c for c in self.history
                     if c.source_id == source_id and c.claim_id not in replaced)

    def review(self, claim_id):
        claim = next((c for c in self.history if c.claim_id == claim_id), None)
        if claim is None:
            raise ValueError('UNKNOWN_CLAIM')
        return Review(claim_id,
                      'REFERENCED' if claim.evidence_spans else 'NO_SOURCE_SPANS',
                      'EXACT_EXCERPT_ONLY' if claim.kind == 'OBSERVATION'
                      else 'NOT_SEMANTICALLY_VERIFIED')
