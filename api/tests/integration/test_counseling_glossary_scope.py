from pathlib import Path
from uuid import UUID, uuid4

import pytest

from api.adapters.counseling_postgres import PostgresCounselingStore
from api.application.counseling import CounselingService


pytestmark = pytest.mark.integration


def test_forget_invalidates_references_beyond_the_visible_list_without_touching_unrelated_sessions(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))

    owner, other_owner = owners
    store = PostgresCounselingStore(pg_pool)
    service = CounselingService(store, None, [])
    definition = service.define(owner, '반짝', '우리 독서 모임')
    unrelated_history = [
        {'role': 'user', 'text': '오늘은 반짝이는 물건을 봤어요.'},
        {'role': 'assistant', 'text': '눈에 들어온 게 있었군요.'},
    ]
    drawn_cards = [{'card_id': 2}]
    draw = {'id': str(uuid4())}
    earlier_state = {
        'phase': 'advice',
        'cards': drawn_cards,
        'draw': draw,
        'result': {'advice': '독서 모임에서 관심사를 나눠 보세요.'},
        'sources': [{'id': f"personal:{definition['id']}", 'kind': 'user_definition'}],
        'history': unrelated_history + [
            {'role': 'user', 'text': '반짝에 다시 가 볼까요?'},
            {'role': 'assistant', 'text': '독서 모임에서 관심사를 나눠 보세요.'},
        ],
    }
    unrelated_state = {
        'phase': 'advice', 'cards': drawn_cards, 'draw': draw,
        'result': {'advice': '그때의 느낌을 떠올려 보세요.'},
        'history': unrelated_history,
    }
    with store.transaction(owner) as repo:
        earlier = repo.create_session(owner, uuid4(), 'care', earlier_state, uuid4())
        for _ in range(50):
            repo.create_session(owner, uuid4(), 'care', {}, uuid4())
        unrelated = repo.create_session(owner, uuid4(), 'care', unrelated_state, uuid4())
    with store.transaction(other_owner) as repo:
        other = repo.create_session(other_owner, uuid4(), 'care', unrelated_state, uuid4())

    assert all(row['id'] != str(earlier['id']) for row in service.list(owner))
    service.forget(owner, UUID(definition['id']))

    with store.transaction(owner) as repo:
        earlier_after = repo.session(owner, earlier['id'])
        unrelated_after = repo.session(owner, unrelated['id'])
    with store.transaction(other_owner) as repo:
        assert repo.session(other_owner, other['id'])['state'] == unrelated_state

    assert service.glossary(owner) == []
    state = earlier_after['state']
    assert state['phase'] == 'listening'
    assert not state.get('result') and not state.get('sources')
    assert state['cards'] == drawn_cards and state['draw'] == draw
    assert state['history'] == earlier_state['history'][:-1]
    assert unrelated_after['version'] == unrelated['version']
    assert unrelated_after['state'] == unrelated_state
