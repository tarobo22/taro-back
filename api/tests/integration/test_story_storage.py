from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from api.adapters.story_catalog import load_story_catalog
from api.adapters.story_postgres import PostgresStoryUnitOfWork
from api.application.story import StoryService
from api.domain.story import StoryConflict, StoryNotFound
from api.domain.data_privacy import OwnerDeleting

pytestmark = pytest.mark.integration


@pytest.fixture
def story_storage(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    return StoryService(lambda: PostgresStoryUnitOfWork(pg_pool), load_story_catalog()), pg_pool, owners


def test_concurrent_start_and_same_choice_identity_commit_once_with_real_owner_lock(story_storage):
    service, pool, owners = story_storage; owner = owners[0]; key = uuid4()
    body = {'episode_id': 'window-pause', 'episode_version': '1.0.0'}
    with ThreadPoolExecutor(max_workers=2) as executor:
        starts = list(executor.map(lambda _: service.start(owner, body, key), range(2)))
    assert starts[0]['id'] == starts[1]['id']; run = starts[0]; choice_key = uuid4()
    command = {'expected_version': 1, 'node_id': run['current_node']['id'], 'answer': True}
    with ThreadPoolExecutor(max_workers=2) as executor:
        choices = list(executor.map(lambda _: service.choose(owner, run['id'], command, choice_key), range(2)))
    assert choices[0] == choices[1] and choices[0]['choice_count'] == 1
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM story_runs WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1


def test_database_owner_isolation_stale_version_and_deleting_gate(story_storage):
    service, pool, owners = story_storage
    run = service.start(owners[0], {'episode_id': 'window-pause', 'episode_version': '1.0.0'}, uuid4())
    with pytest.raises(StoryNotFound): service.get(owners[1], run['id'])
    service.choose(owners[0], run['id'], {'expected_version': 1, 'node_id': run['current_node']['id'], 'answer': False}, uuid4())
    with pytest.raises(StoryConflict): service.choose(owners[0], run['id'], {'expected_version': 1, 'node_id': run['current_node']['id'], 'answer': True}, uuid4())
    with pool.connection() as connection:
        connection.execute("INSERT INTO data_privacy_profiles(owner_id,state) VALUES(%s,'deleting')", (owners[0],))
    with pytest.raises(OwnerDeleting): service.get(owners[0], run['id'])


def test_restart_and_privacy_callbacks_use_the_callers_transaction_and_preserve_another_owner(story_storage):
    service, pool, owners = story_storage
    body = {'episode_id': 'window-pause', 'episode_version': '1.0.0'}
    first, other = service.start(owners[0], body, uuid4()), service.start(owners[1], body, uuid4())
    restarted = service.restart(owners[0], first['id'], {'expected_version': 1}, uuid4())
    assert restarted['id'] != first['id'] and service.progress(owners[0], 'window-pause')['run']['id'] == restarted['id']
    with pool.connection() as connection:
        exported = service.export_owner(connection, owners[0])['story_runs']
        assert {row['id'] for row in exported} == {first['id'], restarted['id']}
        service.erase_owner(connection, owners[0])
    assert service.progress(owners[0], 'window-pause') == {'run': None}
    assert service.get(owners[1], other['id'])['id'] == other['id']
