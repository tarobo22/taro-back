"""Insight use cases and leased worker boundaries with opaque in-memory ports."""
import ast
from copy import deepcopy
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from api.application.ai_jobs import AIJobsService
from api.application.journal_insights import JournalInsightsService
from api.domain.ai_jobs import AIError


RESULT = {'kind': 'insight', 'empathy': '기록해 주셨네요.', 'summary': '천천히 살펴봐요.', 'card_insights': [],
          'advice': '작은 행동을 골라 봐요.', 'actions': ['잠시 쉬어요.'], 'expression': 'listen',
          'follow_up_questions': [], 'safety_mode': 'standard'}


class State:
    def __init__(self):
        self.owner, self.entry, self.resource, self.job_id = uuid4(), uuid4(), uuid4(), uuid4()
        self.transaction, self.active, self.commits = object(), 0, 0
        self.consents = {kind: {'id': uuid4(), 'granted': True, 'policy_version': '2026-11-05'}
                         for kind in ('ai_processing', 'journal_context')}
        self.sources = [{'entry_id': self.entry, 'entry_version': 1}]
        self.journals = {self.entry: {'id': self.entry, 'version': 1, 'day': date(2026, 11, 5),
                                    'mood': 'calm', 'body': 'explicit private text'}}
        self.row = {'id': self.resource, 'owner_id': self.owner, 'range_start': date(2026, 11, 1),
            'range_end': date(2026, 11, 6), 'based_on_count': 1, 'state': 'generating', 'version': 1,
            'consent_event_id': self.consents['ai_processing']['id'],
            'context_consent_id': self.consents['journal_context']['id'], 'sources': self.sources, 'result': None}
        self.job = {'id': self.job_id, 'owner_id': self.owner, 'kind': 'insight', 'state': 'queued',
            'resource_id': self.resource, 'resource_version': 1, 'consent_event_id': self.row['consent_event_id'],
            'context_consent_id': self.row['context_consent_id'], 'payload': {
                'context_entry_ids': [str(self.entry)], 'context_versions': {str(self.entry): 1}}}


class InsightsRepository:
    def __init__(self, state):
        self.state, self.transaction = state, state.transaction

    def lock_owner(self, owner):
        assert self.state.active == 1

    def owner_available(self, owner, guard=None):
        return guard(self.transaction, owner) if guard else owner == self.state.owner

    def get(self, owner, resource):
        return deepcopy(self.state.row) if self.state.row and (owner, resource) == (self.state.owner, self.state.resource) else None

    def current_sources(self, owner, ids):
        assert owner == self.state.owner
        return [deepcopy(self.state.journals[entry]) for entry in ids if entry in self.state.journals]

    def latest_consent(self, owner, kind):
        return deepcopy(self.state.consents[kind])

    def invalidate(self, owner, resource):
        assert (owner, resource) == (self.state.owner, self.state.resource)
        if self.state.row:
            self.state.row.update(state='invalidated', result=None, version=self.state.row['version'] + 1)

    def mark_terminal(self, owner, resource, version, state):
        if self.state.row and self.state.row['version'] == version and self.state.row['state'] == 'generating':
            self.state.row.update(state=state, result=None)

    def complete(self, owner, resource, version, result):
        assert self.state.active == 1
        self.state.row.update(state='ready', result=deepcopy(result), version=version + 1)
        return True

    def all_owned(self, owner):
        return [deepcopy(self.state.row)] if self.state.row else []


class JobsRepository:
    def __init__(self, state):
        self.state, self.transaction = state, state.transaction

    def lock_owner(self, owner):
        assert self.state.active == 1

    def owner_available(self, owner, guard=None):
        return owner == self.state.owner

    def latest_consent(self, owner, kind):
        return self.state.consents[kind]

    def claim_next(self, lease, seconds):
        if self.state.job['state'] != 'queued': return None
        self.state.job.update(state='running', lease_id=lease)
        return deepcopy(self.state.job)

    def job(self, owner, job_id, for_update=False):
        return deepcopy(self.state.job)

    def context_sources(self, job_id):
        return deepcopy(self.state.sources)

    def journal_version(self, owner, entry):
        source = self.state.journals.get(entry)
        return source['version'] if source else None

    def journal(self, owner, entry):
        return deepcopy(self.state.journals.get(entry if entry in self.state.journals else next(
            (key for key in self.state.journals if str(key) == str(entry)), None)))

    def mark_provider_started(self, job_id, lease, started):
        assert self.state.active == 1
        self.state.job['provider_started'] = started

    def finish_job(self, job_id, state, code, url):
        self.state.job.update(state=state, error_code=code, result_url=url)

    def add_run(self, values):
        self.state.run = values


