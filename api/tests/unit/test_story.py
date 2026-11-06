from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api.adapters.story_catalog import load_story_catalog
from api.application.story import StoryService
from api.domain.story import StoryCatalog, StoryConflict, StoryInvalid, StoryNotFound
from api.domain.data_privacy import OwnerDeleting


class State:
    def __init__(self):
        self.rows, self.requests, self.deleting = {}, {}, set()


class Repository:
    def __init__(self, state): self.state = state
    def lock(self, owner): pass
    def active(self, owner):
        if owner in self.state.deleting: raise OwnerDeleting('owner_deleting')
    def request(self, owner, key): return self.state.requests.get((owner, key))
    def remember(self, owner, key, fingerprint, run_id): self.state.requests[owner, key] = {'fingerprint': fingerprint, 'run_id': run_id}
    def get(self, owner, run_id):
        row = self.state.rows.get(run_id)
        return deepcopy(row) if row and row['owner_id'] == owner else None
    def latest(self, owner, episode):
        rows = [r for r in self.state.rows.values() if r['owner_id'] == owner and r['episode_id'] == episode]
        return deepcopy(rows[-1]) if rows else None
    def create(self, owner, run_id, values):
        row = {**deepcopy(values), 'id': run_id, 'owner_id': owner, 'version': 1}
        self.state.rows[run_id] = row
        return deepcopy(row)
    def save(self, owner, run_id, expected, values):
        row = self.get(owner, run_id)
        if row is None or row['version'] != expected: return None
        row.update(deepcopy(values), version=expected + 1)
        self.state.rows[run_id] = row
        return deepcopy(row)
    def all_owned(self, owner): return [deepcopy(r) for r in self.state.rows.values() if r['owner_id'] == owner]
    def erase_owner(self, owner):
        self.state.rows = {k: v for k, v in self.state.rows.items() if v['owner_id'] != owner}
        self.state.requests = {k: v for k, v in self.state.requests.items() if k[0] != owner}


class UnitOfWork:
    def __init__(self, state): self.state, self.story = state, Repository(state)
    def __enter__(self): self.before = deepcopy(self.state.__dict__); return self
    def __exit__(self, *exc):
        if exc[0]: self.state.__dict__.update(self.before)
    def commit(self): pass
    def repository_for(self, _transaction): return Repository(self.state)


@pytest.fixture
def story():
    state, owner = State(), uuid4()
    service = StoryService(lambda: UnitOfWork(state), load_story_catalog(), clock=lambda: datetime(2026, 11, 6, tzinfo=timezone.utc))
    return service, state, owner


def begin(service, owner, key=None):
    return service.start(owner, {'episode_id': 'window-pause', 'episode_version': '1.0.0'}, key or uuid4())


def choose(service, owner, run, answer, key=None):
    return service.choose(owner, run['id'], {'expected_version': run['version'], 'node_id': run['current_node']['id'], 'answer': answer}, key or uuid4())


def test_versioned_original_episode_has_three_choices_and_three_complete_gentle_endings(story):
    service, _, owner = story
    endings = set()
    for answers in [(True, True, True), (True, True, False), (False, False, True)]:
        run = begin(service, owner)
        if run['state'] != 'active': run = service.restart(owner, run['id'], {'expected_version': run['version']}, uuid4())
        for answer in answers: run = choose(service, owner, run, answer)
        assert run['state'] == 'completed' and run['choice_count'] == 3 and run['current_node'] is None
        assert all(run['ending'][key] for key in ('id', 'title', 'body', 'action'))
        endings.add(run['ending']['id'])
    assert len(endings) == 3


