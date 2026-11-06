"""Reading use cases over repository ports and pure card-selection rules."""
import base64
import json
import random
from datetime import datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Callable

from api.application.reading_v2_ports import ReadingCatalog, ReadingRepository, ReadingUnitOfWork
from api.domain.ai_jobs import AIError, fingerprint, validate_result
from api.domain.data_privacy import DataNotFound
from api.domain.reading_v2 import TOPICS, DeckAccessDenied, private_pool, reading_input, select_cards, selected_card


class ReadingV2Service:
    def __init__(self, uow_factory: Callable[[], ReadingUnitOfWork], catalog: ReadingCatalog,
                 *, owner_guard=None, clock=None, random_sample=None, ai_jobs=None,
                 allow_candidate=True, deck_access=None):
        self._uow, self.catalog = uow_factory, catalog
        self.owner_guard = owner_guard
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sample = random_sample or random.SystemRandom().sample
        self.ai_jobs, self.allow_candidate = ai_jobs, allow_candidate
        self.deck_access = deck_access

    def _allow_deck(self, transaction, owner, deck_id):
        if self.deck_access is not None and not self.deck_access(transaction, owner, deck_id):
            raise DeckAccessDenied('deck_purchase_required')

    def _owner(self, repository: ReadingRepository, owner):
        repository.lock_owner(owner)
        if not repository.owner_available(owner, self.owner_guard):
            raise AIError('OWNER_UNAVAILABLE', 401)

    def _row(self, repository: ReadingRepository, owner, rid):
        row = repository.get(owner, rid)
        if not row:
            raise AIError('NOT_FOUND', 404)
        return row

    def _once(self, repository: ReadingRepository, owner, scope, key, payload):
        row = repository.command(owner, scope, key, self.clock())
        if row:
            if row.fingerprint != fingerprint(payload):
                raise AIError('IDEMPOTENCY_CONFLICT', 409)
            result = repository.get(owner, row.reading_id)
            if not result:
                raise AIError('READING_DELETED', 410)
            return result.view
        repository.discard_command(owner, scope, key)

    def _remember(self, repository: ReadingRepository, owner, scope, key, payload, rid):
        repository.remember(owner, scope, key, fingerprint(payload), rid, self.clock() + timedelta(hours=24))

    def _catalog(self, deck_id):
        try:
            deck = self.catalog.get_deck(deck_id)
        except DataNotFound:
            raise AIError('DECK_NOT_FOUND', 404) from None
        if deck['status'] != 'published' and not (self.allow_candidate and deck['status'] == 'candidate'):
            raise AIError('DECK_UNAVAILABLE', 409)
        cards = self.catalog.list_cards(deck_version_id=deck_id)['items']
        if len(cards) != 78 or {card['id'] for card in cards} != set(range(78)):
            raise AIError('DECK_UNAVAILABLE', 409)
        return deck, cards

    def _timezone(self, repository: ReadingRepository, owner):
        name = repository.timezone(owner)
        if name is None:
            name = 'Asia/Seoul'
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise AIError('INVALID_TIMEZONE') from None

    def create(self, owner, key, body):
        body = reading_input(body)
        with self._uow() as uow:
            repository = uow.readings
            self._owner(repository, owner)
            previous = self._once(repository, owner, 'create', key, body)
            if previous:
                return previous
            self._allow_deck(repository.transaction, owner, body['deck_version_id'])
            deck, cards = self._catalog(body['deck_version_id'])
            name, tz = self._timezone(repository, owner)
            now, day, slots, selected = self.clock(), None, [], []
            rid, state = uuid4(), 'draft'
            if body['kind'] == 'daily':
                day = now.astimezone(tz).date()
                existing = repository.find_daily(owner, day, body['topic'])
                if existing:
                    self._remember(repository, owner, 'create', key, body, existing.id)
                    uow.commit()
                    return existing.view
                anchor = repository.daily_anchor(owner, day, body['topic'])
                if not anchor:
                    card_id = self.sample([card['id'] for card in cards], 1)[0]
                    expires = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
                    anchor = repository.add_daily_anchor(owner, day, body['topic'], card_id,
                                                         body['deck_version_id'], name, expires)
                # The fixed card, deck and captured zone survive deletion of the reading body.
                deck_id, name = str(anchor.deck_version_id), anchor.captured_timezone
                if deck_id != body['deck_version_id']:
                    self._allow_deck(repository.transaction, owner, deck_id)
                cards = self.catalog.list_cards(deck_version_id=deck_id)['items']
                selected = [selected_card(next(card for card in cards if card['id'] == anchor.card_id))]
                state = 'selected'
            else:
                deck_id = body['deck_version_id']
                slots = private_pool([card['id'] for card in cards], self.sample)
            row = repository.add(owner, rid, kind=body['kind'], topic=body['topic'], question=body.get('question', ''),
                                 deck_version_id=deck_id, local_day=day, captured_timezone=name, state=state,
                                 private_slots=slots, selected_cards=selected)
            self._remember(repository, owner, 'create', key, body, rid)
            uow.commit()
            return row.view

    def get(self, owner, rid):
        with self._uow() as uow:
            self._owner(uow.readings, owner)
            return self._row(uow.readings, owner, rid).view

    def _draft(self, repository: ReadingRepository, owner, rid, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise AIError('INVALID_VERSION')
        row = self._row(repository, owner, rid)
        if row.version != expected_version:
            raise AIError('READING_VERSION_CONFLICT', 409)
        if row.kind != 'three_card' or row.state != 'draft':
            raise AIError('READING_STATE_CONFLICT', 409)
        return row

    def shuffle(self, owner, rid, key, expected_version):
        payload = [expected_version]
        with self._uow() as uow:
            repository = uow.readings
            self._owner(repository, owner)
            previous = self._once(repository, owner, f'shuffle:{rid}', key, payload)
            if previous:
                return previous
            row = self._draft(repository, owner, rid, expected_version)
            cards = self.catalog.list_cards(deck_version_id=row.deck_version_id)['items']
            slots = private_pool([card['id'] for card in cards], self.sample)
            row = repository.shuffle(owner, rid, expected_version, slots)
            if row is None:
                raise AIError('READING_VERSION_CONFLICT', 409)
            self._remember(repository, owner, f'shuffle:{rid}', key, payload, rid)
            uow.commit()
            return row.view

    def select(self, owner, rid, key, expected_version, slot_ids):
        payload = [expected_version, list(map(str, slot_ids))]
        with self._uow() as uow:
            repository = uow.readings
            self._owner(repository, owner)
            previous = self._once(repository, owner, f'select:{rid}', key, payload)
            if previous:
                return previous
            row = self._draft(repository, owner, rid, expected_version)
            cards = self.catalog.list_cards(deck_version_id=row.deck_version_id)['items']
            selected = select_cards(row.private_slots, slot_ids, cards)
            row = repository.select(owner, rid, expected_version, selected)
            if row is None:
                raise AIError('READING_VERSION_CONFLICT', 409)
            self._remember(repository, owner, f'select:{rid}', key, payload, rid)
            uow.commit()
            return row.view

    def basic(self, owner, rid):
        with self._uow() as uow:
            self._owner(uow.readings, owner)
            row = self._row(uow.readings, owner, rid)
            if not row.selected_cards:
                raise AIError('READING_NOT_SELECTED', 409)
            cards = row.selected_cards
            return {'reading_id': str(rid), 'source': 'basic', 'cards': cards,
                    'summary': ' '.join(card['card']['basic_meaning'] for card in cards),
                    'advice': '카드의 뜻을 참고하며 지금 나에게 필요한 선택을 천천히 살펴보세요.',
                    'action': '오늘 할 수 있는 작은 행동 하나를 적어 보세요.'}

    def delete(self, owner, rid):
        with self._uow() as uow:
            repository = uow.readings
            self._owner(repository, owner)
            self._row(repository, owner, rid)
            if self.ai_jobs:
                self.ai_jobs.cancel_for_resource(repository.transaction, owner, rid)
            repository.delete(owner, rid)
            # Daily anchors and reference-only receipts intentionally survive until their TTL.
            uow.commit()

    def list(self, owner, limit=20, cursor=None, kind=None, topic=None):
        if type(limit) is not int or not 1 <= limit <= 50 or kind not in (None, 'daily', 'three_card') or topic not in (None, *TOPICS):
            raise AIError('INVALID_READING_FILTER')
        binding = {'owner': str(owner), 'kind': kind, 'topic': topic}
        after = None
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if data['filters'] != binding or len(data['after']) != 2:
                    raise ValueError()
                stamp, rid = datetime.fromisoformat(data['after'][0]), UUID(data['after'][1])
                if stamp.tzinfo is None:
                    raise ValueError()
                after = [stamp, rid]
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise AIError('INVALID_CURSOR') from None
        with self._uow() as uow:
            repository = uow.readings
            self._owner(repository, owner)
            rows = repository.list(owner, limit=limit + 1, after=after, kind=kind, topic=topic)
            items, more = rows[:limit], len(rows) > limit
            next_cursor = None
            if more:
                next_cursor = base64.urlsafe_b64encode(json.dumps({'filters': binding, 'after': [items[-1].created_at.isoformat(), str(items[-1].id)]}).encode()).decode()
            return {'items': [row.view for row in items], 'next_cursor': next_cursor}

    def _repository_for(self, transaction):
        return self._uow().repository_for(transaction)

    def snapshot(self, connection, owner, reading_id):
        repository = self._repository_for(connection)
        self._owner(repository, owner)
        return repository.snapshot(owner, reading_id)

    def validate(self, connection, owner, reading_id, version):
        repository = self._repository_for(connection)
        self._owner(repository, owner)
        row = repository.get(owner, reading_id)
        return bool(row and row.version == version and row.selected_cards)

    def mark_generating(self, connection, owner, reading_id, version):
        repository = self._repository_for(connection)
        self._owner(repository, owner)
        return repository.mark_generating(owner, reading_id, version)

    def mark_failed(self, connection, owner, reading_id, version):
        # Cancellation during erasure also uses this seam, so do not require an active owner here.
        repository = self._repository_for(connection)
        repository.lock_owner(owner)
        repository.mark_failed(owner, reading_id, version)

    def complete(self, connection, owner, reading_id, version, result):
        repository = self._repository_for(connection)
        self._owner(repository, owner)
        row = repository.get(owner, reading_id)
        if not row or row.version != version or not row.selected_cards:
            return False
        result = validate_result(result, 'tarot', row.selected_cards)
        return repository.complete(owner, reading_id, version, result)

    def source_exists(self, connection, owner, reading_id):
        return self._repository_for(connection).exists(owner, reading_id)

    def export_owner(self, connection, owner):
        return {'v2_readings': [row.view for row in self._repository_for(connection).all_owned(owner)]}

    def erase_owner(self, owner, connection=None):
        if connection is not None and not isinstance(owner, (UUID, str)):
            owner, connection = connection, owner
        if connection is None:
            with self._uow() as uow:
                self._erase(uow.readings, owner)
                uow.commit()
            return
        self._erase(self._repository_for(connection), owner)

    def _erase(self, repository: ReadingRepository, owner):
        repository.lock_owner(owner)
        if self.ai_jobs:
            for reading_id in repository.owned_ids(owner):
                self.ai_jobs.cancel_for_resource(repository.transaction, owner, reading_id)
        repository.erase_owner(owner)

    def purge_expired(self):
        with self._uow() as uow:
            removed = uow.readings.purge_expired(self.clock())
            uow.commit()
            return removed
