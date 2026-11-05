from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.local_guidance import LocalGuidance
from api.application.ai_jobs import AIJobsService
from api.application.experience import ExperienceUseCases, GuidanceUnavailable
from api.domain.experience import ExperiencePolicy
from api.tests.integration.test_p0_ai_jobs import ai_env, consent

pytestmark = pytest.mark.integration


class LegacyProvider:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls = 0

    def advise(self, *args):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        return {'empathy': 'heard', 'advice': 'reply', 'action': 'rest', 'expression': 'listen'}


def legacy(ai, pool, provider):
    return ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pool),
                              ExperiencePolicy(('care',), (), {}, (), 500, 2000), LocalGuidance(),
                              provider, ai_guard=ai.legacy_guard())


def ask(service, owner):
    return service.counsel(owner, 'care', 'private input', [], 'warm', uuid4(), use_ai=True, consent=True)


def test_production_v1_guard_requires_persisted_current_consent(ai_env):
    ai, _, pool, owners, _ = ai_env
    provider = LegacyProvider()
    service = legacy(ai, pool, provider)
    with pytest.raises(GuidanceUnavailable):
        ask(service, owners[0])
    assert provider.calls == 0


def test_v1_late_result_after_consent_revoke_and_regrant_is_not_saved(ai_env):
    ai, _, pool, owners, _ = ai_env
    owner = owners[0]
    provider = LegacyProvider()
    service = legacy(ai, pool, provider)
    consent(ai, owner)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ask, service, owner)
        try:
            assert provider.entered.wait(3)
            consent(ai, owner, granted=False)
            consent(ai, owner, granted=True)
        finally:
            provider.release.set()
        with pytest.raises(GuidanceUnavailable):
            pending.result(timeout=5)
    assert service.list(owner, 'counsel') == []


def test_v1_late_result_after_owner_lifecycle_change_is_not_saved(ai_env):
    _, _, pool, owners, factory = ai_env
    owner = owners[0]
    active = [True]
    ai = AIJobsService(factory, owner_guard=lambda connection, identity: active[0])
    consent(ai, owner)
    provider = LegacyProvider()
    service = legacy(ai, pool, provider)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ask, service, owner)
        try:
            assert provider.entered.wait(3)
            active[0] = False
        finally:
            provider.release.set()
        with pytest.raises(GuidanceUnavailable):
            pending.result(timeout=5)
    assert service.list(owner, 'counsel') == []
