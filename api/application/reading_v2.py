"""Versioned server-authoritative P0 reading state and the AI reading-source port."""
import base64
import json
import random
from datetime import datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb

from api.application.ai_jobs import default_owner_guard, owner_lock
from api.domain.ai_jobs import AIError, fingerprint, validate_result
from api.domain.data_privacy import DataNotFound
from api.domain.reading_v2 import TOPICS, private_pool, reading_input, select_cards, selected_card


def reading_view(row):
    return {'id': str(row['id']), 'kind': row['kind'], 'topic': row['topic'], 'question': row['question'],
            'deck_version_id': str(row['deck_version_id']), 'local_day': str(row['local_day']) if row['local_day'] else None,
            'captured_timezone': row['captured_timezone'], 'state': row['state'], 'version': row['version'],
            'slots': [{'id': slot['id'], 'index': slot['index']} for slot in row['private_slots']],
            'selected_cards': row['selected_cards'], 'result': row['result'],
            'created_at': row['created_at'].isoformat()}


class ReadingV2Service:
    def __init__(self, uow_factory, catalog, *, owner_guard=None, clock=None, random_sample=None, ai_jobs=None,
                 allow_candidate=True):
        self._uow, self.catalog = uow_factory, catalog
        self.owner_guard = owner_guard or default_owner_guard
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sample = random_sample or random.SystemRandom().sample
        self.ai_jobs, self.allow_candidate = ai_jobs, allow_candidate

    def _owner(self, connection, owner):
        owner_lock(connection, owner)
        if self.owner_guard(connection, owner) is False:
            raise AIError('OWNER_UNAVAILABLE', 401)

    def _row(self, connection, owner, rid):
        row = connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, rid)).fetchone()
        if not row:
            raise AIError('NOT_FOUND', 404)
        return row

    def _once(self, connection, owner, scope, key, payload):
        row = connection.execute('SELECT * FROM p0_reading_commands WHERE owner_id=%s AND scope=%s AND key=%s AND expires_at>%s', (owner, scope, key, self.clock())).fetchone()
        if row:
            if row['fingerprint'] != fingerprint(payload):
                raise AIError('IDEMPOTENCY_CONFLICT', 409)
            result = connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, row['reading_id'])).fetchone()
            if not result:
                raise AIError('READING_DELETED', 410)
            return reading_view(result)
        connection.execute('DELETE FROM p0_reading_commands WHERE owner_id=%s AND scope=%s AND key=%s', (owner, scope, key))

    def _remember(self, connection, owner, scope, key, payload, rid):
        connection.execute('INSERT INTO p0_reading_commands(owner_id,scope,key,fingerprint,reading_id,expires_at) VALUES(%s,%s,%s,%s,%s,%s)',
                           (owner, scope, key, fingerprint(payload), rid, self.clock() + timedelta(hours=24)))

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

    def _timezone(self, connection, owner):
        row = connection.execute('SELECT timezone FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()
        name = row['timezone'] if row else 'Asia/Seoul'
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise AIError('INVALID_TIMEZONE') from None

    def create(self, owner, key, body):
        body = reading_input(body)
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, 'create', key, body)
            if previous:
                return previous
            deck, cards = self._catalog(body['deck_version_id'])
            name, tz = self._timezone(c, owner)
            now, day, slots, selected = self.clock(), None, [], []
            rid, state = uuid4(), 'draft'
            if body['kind'] == 'daily':
                day = now.astimezone(tz).date()
                existing = c.execute("SELECT * FROM p0_readings WHERE owner_id=%s AND kind='daily' AND local_day=%s AND topic=%s", (owner, day, body['topic'])).fetchone()
                if existing:
                    self._remember(c, owner, 'create', key, body, existing['id'])
                    uow.commit()
                    return reading_view(existing)
                anchor = c.execute('SELECT * FROM p0_daily_draws WHERE owner_id=%s AND local_day=%s AND topic=%s', (owner, day, body['topic'])).fetchone()
                if not anchor:
                    card_id = self.sample([card['id'] for card in cards], 1)[0]
                    expires = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
                    anchor = c.execute('INSERT INTO p0_daily_draws(owner_id,local_day,topic,card_id,deck_version_id,captured_timezone,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *', (owner, day, body['topic'], card_id, body['deck_version_id'], name, expires)).fetchone()
                # The fixed card, deck and captured zone survive deletion of the reading body.
                deck_id, name = str(anchor['deck_version_id']), anchor['captured_timezone']
                cards = self.catalog.list_cards(deck_version_id=deck_id)['items']
                selected = [selected_card(next(card for card in cards if card['id'] == anchor['card_id']))]
                state = 'selected'
            else:
                deck_id = body['deck_version_id']
                slots = private_pool([card['id'] for card in cards], self.sample)
            row = c.execute('''INSERT INTO p0_readings(id,owner_id,kind,topic,question,deck_version_id,local_day,captured_timezone,state,private_slots,selected_cards)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (rid, owner, body['kind'], body['topic'], body.get('question', ''), deck_id, day, name, state, Jsonb(slots), Jsonb(selected))).fetchone()
            self._remember(c, owner, 'create', key, body, rid)
            uow.commit()
            return reading_view(row)

    def get(self, owner, rid):
        with self._uow() as uow:
            self._owner(uow.connection, owner)
            return reading_view(self._row(uow.connection, owner, rid))

    def _draft(self, c, owner, rid, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise AIError('INVALID_VERSION')
        row = self._row(c, owner, rid)
        if row['version'] != expected_version:
            raise AIError('READING_VERSION_CONFLICT', 409)
        if row['kind'] != 'three_card' or row['state'] != 'draft':
            raise AIError('READING_STATE_CONFLICT', 409)
        return row

    def shuffle(self, owner, rid, key, expected_version):
        payload = [expected_version]
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'shuffle:{rid}', key, payload)
            if previous:
                return previous
            row = self._draft(c, owner, rid, expected_version)
            cards = self.catalog.list_cards(deck_version_id=row['deck_version_id'])['items']
            slots = private_pool([card['id'] for card in cards], self.sample)
            row = c.execute('UPDATE p0_readings SET private_slots=%s,version=version+1 WHERE owner_id=%s AND id=%s AND version=%s RETURNING *', (Jsonb(slots), owner, rid, expected_version)).fetchone()
            self._remember(c, owner, f'shuffle:{rid}', key, payload, rid)
            uow.commit()
            return reading_view(row)

    def select(self, owner, rid, key, expected_version, slot_ids):
        payload = [expected_version, list(map(str, slot_ids))]
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'select:{rid}', key, payload)
            if previous:
                return previous
            row = self._draft(c, owner, rid, expected_version)
            cards = self.catalog.list_cards(deck_version_id=row['deck_version_id'])['items']
            selected = select_cards(row['private_slots'], slot_ids, cards)
            row = c.execute("UPDATE p0_readings SET private_slots='[]',selected_cards=%s,state='selected',version=version+1 WHERE owner_id=%s AND id=%s AND version=%s RETURNING *", (Jsonb(selected), owner, rid, expected_version)).fetchone()
            self._remember(c, owner, f'select:{rid}', key, payload, rid)
            uow.commit()
            return reading_view(row)

    def basic(self, owner, rid):
        with self._uow() as uow:
            self._owner(uow.connection, owner)
            row = self._row(uow.connection, owner, rid)
            if not row['selected_cards']:
                raise AIError('READING_NOT_SELECTED', 409)
            cards = row['selected_cards']
            return {'reading_id': str(rid), 'source': 'basic', 'cards': cards,
                    'summary': ' '.join(card['card']['basic_meaning'] for card in cards),
                    'advice': '카드의 뜻을 참고하며 지금 나에게 필요한 선택을 천천히 살펴보세요.',
                    'action': '오늘 할 수 있는 작은 행동 하나를 적어 보세요.'}

    def delete(self, owner, rid):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            self._row(c, owner, rid)
            if self.ai_jobs:
                self.ai_jobs.cancel_for_resource(c, owner, rid)
            c.execute('DELETE FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, rid))
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
            c = uow.connection
            self._owner(c, owner)
            sql, args = 'SELECT * FROM p0_readings WHERE owner_id=%s', [owner]
            for field, value in [('kind', kind), ('topic', topic)]:
                if value:
                    sql += f' AND {field}=%s'
                    args.append(value)
            if after:
                sql += ' AND (created_at,id)<(%s,%s)'
                args += after
            rows = c.execute(sql + ' ORDER BY created_at DESC,id DESC LIMIT %s', args + [limit + 1]).fetchall()
            items, more = rows[:limit], len(rows) > limit
            next_cursor = None
            if more:
                next_cursor = base64.urlsafe_b64encode(json.dumps({'filters': binding, 'after': [items[-1]['created_at'].isoformat(), str(items[-1]['id'])]}).encode()).decode()
            return {'items': [reading_view(row) for row in items], 'next_cursor': next_cursor}

    def snapshot(self, connection, owner, reading_id):
        self._owner(connection, owner)
        row = connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone()
        if not row:
            return None
        return {'id': str(row['id']), 'version': row['version'], 'kind': row['kind'], 'topic': row['topic'],
                'question': row['question'], 'cards': row['selected_cards']}

    def validate(self, connection, owner, reading_id, version):
        self._owner(connection, owner)
        row = connection.execute('SELECT version,selected_cards FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone()
        return bool(row and row['version'] == version and row['selected_cards'])

    def mark_generating(self, connection, owner, reading_id, version):
        self._owner(connection, owner)
        row = connection.execute("UPDATE p0_readings SET state='generating',result=NULL WHERE owner_id=%s AND id=%s AND version=%s AND state IN ('selected','ready','failed') AND jsonb_array_length(selected_cards)>0 RETURNING id", (owner, reading_id, version)).fetchone()
        return row is not None

    def mark_failed(self, connection, owner, reading_id, version):
        # Cancellation during erasure also uses this seam, so do not require an active owner here.
        owner_lock(connection, owner)
        connection.execute("UPDATE p0_readings SET state='failed',result=NULL WHERE owner_id=%s AND id=%s AND version=%s AND state='generating'", (owner, reading_id, version))

    def complete(self, connection, owner, reading_id, version, result):
        self._owner(connection, owner)
        row = connection.execute('SELECT selected_cards FROM p0_readings WHERE owner_id=%s AND id=%s AND version=%s', (owner, reading_id, version)).fetchone()
        if not row or not row['selected_cards']:
            return False
        result = validate_result(result, 'tarot', row['selected_cards'])
        updated = connection.execute("UPDATE p0_readings SET state='ready',result=%s,version=version+1 WHERE owner_id=%s AND id=%s AND version=%s AND state='generating' RETURNING id", (Jsonb(result), owner, reading_id, version)).fetchone()
        return updated is not None

    def source_exists(self, connection, owner, reading_id):
        return bool(connection.execute('SELECT 1 FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone())

    def export_owner(self, connection, owner):
        return {'v2_readings': [reading_view(row) for row in connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s ORDER BY created_at,id', (owner,))]}

    def erase_owner(self, owner, connection=None):
        if hasattr(owner, 'execute'):
            owner, connection = connection, owner
        if connection is None:
            with self._uow() as uow:
                self.erase_owner(owner, connection=uow.connection)
                uow.commit()
            return
        owner_lock(connection, owner)
        if self.ai_jobs:
            for row in connection.execute('SELECT id FROM p0_readings WHERE owner_id=%s', (owner,)).fetchall():
                self.ai_jobs.cancel_for_resource(connection, owner, row['id'])
        for table in ('p0_readings', 'p0_daily_draws', 'p0_reading_commands'):
            connection.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))

    def purge_expired(self):
        with self._uow() as uow:
            c = uow.connection
            anchors = c.execute('DELETE FROM p0_daily_draws WHERE expires_at<=%s RETURNING owner_id', (self.clock(),)).fetchall()
            commands = c.execute('DELETE FROM p0_reading_commands WHERE expires_at<=%s RETURNING owner_id', (self.clock(),)).fetchall()
            uow.commit()
            return len(anchors) + len(commands)
