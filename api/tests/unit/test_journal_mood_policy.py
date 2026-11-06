from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.adapters.http.journal import journal_router
from api.domain.journal import JournalPolicy


MOODS = {'calm': '편안해요', 'happy': '기뻐요', 'thinking': '생각이 많아요',
         'settled': '차분해요', 'talk': '이야기하고 싶어요'}
POLICY = JournalPolicy(frozenset(MOODS.values()), 4000)


def test_v1_policy_keeps_legacy_order_and_exposes_explicit_codes_when_injected():
    # Sorted labels deliberately differ from the semantic v2 code order.
    assert sorted(POLICY.moods)[0] != MOODS['calm']
    app = FastAPI()
    app.include_router(journal_router(Mock(), POLICY, uuid4, mood_codes=MOODS))
    with TestClient(app) as client:
        response = client.get('/v1/journal-policy')
    assert response.status_code == 200
    assert response.json() == {'moods': sorted(POLICY.moods), 'maxBodyLength': 4000, 'moodCodes': MOODS}
    assert response.json()['moodCodes']['calm'] == '편안해요'
    assert response.json()['moodCodes']['happy'] == '기뻐요'


def test_existing_three_argument_router_has_the_exact_original_response():
    app = FastAPI()
    app.include_router(journal_router(Mock(), POLICY, uuid4))
    with TestClient(app) as client:
        assert client.get('/v1/journal-policy').json() == {'moods': sorted(POLICY.moods), 'maxBodyLength': 4000}


@pytest.mark.parametrize('mapping', [
    {'calm': '편안해요'}, {**MOODS, 'unknown': '추가'},
    {**MOODS, 'happy': '편안해요'}, {**MOODS, 'talk': '다른 목록'}, {**MOODS, 'happy': ''},
])
def test_incoherent_mapping_is_rejected_at_composition_instead_of_mislabeling(mapping):
    with pytest.raises(ValueError, match='mood'):
        journal_router(Mock(), POLICY, uuid4, mood_codes=mapping)


def test_injected_mapping_is_a_snapshot_and_cannot_drift_after_router_creation():
    mapping = dict(MOODS)
    router = journal_router(Mock(), POLICY, uuid4, mood_codes=mapping)
    mapping['calm'] = 'wrong label'
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/v1/journal-policy').json()['moodCodes'] == MOODS
