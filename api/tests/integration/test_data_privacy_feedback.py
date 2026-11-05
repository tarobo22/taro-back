from uuid import uuid4

import pytest

from api.tests.integration.test_data_privacy_profile_catalog import data_context


pytestmark = pytest.mark.integration


def test_feedback_is_durable_idempotent_and_owner_scoped(data_context):
    client, current, service, owners, pool = data_context
    body = {'rating': 'helpful', 'reason': 'clear', 'comment': 'A useful next step'}
    headers = {'X-Idempotency-Key': str(uuid4())}
    first = client.post('/v2/feedback', json=body, headers=headers)
    assert first.status_code == 201
    assert first.json() == client.post('/v2/feedback', json=body, headers=headers).json()
    assert client.post('/v2/feedback', json={**body, 'rating': 'unhelpful'}, headers=headers).status_code == 409
    with pool.connection() as connection:
        row = connection.execute('SELECT owner_id,rating,reason,comment FROM data_privacy_feedback WHERE id=%s',
                                 (first.json()['id'],)).fetchone()
        assert row == {'owner_id': owners[0], **body}
        assert connection.execute('SELECT count(*) AS n FROM data_privacy_feedback WHERE owner_id=%s',
                                  (owners[0],)).fetchone()['n'] == 1
    current[0] = owners[1]
    other = client.post('/v2/feedback', json=body, headers=headers)
    assert other.status_code == 201 and other.json()['id'] != first.json()['id']


def test_feedback_target_requires_ownership(data_context):
    client, current, service, owners, pool = data_context
    reading_id = uuid4()
    with pool.connection() as connection:
        connection.execute('''INSERT INTO tarot_readings(id,session_id,topic,question,card_ids,result)
            VALUES(%s,%s,'daily','',[0],'{}')'''.replace('[0]', 'ARRAY[0]'), (reading_id, owners[0]))
    body = {'rating': 'helpful', 'reason': 'clear', 'reading_id': str(reading_id)}
    assert client.post('/v2/feedback', json=body, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 201
    current[0] = owners[1]
    headers = {'X-Idempotency-Key': str(uuid4())}
    assert client.post('/v2/feedback', json=body, headers=headers).status_code == 404
    assert client.post('/v2/feedback', json={**body, 'reading_id': str(uuid4())}, headers=headers).status_code == 404
    assert client.post('/v2/feedback', json={'rating': 'helpful', 'reason': 'clear',
                                           'turn_id': str(uuid4())}, headers=headers).status_code == 404


@pytest.mark.parametrize('values', [
    {'rating': 'other', 'reason': 'clear'}, {'rating': 'helpful', 'reason': ''},
    {'rating': 'helpful', 'reason': ' '}, {'rating': 'helpful', 'reason': 'x' * 101},
    {'rating': 'helpful', 'reason': 'clear', 'comment': 'x' * 501},
    {'rating': 'helpful', 'reason': 'clear', 'reading_id': str(uuid4()), 'turn_id': str(uuid4())},
    {'rating': 'helpful', 'reason': 'clear', 'extra': True},
])
def test_feedback_validates_policy_input_before_persistence(data_context, values):
    client, current, service, owners, pool = data_context
    assert client.post('/v2/feedback', json=values, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 422
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM data_privacy_feedback WHERE owner_id=%s',
                                  (owners[0],)).fetchone()['n'] == 0


def test_feedback_requires_nonconflicting_request_identity_and_enforces_policy_limit(data_context):
    client, current, service, owners, pool = data_context
    body = {'rating': 'unhelpful', 'reason': 'other'}
    assert client.post('/v2/feedback', json=body).status_code == 422
    assert client.post('/v2/feedback', json=body, headers={'X-Idempotency-Key': 'invalid'}).status_code == 422
    assert client.post('/v2/feedback', json=body, headers={'X-Idempotency-Key': str(uuid4()),
                        'Idempotency-Key': str(uuid4())}).status_code == 422
    alias = {'Idempotency-Key': str(uuid4())}
    first = client.post('/v2/feedback', json=body, headers=alias)
    assert first.status_code == 201
    for _ in range(service.policy.feedback_daily_limit - 1):
        assert client.post('/v2/feedback', json=body, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 201
    assert client.post('/v2/feedback', json=body, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 429
    # Replays remain available at the limit and never create another row.
    assert client.post('/v2/feedback', json=body, headers=alias).json() == first.json()
