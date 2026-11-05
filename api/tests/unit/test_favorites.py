import ast
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from api.application.favorites import FavoritesUseCases
from api.application.favorites_ports import FavoriteRecord, FavoriteRequest, FavoritesRepository
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.favorites import FavoriteConflict, FavoriteInvalid, FavoriteNotFound


OWNER, READING, OTHER, KEY = (UUID(int=i) for i in range(1, 5))
NOW = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
RECORD = FavoriteRecord(READING, NOW)


class FakeFavoritesUnit:
    """This application fake exposes ports only, never a database connection."""
    def __init__(self, repository):
        self.favorites = repository
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def commit(self):
        self.commits += 1

    def repository_for(self, transaction):
        assert transaction is TOKEN
        return self.favorites


TOKEN = object()


def context():
    repo = Mock(spec=FavoritesRepository)
    repo.request.return_value = None
    repo.has_reading.return_value = True
    repo.get.return_value = None
    repo.add.return_value = RECORD
    repo.list.return_value = []
    repo.all_owned.return_value = [RECORD]
    repo.remove.return_value = True
    unit = FakeFavoritesUnit(repo)
    factory = Mock(return_value=unit)
    service = FavoritesUseCases(factory)
    return service, repo, unit, factory


def test_create_uses_guarded_repository_and_remembers_only_public_reference_metadata():
    service, repo, unit, _ = context()
    result = service.add(OWNER, {'reading_id': str(READING)}, KEY)
    assert result == {'reading_id': str(READING), 'created_at': NOW.isoformat()}
    assert [call[0] for call in repo.method_calls] == ['lock', 'active', 'request', 'has_reading', 'get', 'add', 'remember']
    repo.has_reading.assert_called_once_with(OWNER, READING)
    repo.add.assert_called_once_with(OWNER, READING)
    assert repo.remember.call_args.args[0] == OWNER
    assert repo.remember.call_args.args[-1] == RECORD
    assert len(repo.remember.call_args.args[1]) == len(repo.remember.call_args.args[2]) == 64
    assert unit.commits == 1


def test_new_key_for_existing_favorite_returns_its_original_timestamp_without_another_row():
    service, repo, unit, _ = context()
    repo.get.return_value = RECORD
    assert service.add(OWNER, {'reading_id': READING}, KEY)['created_at'] == NOW.isoformat()
    repo.add.assert_not_called()
    repo.remember.assert_called_once()
    assert unit.commits == 1


def replay_context():
    service, repo, unit, factory = context()
    service.add(OWNER, {'reading_id': READING}, KEY)
    _, key_hash, fingerprint, record = repo.remember.call_args.args
    repo.reset_mock()
    repo.request.return_value = FavoriteRequest(fingerprint, record.reading_id, record.created_at)
    repo.get.return_value = FavoriteRecord(READING, NOW + timedelta(days=1))
    return service, repo, unit, fingerprint


def test_request_replay_returns_the_original_dto_and_never_commits_or_adds():
    service, repo, unit, _ = replay_context()
    assert service.add(OWNER, {'reading_id': READING}, KEY) == {'reading_id': str(READING), 'created_at': NOW.isoformat()}
    repo.add.assert_not_called()
    repo.remember.assert_not_called()
    assert unit.commits == 1


def test_changed_payload_is_a_conflict_even_when_the_original_reading_was_deleted():
    service, repo, unit, fingerprint = replay_context()
    repo.request.return_value = FavoriteRequest(fingerprint, None, NOW)
    with pytest.raises(FavoriteConflict):
        service.add(OWNER, {'reading_id': OTHER}, KEY)
    repo.has_reading.assert_not_called()
    repo.add.assert_not_called()
    assert unit.commits == 1


@pytest.mark.parametrize('deleted', ['reading', 'favorite'])
def test_same_key_cannot_resurrect_a_deleted_source_or_removed_favorite(deleted):
    service, repo, unit, fingerprint = replay_context()
    if deleted == 'reading':
        repo.request.return_value = FavoriteRequest(fingerprint, None, NOW)
    else:
        repo.get.return_value = None
    with pytest.raises(FavoriteNotFound):
        service.add(OWNER, {'reading_id': READING}, KEY)
    repo.add.assert_not_called()
    repo.remember.assert_not_called()
    assert unit.commits == 1


def test_missing_or_foreign_reading_is_not_found_before_any_favorite_write():
    service, repo, unit, _ = context()
    repo.has_reading.return_value = False
    with pytest.raises(FavoriteNotFound):
        service.add(OWNER, {'reading_id': READING}, KEY)
    repo.get.assert_not_called()
    repo.add.assert_not_called()
    repo.remember.assert_not_called()
    assert unit.commits == 0


