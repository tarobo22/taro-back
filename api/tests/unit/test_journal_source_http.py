"""Owned journal-to-source navigation is a bounded read, not a scan through every conversation."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal_v2 import journal_v2_router
from api.application.journal_v2 import JournalV2UseCases
from api.domain.journal_v2 import JournalV2NotFound


def policy():
    return SimpleNamespace(max_body_length=2000, page_default=20, page_max=50)


@pytest.mark.parametrize('kind', ['reading_v2', 'turn_v2', 'legacy_reading', 'legacy_counsel'])
def test_source_http_is_discriminated_and_never_caches_owned_content(kind):
    entry, resource, conversation, owner = uuid4(), uuid4(), uuid4(), uuid4()
    reading = {'id': str(resource), 'day': None, 'topic': '나의 마음', 'question': '작은 질문',
               'spreadId': 'one', 'cards': [{'id': 0, 'name': '바보', 'keyword': '시작',
               'message': '천천히', 'action': '작게 시작', 'art': 'sun', 'position': '지금'}],
               'summary': '정리', 'advice': '조언', 'action': '한 걸음', 'created_at': '2026-11-06T00:00:00Z'}
    counsel = {'id': str(resource), 'topic': '나의 마음', 'question': '작은 질문', 'tone': 'warm',
               'empathy': '들었어요', 'advice': '조언', 'action': '한 걸음', 'expression': 'listen',
               'source': 'local', 'parentId': None, 'created_at': '2026-11-06T00:00:00Z'}
    expected = {'reading_v2': {'kind': kind, 'reading_id': str(resource)},
                'turn_v2': {'kind': kind, 'conversation_id': str(conversation), 'turn_id': str(resource)},
                'legacy_reading': {'kind': kind, 'reading': reading},
                'legacy_counsel': {'kind': kind, 'counsel': counsel}}[kind]
    calls = []
    class Service:
        policy = policy()
        def source(self, current_owner, current_entry):
            calls.append((current_owner, current_entry))
            return expected
    app = FastAPI();app.include_router(journal_v2_router(Service(), lambda: owner))
    with TestClient(app) as client:
        result = client.get(f'/v2/journal-entries/{entry}/source')
        assert result.status_code == 200
        assert result.json() == expected
        assert result.headers['cache-control'] == 'no-store'
    assert calls == [(owner, entry)]


def test_missing_source_is_404_and_bad_entry_id_does_not_call_application():
    calls = []
    class Service:
        policy = policy()
        def source(self, owner, entry):
            calls.append(entry);raise JournalV2NotFound('journal_source_not_found')
    app = FastAPI();app.include_router(journal_v2_router(Service(), lambda: uuid4()))
    with TestClient(app) as client:
        assert client.get(f'/v2/journal-entries/{uuid4()}/source').status_code == 404
        assert client.get('/v2/journal-entries/not-a-uuid/source').status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize('entry_present,has_reference,resolved', [(False, True, True), (True, False, True), (True, True, False)])
def test_owner_and_journal_are_checked_before_source_resolution(entry_present, has_reference, resolved):
    owner, entry, resource = uuid4(), uuid4(), uuid4()
    events = [];transaction = object()
    class Repository:
        def lock(self, value): events.append(('lock', value))
        def active(self, value): events.append(('active', value))
        def get(self, value, key):
            events.append(('journal', value, key))
            return {'source_reading_id': resource if has_reference else None, 'source_turn_id': None} if entry_present else None
    class UnitOfWork:
        connection = transaction
        journals = Repository()
        def __enter__(self): return self
        def __exit__(self, *args): events.append(('release',))
    def lookup(current_transaction, current_owner, kind, key):
        events.append(('source', current_transaction, current_owner, kind, key))
        return {'kind': 'reading_v2', 'reading_id': str(resource)} if resolved else None
    service = JournalV2UseCases(UnitOfWork, policy(), source_lookup=lookup)
    with pytest.raises(JournalV2NotFound):service.source(owner, entry)
    assert events[:3] == [('lock', owner), ('active', owner), ('journal', owner, entry)]
    assert any(e[0] == 'source' for e in events) == (entry_present and has_reference)
    assert events[-1] == ('release',)


def test_source_usecase_reuses_the_callers_transaction_and_returns_only_the_resolved_reference():
    owner, entry, resource = uuid4(), uuid4(), uuid4();transaction = object();events = []
    class Repository:
        def lock(self, value):events.append('lock')
        def active(self, value):events.append('active')
        def get(self, current_owner, key):
            assert (current_owner, key) == (owner, entry)
            return {'source_reading_id': None, 'source_turn_id': resource}
    class UnitOfWork:
        connection = transaction;journals = Repository()
        def __enter__(self):return self
        def __exit__(self, *args):events.append('release')
    expected = {'kind': 'turn_v2', 'conversation_id': str(uuid4()), 'turn_id': str(resource)}
    def lookup(current_transaction, current_owner, kind, key):
        assert (current_transaction, current_owner, kind, key) == (transaction, owner, 'turn', resource)
        events.append('lookup');return expected
    assert JournalV2UseCases(UnitOfWork, policy(), source_lookup=lookup).source(owner, entry) == expected
    assert events == ['lock', 'active', 'lookup', 'release']
