from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from api.adapters.data_privacy_postgres import initialize_profile, owner_guard
from api.adapters.identity_postgres import PostgresIdentityUnitOfWork
from api.application.identity import IdentityUseCases
from api.domain.identity import IdentityPolicy, IdentityRuleViolation, SessionRejected

pytestmark = pytest.mark.integration


@pytest.fixture
def identity_setup(pg_pool):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    created = []
    clock = [datetime(2026, 11, 6, tzinfo=timezone.utc)]

    def new_id():
        value = uuid4()
        created.append(value)
        return value

    def service():
        return IdentityUseCases(lambda: PostgresIdentityUnitOfWork(pg_pool, initialize_profile, owner_guard),
                                IdentityPolicy(180, ('ko-KR',)), lambda: clock[0], new_id)

    yield service, clock, created, pg_pool
    with pg_pool.connection() as connection:
        connection.execute('DELETE FROM app_sessions WHERE id=ANY(%s)', (created,))


def test_session_refresh_preserves_owner_and_extends_only_a_valid_identity(identity_setup):
    make, clock, created, pool = identity_setup
    service = make()
    grant = service.create('android', 'ko-KR', 'Asia/Seoul')
    assert grant.user_id == grant.session_id
    assert grant.expires_at == clock[0] + timedelta(days=180)
    assert service.authenticate('Bearer ' + grant.access_token) == grant.user_id
    with pool.connection() as connection:
        row = connection.execute('SELECT token_hash FROM app_sessions WHERE id=%s', (grant.user_id,)).fetchone()
        assert row['token_hash'] != grant.access_token
        profile = connection.execute('SELECT timezone FROM data_privacy_profiles WHERE owner_id=%s', (grant.user_id,)).fetchone()
        assert profile['timezone'] == 'Asia/Seoul'
    clock[0] += timedelta(days=1)
    renewed = service.refresh(grant.user_id)
    assert renewed.session_id == grant.session_id
    assert renewed.expires_at == clock[0] + timedelta(days=180)
    clock[0] += timedelta(days=181)
    with pytest.raises(SessionRejected):
        service.authenticate('Bearer ' + grant.access_token)
    assert len(created) == 1


def test_invalid_timezone_does_not_create_a_new_owner(identity_setup):
    make, _, created, _ = identity_setup
    with pytest.raises(IdentityRuleViolation):
        make().create('web', 'ko-KR', 'Invalid/Clock')
    assert created == []


def test_erasing_or_revoking_an_identity_never_silently_allocates_a_replacement(identity_setup):
    make, _, created, pool = identity_setup
    service = make()
    grant = service.create('web', 'ko-KR', 'Asia/Seoul')
    with pool.connection() as connection:
        connection.execute("UPDATE data_privacy_profiles SET state='erased' WHERE owner_id=%s", (grant.user_id,))
    with pytest.raises(SessionRejected):
        service.authenticate('Bearer ' + grant.access_token)
    with pytest.raises(SessionRejected):
        service.refresh(grant.user_id)
    assert len(created) == 1
