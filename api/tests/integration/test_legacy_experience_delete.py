from uuid import uuid4

import pytest

from api.tests.integration.test_experience_http import experience

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('kind', ['reading', 'counsel'])
def test_legacy_result_delete_is_owned_and_the_original_receipt_cannot_restore_it(experience, owners, kind):
    client, current, topic = experience
    endpoint = '/v1/readings' if kind == 'reading' else '/v1/counsel'
    body = {'topic': topic, 'requestId': str(uuid4()), **(
        {'cardIds': [0], 'spreadId': 'one'} if kind == 'reading' else {'question': '작은 계획을 세울게요.'})}
    saved = client.post(endpoint, json=body).json()
    current[0] = owners[1]
    assert client.delete(endpoint + '/' + saved['id']).status_code == 404
    current[0] = owners[0]
    assert len(client.get(endpoint).json()) == 1
    assert client.delete(endpoint + '/' + saved['id']).status_code == 204
    assert client.get(endpoint).json() == []
    assert client.post(endpoint, json=body).status_code == 409
    assert client.get(endpoint).json() == []


def test_deleting_a_legacy_parent_detaches_the_owned_followup_without_erasing_its_text(experience):
    client, _, topic = experience
    parent = client.post('/v1/counsel', json={'topic': topic, 'question': '처음 이야기', 'requestId': str(uuid4())}).json()
    child = client.post('/v1/counsel', json={'topic': topic, 'question': '남길 이어진 이야기',
        'parentId': parent['id'], 'requestId': str(uuid4())}).json()
    assert client.delete('/v1/counsel/' + parent['id']).status_code == 204
    remaining = client.get('/v1/counsel').json()
    assert [row['id'] for row in remaining] == [child['id']]
    assert remaining[0]['question'] == '남길 이어진 이야기' and remaining[0]['parentId'] is None


def test_deleted_legacy_daily_is_removed_but_explicit_new_draw_keeps_the_same_cards(experience):
    client, _, topic = experience
    body = {'topic': topic, 'day': '2026-11-06'}
    original = client.post('/v1/daily', json=body).json()
    assert client.delete('/v1/readings/' + original['id']).status_code == 204
    assert client.get('/v1/readings').json() == []
    recreated = client.post('/v1/daily', json=body).json()
    assert recreated['cards'] == original['cards']
