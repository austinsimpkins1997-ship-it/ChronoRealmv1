"""Synthetic example: no private documents, credentials, or live actions."""
from reflection_workflow import Workflow

source = 'Python preferred, not required.'
w = Workflow().add_source('synthetic-jd', source)
print('SOURCE:', source)
try:
    w.add_claim('bad-quote', 'synthetic-jd', 'OBSERVATION',
                'Python required', [(0, 16)])
except ValueError as error:
    print('REJECTED AS EXACT OBSERVATION:', error)
w = w.add_claim('reading-1', 'synthetic-jd', 'INTERPRETATION',
                'Python required', [(0, 16)])
print('INTERPRETATION STATUS:', w.review('reading-1').meaning_status)
w = w.add_claim('reading-2', 'synthetic-jd', 'INTERPRETATION',
                'Python preferred', [(0, 16)], supersedes='reading-1',
                correction_reason='Preferred was incorrectly described as required')
print('HISTORY:', [(c.claim_id, c.content) for c in w.history])
print('CURRENT:', [c.claim_id for c in w.current_claims('synthetic-jd')])
print('AUTHORITY:', w.review('reading-2').authority)
assert len(w.history) == 2
assert w.current_claims('synthetic-jd')[0].claim_id == 'reading-2'
