"""Application contracts run without SQL, a database driver, or an executable transaction."""
import ast
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from api.application.ai_jobs import AIJobsService
from api.domain.ai_jobs import AIError


class Repository:
    def __init__(self):
        self.transaction = object()
        self.events, self.operations, self.jobs = [], {}, {}
        self.active, self.commits, self.locks, self.started = 0, 0, [], []

    def lock_owner(self, owner):
        self.locks.append(owner)

    def owner_available(self, owner, guard=None):
        return guard(self.transaction, owner) if guard else True

    def idempotency(self, owner, scope, key):
        return self.operations.get((owner, scope, key))

    def remember(self, owner, scope, key, fingerprint, resource, response):
        self.operations[owner, scope, key] = {'fingerprint': fingerprint, 'response': deepcopy(response)}

    def latest_consent(self, owner, kind):
        return next((e for e in reversed(self.events) if e['owner_id'] == owner and e['kind'] == kind), None)

    def add_consent(self, event_id, owner, kind, policy_version, granted):
        event = {'id': event_id, 'owner_id': owner, 'kind': kind, 'policy_version': policy_version,
                 'granted': granted, 'occurred_at': '2026-11-06T00:00:00+00:00'}
        self.events.append(event)
        return event

    def pending_for_consent(self, owner, kind):
        return []

    def consent_view(self, event):
        return {key: str(event[key]) if key == 'id' else event[key]
                for key in ('id', 'kind', 'policy_version', 'granted', 'occurred_at')}

    def consents(self, owner, after, limit):
        return [e for e in reversed(self.events) if e['owner_id'] == owner][:limit]

    def consent_cursor(self, event):
        return [event['occurred_at'], str(event['id'])]

    def claim_next(self, lease, seconds):
        job = next((j for j in self.jobs.values() if j['state'] == 'queued'), None)
        if job:
            job.update(state='running', lease_id=lease)
            return deepcopy(job)

    def job(self, owner, job_id, for_update=False):
        job = self.jobs.get(job_id)
        return deepcopy(job) if job and job['owner_id'] == owner else None

    def context_sources(self, job_id):
        return []

    def mark_provider_started(self, job_id, lease, started):
        assert self.active == 1
        self.jobs[job_id]['provider_started'] = started
        self.started.append(job_id)

    def finish_job(self, job_id, state, code, url):
        self.jobs[job_id].update(state=state, error_code=code, result_url=url)

    def add_run(self, values):
        self.run = values


class UnitOfWork:
    def __init__(self, repository):
        self.ai_jobs = repository

    def __enter__(self):
        self.ai_jobs.active += 1
        return self

    def __exit__(self, *args):
        self.ai_jobs.active -= 1

    def commit(self):
        self.ai_jobs.commits += 1

    def repository_for(self, transaction):
        assert transaction is self.ai_jobs.transaction
        return self.ai_jobs


def test_ai_application_has_no_sql_driver_or_database_execution():
    module = Path(__file__).parents[2] / 'application/ai_jobs.py'
    tree = ast.parse(module.read_text(encoding='utf-8'))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and
                (n.module or '').startswith(('psycopg', 'api.adapters'))]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in
                ('execute', 'fetchone', 'fetchall', 'row_factory', 'connection')]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and any(phrase in n.value.upper() for phrase in ('SELECT ', 'INSERT INTO ', 'UPDATE P0_', 'DELETE FROM '))]


def test_consent_policy_idempotency_and_ownership_use_fake_repository_only():
    repository = Repository()
    service = AIJobsService(lambda: UnitOfWork(repository))
    owner, other, key = uuid4(), uuid4(), uuid4()
    event = service.record_consent(owner, key, 'ai_processing', '2026-11-05', True)
    assert service.record_consent(owner, key, 'ai_processing', '2026-11-05', True) == event
    assert len(repository.events) == 1 and repository.commits == 1
    assert service.list_consents(other)['items'] == []
    assert service.list_consents(owner)['items'] == [event]
    with pytest.raises(AIError, match='IDEMPOTENCY_CONFLICT'):
        service.record_consent(owner, key, 'ai_processing', '2026-11-05', False)


def test_worker_preserves_transaction_boundaries_and_opaque_reading_callbacks():
    repository = Repository()
    owner, job_id, reading_id, consent_id = uuid4(), uuid4(), uuid4(), uuid4()
    repository.add_consent(consent_id, owner, 'ai_processing', '2026-11-05', True)
    cards = [{'card_id': 7, 'position': 1}]
    repository.jobs[job_id] = {'id': job_id, 'owner_id': owner, 'kind': 'tarot', 'state': 'queued',
        'resource_id': reading_id, 'resource_version': 2, 'consent_event_id': consent_id,
        'context_consent_id': None, 'payload': {'cards': cards, 'context_entry_ids': []}}
    result = {'kind': 'tarot', 'empathy': '고마워요.', 'summary': '생각을 살펴봐요.',
              'card_insights': [{'card_id': 7, 'position': 1, 'meaning': '작은 선택', 'reflection': '돌아봐요.'}],
              'advice': '작은 행동을 해 봐요.', 'actions': ['메모해요.'], 'expression': 'listen',
              'follow_up_questions': [], 'safety_mode': 'standard'}
    class Reading:
        def validate(self, transaction, identity, resource, version):
            assert transaction is repository.transaction and repository.active == 1
            return identity == owner and resource == reading_id and version == 2
        def snapshot(self, transaction, identity, resource):
            assert transaction is repository.transaction
            return {'topic': 'care', 'question': '현재 마음', 'cards': cards}
        def complete(self, transaction, identity, resource, version, value):
            assert transaction is repository.transaction and repository.active == 1
            assert value == result
            self.completed = True
            return True
    class Provider:
        model = 'fake-port-model'
        def available(self):
            return True
        def generate(self, payload):
            assert repository.active == 0 and repository.started == [job_id]
            assert payload['cards'] == cards
            return result
    reading = Reading()
    service = AIJobsService(lambda: UnitOfWork(repository), provider=Provider(), reading_source=reading)
    assert service.run_next('port-worker') is True
    assert reading.completed and repository.jobs[job_id]['state'] == 'succeeded'
    assert repository.commits == 3 and repository.active == 0


def test_custom_owner_guard_receives_opaque_transaction_and_stops_before_repository_mutation():
    repository = Repository()
    def inactive(transaction, owner):
        assert transaction is repository.transaction and repository.locks == [owner]
        return False
    service = AIJobsService(lambda: UnitOfWork(repository), owner_guard=inactive)
    with pytest.raises(AIError, match='OWNER_UNAVAILABLE'):
        service.record_consent(uuid4(), uuid4(), 'ai_processing', '2026-11-05', True)
    assert not repository.events and repository.commits == 0