class UnitOfWork:
    def __init__(self, state):
        self.state = state
        self.ai_jobs, self.insights = JobsRepository(state), InsightsRepository(state)

    def __enter__(self):
        self.state.active += 1
        return self

    def __exit__(self, *exc):
        self.state.active -= 1

    def commit(self):
        self.state.commits += 1

    def repository_for(self, transaction):
        assert transaction is self.state.transaction
        return self.insights


def test_new_insight_application_has_no_sql_or_driver_dependency_and_constructor_is_database_free():
    def fail(): raise AssertionError('constructor must not open persistence')
    service = JournalInsightsService(fail)
    AIJobsService(fail, insight_source=service)
    path = Path(__file__).parents[2] / 'application/journal_insights.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and
                (node.module or '').startswith(('psycopg', 'api.adapters'))]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in
                ('execute', 'fetchone', 'fetchall', 'row_factory', 'connection')]


@pytest.mark.parametrize('change', ['none', 'edit_during_call', 'delete_during_call', 'superseded_consent'])
def test_insight_worker_rechecks_sources_and_consent_and_releases_all_transactions_for_provider(change):
    state = State()
    factory = lambda: UnitOfWork(state)
    insights = JournalInsightsService(factory)
    class Provider:
        def available(self): return True
        def generate(self, payload):
            assert state.active == 0 and state.job['provider_started']
            assert payload['kind'] == 'insight' and payload['cards'] == []
            assert payload['journals'] == [{'id': str(state.entry), 'version': 1, 'day': '2026-11-05',
                                          'mood': 'calm', 'body': 'explicit private text'}]
            if change == 'edit_during_call': state.journals[state.entry]['version'] = 2
            if change == 'delete_during_call': del state.journals[state.entry]
            if change == 'superseded_consent': state.consents['journal_context']['id'] = uuid4()
            return dict(RESULT)
    jobs = AIJobsService(factory, provider=Provider(), insight_source=insights)
    assert jobs.run_next()
    assert state.active == 0 and state.commits == 3
    assert state.row['state'] == ('ready' if change == 'none' else 'invalidated')
    assert state.row['result'] == (RESULT if change == 'none' else None)
    assert state.job['state'] == ('succeeded' if change == 'none' else 'cancelled')


def test_get_and_export_invalidate_completed_results_and_keep_original_reference_metadata():
    state = State()
    state.row.update(state='ready', result=dict(RESULT), version=2)
    insights = JournalInsightsService(lambda: UnitOfWork(state))
    assert insights.get(state.owner, state.resource)['result'] == RESULT
    del state.journals[state.entry]
    result = insights.get(state.owner, state.resource)
    assert result['state'] == 'invalidated' and result['result'] is None
    assert result['entry_ids'] == [str(state.entry)] and result['entry_versions'] == {str(state.entry): 1}
    with UnitOfWork(state):
        exported = insights.export_owner(state.transaction, state.owner)
    assert exported == {'journal_insights': [result]}


def test_completed_insight_output_still_rejects_diagnoses_and_guarantees():
    state = State()
    insights = JournalInsightsService(lambda: UnitOfWork(state))
    with UnitOfWork(state):
        with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
            insights.complete(state.transaction, state.owner, state.resource, 1,
                              {**RESULT, 'summary': '당신은 우울증입니다.'})
    assert state.row['state'] == 'generating' and state.row['result'] is None


def test_worker_does_not_send_a_new_source_version_changed_after_initial_check(monkeypatch):
    state = State()
    original_version = JobsRepository.journal_version
    def version_then_edit(repository, owner, entry):
        version = original_version(repository, owner, entry)
        state.journals[state.entry].update(version=2, body='unapproved newer text')
        return version
    monkeypatch.setattr(JobsRepository, 'journal_version', version_then_edit)
    calls = []
    class Provider:
        def available(self): return True
        def generate(self, payload):
            calls.append(payload)
            return dict(RESULT)
    factory = lambda: UnitOfWork(state)
    source = JournalInsightsService(factory)
    service = AIJobsService(factory, provider=Provider(), insight_source=source)
    assert service.run_next()
    assert calls == []
    assert state.job['state'] == 'cancelled' and state.row['state'] == 'invalidated'


def test_persisted_job_cannot_expand_the_durable_explicit_selection():
    state = State()
    extra = uuid4()
    state.journals[extra] = {**state.journals[state.entry], 'id': extra, 'body': 'never selected'}
    state.job['payload']['context_entry_ids'].append(str(extra))
    state.job['payload']['context_versions'][str(extra)] = 1
    state.sources = [*state.sources, {'entry_id': extra, 'entry_version': 1}]
    calls = []
    class Provider:
        def available(self): return True
        def generate(self, payload):
            calls.append(payload)
            return dict(RESULT)
    factory = lambda: UnitOfWork(state)
    service = AIJobsService(factory, provider=Provider(), insight_source=JournalInsightsService(factory))
    assert service.run_next() and not calls
    assert state.job['error_code'] == 'RESOURCE_CHANGED' and state.row['result'] is None
