"""v2 journal contract over the existing, shared journal aggregate rows."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid4

from api.domain.journal_v2 import JournalV2Conflict, JournalV2Gone, JournalV2Invalid, JournalV2NotFound


class JournalV2UseCases:
    def __init__(self, uow_factory, policy, *, source_exists=None, invalidate_context=None, clock=None):
        self.uow_factory = uow_factory
        self.policy = policy
        self.source_exists = source_exists
        self.invalidate_context = invalidate_context
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _dto(self, row):
        return {'id': str(row['id']), 'local_day': row['local_day'].isoformat(),
                'mood': self.policy.public_mood(row['mood']), 'body': row['body'],
                'source_reading_id': str(row['source_reading_id']) if row['source_reading_id'] else None,
                'source_turn_id': str(row['source_turn_id']) if row['source_turn_id'] else None,
                'version': row['version'], 'created_at': row['created_at'].isoformat(),
                'updated_at': row['updated_at'].isoformat()}

    def create(self, owner, values, request_key):
        values = self.policy.create_values(values)
        key_hash = self._digest(str(request_key))
        fingerprint = self._digest(json.dumps(values, sort_keys=True, default=str, separators=(',', ':')))
        with self.uow_factory() as uow:
            uow.journals.lock(owner)
            uow.journals.active(owner)
            previous = uow.journals.operation(owner, key_hash)
            if previous:
                if previous['fingerprint'] is None or previous['entry_id'] is None or previous['expires_at'] <= self.clock():
                    uow.journals.expire_operation(owner, key_hash)
                    uow.commit()
                    raise JournalV2Gone('journal_create_reference_expired_or_deleted')
                if previous['fingerprint'] != fingerprint:
                    raise JournalV2Conflict('idempotency_conflict')
                current = uow.journals.get(owner, previous['entry_id'])
                if current is None:
                    raise JournalV2Gone('journal_create_reference_deleted')
                return self._dto(current)
            for kind in ('reading', 'turn'):
                source_id = values.get('source_' + kind + '_id')
                if source_id and (self.source_exists is None or not self.source_exists(uow.connection, owner, kind, source_id)):
                    raise JournalV2NotFound('journal_source_not_found')
            saved = uow.journals.create(owner, uuid4(), values)
            uow.journals.remember(owner, key_hash, fingerprint, saved['id'],
                                  self.clock() + timedelta(hours=self.policy.idempotency_hours))
            uow.commit()
            return self._dto(saved)

    def get(self, owner, entry_id):
        with self.uow_factory() as uow:
            uow.journals.lock(owner)
            uow.journals.active(owner)
            row = uow.journals.get(owner, entry_id)
            if row is None:
                raise JournalV2NotFound('journal_not_found')
            return self._dto(row)

    def update(self, owner, entry_id, values):
        values = self.policy.patch_values(values)
        expected_version = values.pop('expected_version')
        with self.uow_factory() as uow:
            uow.journals.lock(owner)
            uow.journals.active(owner)
            current = uow.journals.get(owner, entry_id)
            if current is None:
                raise JournalV2NotFound('journal_not_found')
            if current['version'] != expected_version:
                raise JournalV2Conflict('stale_journal')
            changed = uow.journals.update(owner, entry_id, expected_version, values)
            if changed is None:
                if uow.journals.get(owner, entry_id) is None:
                    raise JournalV2NotFound('journal_not_found')
                raise JournalV2Conflict('stale_journal')
            uow.commit()
            return self._dto(changed)

    def list(self, owner, *, cursor=None, limit=None, start=None, end=None, mood=None):
        limit = self.policy.page_default if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= self.policy.page_max:
            raise JournalV2Invalid('invalid_page_limit')
        if start is not None:
            self.policy.validate_day(start)
        if end is not None:
            self.policy.validate_day(end)
        if start is not None and end is not None and start > end:
            raise JournalV2Invalid('invalid_date_range')
        moods = [self.policy.stored_mood(mood), mood] if mood is not None else None
        binding = {'owner': str(owner), 'from': start.isoformat() if start else None,
                   'to': end.isoformat() if end else None, 'mood': mood}
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError
                decoded = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
                position = decoded.pop('after')
                if decoded != binding or not isinstance(position, list) or len(position) != 2:
                    raise ValueError
                after = (datetime.fromisoformat(position[0]), UUID(position[1]))
                if after[0].tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError, KeyError):
                raise JournalV2Invalid('invalid_journal_cursor') from None
        with self.uow_factory() as uow:
            uow.journals.lock(owner)
            uow.journals.active(owner)
            rows = uow.journals.list(owner, start=start, end=end, moods=moods, after=after, limit=limit + 1)
            page = rows[:limit]
            next_cursor = None
            if len(rows) > limit:
                tail = page[-1]
                next_cursor = base64.urlsafe_b64encode(json.dumps({**binding,
                    'after': [tail['created_at'].isoformat(), str(tail['id'])]},
                    sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
            return {'items': [self._dto(row) for row in page], 'next_cursor': next_cursor}

    def delete(self, owner, entry_id):
        with self.uow_factory() as uow:
            uow.journals.lock(owner)
            uow.journals.active(owner)
            if uow.journals.get(owner, entry_id) is None:
                if uow.journals.deleted(owner, entry_id):
                    return False
                raise JournalV2NotFound('journal_not_found')
            if self.invalidate_context is not None:
                self.invalidate_context(uow.connection, owner, entry_id)
            if not uow.journals.delete(owner, entry_id):
                raise JournalV2NotFound('journal_not_found')
            uow.commit()
            return True

    def export_owner(self, connection, owner):
        repository = self.uow_factory().repository_for(connection)
        return {'v2_journal_entries': [self._dto(row) for row in repository.list(owner)]}

    def erase_owner(self, connection, owner):
        # Called by privacy orchestration while deleting; do not require active.
        self.uow_factory().repository_for(connection).erase_metadata(owner)

    def purge_expired_idempotency(self):
        """Discard expired request payload hashes/references, retaining spent-key tombstones."""
        with self.uow_factory() as uow:
            uow.journals.purge_expired_idempotency(self.clock())
            uow.commit()
