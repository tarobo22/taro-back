"""Owner-scoped favorites through repository ports and caller-bound privacy seams."""
from collections.abc import Callable

from api.application.favorites_ports import FavoriteRecord, FavoritesRepository, FavoritesUnitOfWork
from api.domain.favorites import (FavoriteConflict, FavoriteNotFound, FavoritesPolicy,
    decode_favorite_cursor, encode_favorite_cursor, favorite_hash, favorite_input, favorite_uuid)


class FavoritesUseCases:
    def __init__(self, uow_factory: Callable[[], FavoritesUnitOfWork], policy: FavoritesPolicy | None = None):
        self.uow_factory = uow_factory
        self.policy = policy or FavoritesPolicy()

    @staticmethod
    def _owner(repository: FavoritesRepository, owner):
        repository.lock(owner)
        repository.active(owner)

    def add(self, owner, values, request_key):
        reading_id = favorite_input(values)
        key_hash, fingerprint = favorite_hash(favorite_uuid(request_key)), favorite_hash(reading_id)
        with self.uow_factory() as uow:
            repository = uow.favorites
            self._owner(repository, owner)
            previous = repository.request(owner, key_hash)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise FavoriteConflict('idempotency_conflict')
                if previous.reading_id is None or repository.get(owner, previous.reading_id) is None:
                    raise FavoriteNotFound('favorite_reference_deleted')
                return FavoriteRecord(previous.reading_id, previous.created_at).dto()
            if not repository.has_reading(owner, reading_id):
                raise FavoriteNotFound('favorite_reading_not_found')
            saved = repository.get(owner, reading_id) or repository.add(owner, reading_id)
            repository.remember(owner, key_hash, fingerprint, saved)
            uow.commit()
            return saved.dto()

    def list(self, owner, *, cursor=None, limit=None):
        limit = self.policy.limit(limit)
        after = decode_favorite_cursor(owner, cursor)
        with self.uow_factory() as uow:
            self._owner(uow.favorites, owner)
            rows = uow.favorites.list(owner, after=after, limit=limit + 1)
            page = rows[:limit]
            next_cursor = None
            if len(rows) > limit:
                tail = page[-1]
                next_cursor = encode_favorite_cursor(owner, tail.created_at, tail.reading_id)
            return {'items': [row.dto() for row in page], 'next_cursor': next_cursor}

    def remove(self, owner, reading_id):
        reading_id = favorite_uuid(reading_id)
        with self.uow_factory() as uow:
            self._owner(uow.favorites, owner)
            if not uow.favorites.remove(owner, reading_id):
                raise FavoriteNotFound('favorite_not_found')
            uow.commit()

    def export_owner(self, connection, owner):
        repository = self.uow_factory().repository_for(connection)
        return {'v2_favorites': [row.dto() for row in repository.all_owned(owner)]}

    def erase_owner(self, connection, owner):
        # Privacy orchestration owns this transaction and has already locked the owner.
        self.uow_factory().repository_for(connection).erase_owner(owner)