@pytest.mark.parametrize('values,key', [({}, KEY), ({'reading_id': None}, KEY),
    ({'reading_id': 'not-uuid'}, KEY), ({'reading_id': READING, 'body': 'private'}, KEY),
    ({'reading_id': READING}, 'not-uuid'), ({'reading_id': READING}, None)])
def test_invalid_create_does_not_open_a_transaction(values, key):
    service, _, _, factory = context()
    with pytest.raises(FavoriteInvalid):
        service.add(OWNER, values, key)
    factory.assert_not_called()


@pytest.mark.parametrize('limit', [0, 51, True, 1.5, '20'])
def test_invalid_page_limits_do_not_open_a_transaction(limit):
    service, _, _, factory = context()
    with pytest.raises(FavoriteInvalid):
        service.list(OWNER, limit=limit)
    factory.assert_not_called()


def test_list_uses_descending_keyset_cursor_bound_to_owner_and_no_other_data():
    service, repo, unit, _ = context()
    second = FavoriteRecord(OTHER, NOW - timedelta(seconds=1))
    repo.list.return_value = [RECORD, second]
    page = service.list(OWNER, limit=1)
    assert page['items'] == [{'reading_id': str(READING), 'created_at': NOW.isoformat()}]
    assert page['next_cursor']
    repo.list.assert_called_once_with(OWNER, after=None, limit=2)
    repo.list.return_value = [second]
    next_page = service.list(OWNER, cursor=page['next_cursor'], limit=1)
    assert next_page['items'] == [{'reading_id': str(OTHER), 'created_at': second.created_at.isoformat()}]
    assert next_page['next_cursor'] is None
    assert repo.list.call_args.kwargs['after'] == (NOW, READING)
    with pytest.raises(FavoriteInvalid):
        service.list(OTHER, cursor=page['next_cursor'], limit=1)
    assert unit.commits == 0


@pytest.mark.parametrize('cursor', ['', 'not-base64!', 'x' * 1025,
    base64.urlsafe_b64encode(b'[]').decode(),
    base64.urlsafe_b64encode(json.dumps({'kind': 'favorites', 'owner': str(OWNER), 'after': ['2026-11-06', str(READING)]}).encode()).decode()])
def test_malformed_cursor_is_rejected_without_reading_favorites(cursor):
    service, repo, _, factory = context()
    with pytest.raises(FavoriteInvalid):
        service.list(OWNER, cursor=cursor)
    factory.assert_not_called()
    repo.list.assert_not_called()


def test_default_page_and_empty_list_and_removal_are_scoped_and_noop_on_missing():
    service, repo, unit, _ = context()
    assert service.list(OWNER) == {'items': [], 'next_cursor': None}
    repo.list.assert_called_once_with(OWNER, after=None, limit=21)
    service.remove(OWNER, READING)
    repo.remove.assert_called_once_with(OWNER, READING)
    assert unit.commits == 1
    repo.remove.return_value = False
    with pytest.raises(FavoriteNotFound):
        service.remove(OWNER, READING)
    assert unit.commits == 1


@pytest.mark.parametrize('method,args', [('add', ({'reading_id': READING}, KEY)), ('list', ()), ('remove', (READING,))])
@pytest.mark.parametrize('failure', [OwnerDeleting('deleting'), DataNotFound('erased')])
def test_every_action_checks_lifecycle_before_accessing_favorite_data(method, args, failure):
    service, repo, unit, _ = context()
    repo.active.side_effect = failure
    with pytest.raises(type(failure)):
        getattr(service, method)(OWNER, *args)
    assert [call[0] for call in repo.method_calls] == ['lock', 'active']
    assert unit.commits == 0


def test_privacy_callbacks_use_bound_repository_and_do_not_open_or_commit_a_transaction():
    service, repo, unit, factory = context()
    assert service.export_owner(TOKEN, OWNER) == {'v2_favorites': [{'reading_id': str(READING), 'created_at': NOW.isoformat()}]}
    service.erase_owner(TOKEN, OWNER)
    repo.all_owned.assert_called_once_with(OWNER)
    repo.erase_owner.assert_called_once_with(OWNER)
    assert unit.commits == 0 and factory.call_count == 2


def test_constructor_is_database_free_and_application_never_uses_sql_or_drivers():
    _, _, _, factory = context()
    factory.assert_not_called()
    root = Path(__file__).parents[3]
    for name in ('favorites.py', 'favorites_ports.py'):
        tree = ast.parse((root / 'api/application' / name).read_text(encoding='utf-8'))
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                       'psycopg' in ast.unparse(node) for node in ast.walk(tree))
        assert not any(isinstance(node, ast.Attribute) and node.attr in ('execute', 'cursor', 'connection')
                       for node in ast.walk(tree))
