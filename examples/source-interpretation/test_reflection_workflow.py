import unittest
from dataclasses import FrozenInstanceError
from reflection_workflow import Workflow


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.w = Workflow().add_source('jd', 'Python preferred, not required.')

    def test_literal_source_preserved(self):
        text = '  A\n\nB  '
        self.assertEqual(Workflow().add_source('raw', text).sources[0].text, text)

    def test_exact_observation(self):
        w = self.w.add_claim('a', 'jd', 'OBSERVATION', 'Python preferred', [(0, 16)])
        self.assertEqual(w.review('a').meaning_status, 'EXACT_EXCERPT_ONLY')

    def test_inference_cannot_masquerade_as_observation(self):
        with self.assertRaisesRegex(ValueError, 'EXACT_EXCERPT'):
            self.w.add_claim('a', 'jd', 'OBSERVATION', 'Python required', [(0, 16)])

    def test_reference_does_not_prove_meaning(self):
        w = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'Python required', [(0, 16)])
        self.assertEqual(w.review('a').meaning_status, 'NOT_SEMANTICALLY_VERIFIED')

    def test_missing_support_remains_visible(self):
        w = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'Possibly remote work')
        self.assertEqual(w.review('a').span_status, 'NO_SOURCE_SPANS')

    def test_response_never_authorizes_execution(self):
        w = self.w.add_claim('a', 'jd', 'PROPOSED_RESPONSE', 'Send an application')
        r = w.review('a')
        self.assertEqual((r.authority, r.execution_status), ('NONE', 'NOT_EXECUTED'))

    def test_correction_preserves_history_and_old_snapshot(self):
        old = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'Required')
        new = old.add_claim('b', 'jd', 'INTERPRETATION', 'Preferred',
                            supersedes='a', correction_reason='Re-read source')
        self.assertEqual([c.claim_id for c in old.current_claims('jd')], ['a'])
        self.assertEqual([c.claim_id for c in new.current_claims('jd')], ['b'])
        self.assertEqual([c.content for c in new.history], ['Required', 'Preferred'])

    def test_cross_source_correction_refused(self):
        w = self.w.add_source('other', 'x').add_claim('a', 'jd', 'INTERPRETATION', 'x')
        with self.assertRaisesRegex(ValueError, 'INVALID_CORRECTION_TARGET'):
            w.add_claim('b', 'other', 'INTERPRETATION', 'y', supersedes='a', correction_reason='x')

    def test_duplicate_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_SOURCE'):
            self.w.add_source('jd', 'replacement')
        w = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'x')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_CLAIM'):
            w.add_claim('a', 'jd', 'INTERPRETATION', 'y')

    def test_equal_text_keeps_separate_sources(self):
        w = self.w.add_source('other', self.w.sources[0].text)
        for sid in ('jd', 'other'):
            w = w.add_claim(sid, sid, 'INTERPRETATION', 'Same rendering')
        self.assertEqual([c.claim_id for c in w.current_claims('jd')], ['jd'])
        self.assertEqual([c.claim_id for c in w.current_claims('other')], ['other'])

    def test_external_container_mutation_cannot_change_claim(self):
        spans = [[0, 6]]
        w = self.w.add_claim('a', 'jd', 'OBSERVATION', 'Python', spans)
        spans[0][1] = 20
        self.assertEqual(w.history[0].evidence_spans, ((0, 6),))
        with self.assertRaises(FrozenInstanceError):
            w.history[0].content = 'changed'

    def test_malformed_spans_rejected(self):
        for spans in (None, [(False, 2)], [(-1, 2)], [(0, 999)], [(1, 1)], [('0', 2)]):
            with self.subTest(spans=spans), self.assertRaisesRegex(ValueError, 'INVALID_SPAN'):
                self.w.add_claim('a', 'jd', 'INTERPRETATION', 'x', spans)

    def test_correction_requires_reason(self):
        w = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'x')
        with self.assertRaisesRegex(ValueError, 'CORRECTION_REASON_REQUIRED'):
            w.add_claim('b', 'jd', 'INTERPRETATION', 'y', supersedes='a')

    def test_disagreement_survives_without_supersession(self):
        w = self.w.add_claim('a', 'jd', 'INTERPRETATION', 'A')
        w = w.add_claim('b', 'jd', 'INTERPRETATION', 'B')
        self.assertEqual(len(w.current_claims('jd')), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
