from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.local_guidance import LocalGuidance
from api.application.experience import ExperienceUseCases, GuidanceUnavailable
from api.domain.experience import ExperiencePolicy

pytestmark = pytest.mark.integration


class Provider:
    def __init__(self, blocked=False, fail=False):
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.blocked = blocked
        self.fail = fail

    def advise(self, *args):
        self.calls += 1
        number = self.calls
        self.entered.set()
        if self.blocked and number == 1:
            assert self.release.wait(10), 'test did not release the provider'
        if self.fail:
            raise GuidanceUnavailable('provider unavailable')
        return {'empathy': 'heard', 'advice': f'reply {number}', 'action': 'one step', 'expression': 'listen'}


@pytest.fixture
def ai_environment(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    policy = ExperiencePolicy(('care',), (), {}, ('one', 'two'), 500, 2000)
    active = [0]

    class TrackedUow:
        def __init__(self):
            self.inner = PostgresExperienceUnitOfWork(pg_pool)

        def __enter__(self):
            unit = self.inner.__enter__()
            active[0] += 1
            return unit

        def __exit__(self, *exc):
            try:
                return self.inner.__exit__(*exc)
            finally:
                active[0] -= 1

    def service(provider, **limits):
        return ExperienceUseCases(TrackedUow, policy, LocalGuidance(), provider, **limits)

    yield service, active, pg_pool, owners
    with pg_pool.connection() as connection:
        for table in ('experience_operations', 'daily_readings', 'tarot_readings', 'counsel_sessions'):
            connection.execute(f'DELETE FROM {table} WHERE session_id=ANY(%s)', (list(owners),))


def ask(service, owner, request_id):
    return service.counsel(owner, 'care', 'help me reflect', [True], 'warm', request_id,
                           use_ai=True, consent=True)


def test_provider_wait_releases_db_and_duplicate_requests_do_not_call_it_twice(ai_environment):
    factory, active, _, owners = ai_environment
    provider = Provider(blocked=True)
    service = factory(provider)
    request_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(ask, service, owners[0], request_id)
        try:
            assert provider.entered.wait(3)
            assert active[0] == 0, 'provider is holding a database transaction'
            duplicate = workers.submit(ask, service, owners[0], request_id)
            with pytest.raises(GuidanceUnavailable):
                duplicate.result(timeout=3)
        finally:
            provider.release.set()
        saved = running.result(timeout=5)
    assert ask(service, owners[0], request_id) == saved
    assert provider.calls == 1


def test_failed_provider_attempt_still_consumes_the_owner_daily_reservation(ai_environment):
    factory, _, _, owners = ai_environment
    provider = Provider(fail=True)
    service = factory(provider, ai_daily_limit=1)
    with pytest.raises(GuidanceUnavailable):
        ask(service, owners[0], uuid4())
    with pytest.raises(GuidanceUnavailable):
        ask(service, owners[0], uuid4())
    assert provider.calls == 1


def test_global_daily_limit_survives_a_new_guest_owner(ai_environment):
    factory, _, _, owners = ai_environment
    provider = Provider()
    service = factory(provider, ai_global_daily_limit=1)
    ask(service, owners[0], uuid4())
    with pytest.raises(GuidanceUnavailable):
        ask(service, owners[1], uuid4())
    assert provider.calls == 1


def test_expired_worker_cannot_overwrite_a_newer_attempt_result(ai_environment):
    factory, _, pool, owners = ai_environment
    provider = Provider(blocked=True)
    service = factory(provider)
    request_id = uuid4()
    with ThreadPoolExecutor(max_workers=1) as workers:
        old = workers.submit(ask, service, owners[0], request_id)
        try:
            assert provider.entered.wait(3)
            with pool.connection() as connection:
                exists = connection.execute("SELECT to_regclass('experience_ai_jobs') AS name").fetchone()['name']
                assert exists is not None, 'AI work has no durable lease'
                connection.execute("UPDATE experience_ai_jobs SET lease_until=now()-interval '1 second' WHERE session_id=%s AND request_id=%s", (owners[0], request_id))
            newer = ask(service, owners[0], request_id)
        finally:
            provider.release.set()
        assert old.result(timeout=5) == newer
    assert ask(service, owners[0], request_id)['advice'] == 'reply 2'
    assert len(service.list(owners[0], 'counsel')) == 1