@pytest.mark.parametrize('change', ['missing_target', 'cycle', 'unreachable', 'too_short', 'duplicate'])
def test_invalid_episode_graphs_are_rejected_before_any_progress_can_start(change):
    value = load_story_catalog().export_definition()
    nodes = value['episodes'][0]['nodes']
    first = nodes[0]
    if change == 'missing_target': first['yes'] = 'missing'
    if change == 'cycle': first['yes'] = first['id']
    if change == 'unreachable': nodes.append({'id': 'unused', 'kind': 'ending', 'title': 't', 'body': 'b', 'action': 'a'})
    if change == 'too_short': first['yes'] = next(n['id'] for n in nodes if n['kind'] == 'ending')
    if change == 'duplicate': nodes.append(deepcopy(first))
    with pytest.raises(StoryInvalid): StoryCatalog.from_mapping(value)


def test_start_resumes_existing_progress_and_replays_without_creating_an_implicit_restart(story):
    service, state, owner = story
    key = uuid4(); first = begin(service, owner, key)
    advanced = choose(service, owner, first, True)
    assert begin(service, owner, key)['version'] == advanced['version']
    assert begin(service, owner)['id'] == first['id'] and len(state.rows) == 1
    assert service.progress(owner, 'window-pause')['run']['id'] == first['id']


def test_choice_identity_replay_and_stale_or_wrong_node_requests_never_advance_twice(story):
    service, state, owner = story
    first, key = begin(service, owner), uuid4()
    advanced = choose(service, owner, first, True, key)
    assert choose(service, owner, first, True, key) == advanced
    with pytest.raises(StoryConflict): choose(service, owner, first, False, key)
    with pytest.raises(StoryConflict): choose(service, owner, first, True)
    with pytest.raises(StoryConflict): service.choose(owner, first['id'], {'expected_version': advanced['version'], 'node_id': first['current_node']['id'], 'answer': True}, uuid4())
    assert state.rows[next(iter(state.rows))]['version'] == 2


def test_owner_isolation_deletion_gate_and_explicit_restart_are_transactional(story):
    service, state, owner = story
    first = begin(service, owner); other = uuid4()
    with pytest.raises(StoryNotFound): service.get(other, first['id'])
    with pytest.raises(StoryNotFound): service.restart(other, first['id'], {'expected_version': 1}, uuid4())
    state.deleting.add(owner)
    with pytest.raises(OwnerDeleting): choose(service, owner, first, True)
    state.deleting.remove(owner)
    restarted = service.restart(owner, first['id'], {'expected_version': 1}, uuid4())
    assert restarted['id'] != first['id'] and restarted['choice_count'] == 0
    assert service.get(owner, first['id'])['state'] == 'restarted'
    with pytest.raises(StoryConflict): service.restart(owner, first['id'], {'expected_version': 1}, uuid4())


def test_export_and_erase_include_only_this_owners_story_and_omit_request_fingerprints(story):
    service, state, owner = story
    first = choose(service, owner, begin(service, owner), False)
    other = uuid4(); other_run = begin(service, other)
    exported = service.export_owner(object(), owner)['story_runs']
    assert len(exported) == 1 and exported[0]['id'] == first['id'] and exported[0]['history'][0]['answer'] is False
    assert 'fingerprint' not in str(exported) and 'key_hash' not in str(exported)
    service.erase_owner(object(), owner)
    assert not service.export_owner(object(), owner)['story_runs']
    assert service.get(other, other_run['id'])['id'] == other_run['id']


@pytest.mark.parametrize('body', [{'expected_version': 1, 'node_id': 'arrive', 'answer': 1}, {'expected_version': True, 'node_id': 'arrive', 'answer': True}])
def test_untyped_versions_or_answers_are_rejected(story, body):
    service, _, owner = story
    run = begin(service, owner)
    with pytest.raises(StoryInvalid): service.choose(owner, run['id'], body, uuid4())


def test_a_missing_content_version_cannot_silently_start_the_latest_episode(story):
    service, state, owner = story
    with pytest.raises(StoryInvalid): service.start(owner, {'episode_id': 'window-pause', 'episode_version': None}, uuid4())
    assert not state.rows
