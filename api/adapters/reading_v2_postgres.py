from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.application.reading_v2_ports import DailyAnchor, ReadingCommand, ReadingRecord


def reading_view(row):
    return {'id': str(row['id']), 'kind': row['kind'], 'topic': row['topic'], 'question': row['question'],
            'deck_version_id': str(row['deck_version_id']), 'local_day': str(row['local_day']) if row['local_day'] else None,
            'captured_timezone': row['captured_timezone'], 'state': row['state'], 'version': row['version'],
            'slots': [{'id': slot['id'], 'index': slot['index']} for slot in row['private_slots']],
            'selected_cards': row['selected_cards'], 'result': row['result'],
            'created_at': row['created_at'].isoformat()}


def reading_record(row):
    if row is None:
        return None
    return ReadingRecord(id=row['id'], kind=row['kind'], state=row['state'], version=row['version'],
                         deck_version_id=row['deck_version_id'], private_slots=row['private_slots'],
                         selected_cards=row['selected_cards'], created_at=row['created_at'], view=reading_view(row))


def daily_anchor(row):
    return DailyAnchor(row['card_id'], row['deck_version_id'], row['captured_timezone']) if row else None


class PostgresReadingV2Repository:
    def __init__(self, connection):
        self.connection = connection

    @property
    def transaction(self):
        return self.connection

    def lock_owner(self, owner):
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'yeoun:owner:{owner}',))

    def owner_available(self, owner, guard=None):
        if guard is not None:
            return guard(self.connection, owner) is not False
        if not self.connection.execute('SELECT 1 FROM app_sessions WHERE id=%s', (owner,)).fetchone():
            return False
        if self.connection.execute("SELECT to_regclass('data_privacy_profiles') AS t").fetchone()['t']:
            row = self.connection.execute('SELECT state FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()
            return not row or row['state'] == 'active'
        return True

    def timezone(self, owner):
        row = self.connection.execute('SELECT timezone FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()
        return row['timezone'] if row else None

    def get(self, owner, reading_id):
        return reading_record(self.connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone())

    def command(self, owner, scope, key, now):
        row = self.connection.execute('''SELECT fingerprint,reading_id FROM p0_reading_commands
            WHERE owner_id=%s AND scope=%s AND key=%s AND expires_at>%s''', (owner, scope, key, now)).fetchone()
        return ReadingCommand(row['fingerprint'], row['reading_id']) if row else None

    def discard_command(self, owner, scope, key):
        self.connection.execute('DELETE FROM p0_reading_commands WHERE owner_id=%s AND scope=%s AND key=%s', (owner, scope, key))

    def remember(self, owner, scope, key, fingerprint, reading_id, expires_at):
        self.connection.execute('''INSERT INTO p0_reading_commands(owner_id,scope,key,fingerprint,reading_id,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s)''', (owner, scope, key, fingerprint, reading_id, expires_at))

    def find_daily(self, owner, day, topic):
        return reading_record(self.connection.execute("SELECT * FROM p0_readings WHERE owner_id=%s AND kind='daily' AND local_day=%s AND topic=%s",
                                                      (owner, day, topic)).fetchone())

    def daily_anchor(self, owner, day, topic):
        return daily_anchor(self.connection.execute('SELECT * FROM p0_daily_draws WHERE owner_id=%s AND local_day=%s AND topic=%s',
                                                    (owner, day, topic)).fetchone())

    def add_daily_anchor(self, owner, day, topic, card_id, deck_version_id, timezone, expires_at):
        return daily_anchor(self.connection.execute('''INSERT INTO p0_daily_draws
            (owner_id,local_day,topic,card_id,deck_version_id,captured_timezone,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (owner, day, topic, card_id, deck_version_id, timezone, expires_at)).fetchone())

    def add(self, owner, reading_id, *, kind, topic, question, deck_version_id, local_day,
            captured_timezone, state, private_slots, selected_cards):
        return reading_record(self.connection.execute('''INSERT INTO p0_readings
            (id,owner_id,kind,topic,question,deck_version_id,local_day,captured_timezone,state,private_slots,selected_cards)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (reading_id, owner, kind, topic, question, deck_version_id, local_day, captured_timezone,
             state, Jsonb(private_slots), Jsonb(selected_cards))).fetchone())

    def shuffle(self, owner, reading_id, version, slots):
        return reading_record(self.connection.execute('''UPDATE p0_readings SET private_slots=%s,version=version+1
            WHERE owner_id=%s AND id=%s AND version=%s RETURNING *''', (Jsonb(slots), owner, reading_id, version)).fetchone())

    def select(self, owner, reading_id, version, cards):
        return reading_record(self.connection.execute("""UPDATE p0_readings SET private_slots='[]',selected_cards=%s,
            state='selected',version=version+1 WHERE owner_id=%s AND id=%s AND version=%s RETURNING *""",
            (Jsonb(cards), owner, reading_id, version)).fetchone())

    def delete(self, owner, reading_id):
        self.connection.execute('DELETE FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id))

    def list(self, owner, *, limit, after=None, kind=None, topic=None):
        query, args = 'SELECT * FROM p0_readings WHERE owner_id=%s', [owner]
        for field, value in [('kind', kind), ('topic', topic)]:
            if value:
                query += f' AND {field}=%s'
                args.append(value)
        if after:
            query += ' AND (created_at,id)<(%s,%s)'
            args += list(after)
        rows = self.connection.execute(query + ' ORDER BY created_at DESC,id DESC LIMIT %s', args + [limit]).fetchall()
        return [reading_record(row) for row in rows]

    def snapshot(self, owner, reading_id):
        row = self.connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone()
        if not row:
            return None
        return {'id': str(row['id']), 'version': row['version'], 'kind': row['kind'], 'topic': row['topic'],
                'question': row['question'], 'cards': row['selected_cards']}

    def mark_generating(self, owner, reading_id, version):
        return self.connection.execute("""UPDATE p0_readings SET state='generating',result=NULL
            WHERE owner_id=%s AND id=%s AND version=%s AND state IN ('selected','ready','failed')
            AND jsonb_array_length(selected_cards)>0 RETURNING id""", (owner, reading_id, version)).fetchone() is not None

    def mark_failed(self, owner, reading_id, version):
        self.connection.execute("""UPDATE p0_readings SET state='failed',result=NULL
            WHERE owner_id=%s AND id=%s AND version=%s AND state='generating'""", (owner, reading_id, version))

    def complete(self, owner, reading_id, version, result):
        return self.connection.execute("""UPDATE p0_readings SET state='ready',result=%s,version=version+1
            WHERE owner_id=%s AND id=%s AND version=%s AND state='generating' RETURNING id""",
            (Jsonb(result), owner, reading_id, version)).fetchone() is not None

    def exists(self, owner, reading_id):
        return bool(self.connection.execute('SELECT 1 FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, reading_id)).fetchone())

    def all_owned(self, owner):
        return [reading_record(row) for row in self.connection.execute('SELECT * FROM p0_readings WHERE owner_id=%s ORDER BY created_at,id', (owner,))]

    def owned_ids(self, owner):
        return [row['id'] for row in self.connection.execute('SELECT id FROM p0_readings WHERE owner_id=%s', (owner,))]

    def erase_owner(self, owner):
        for table in ('p0_readings', 'p0_daily_draws', 'p0_reading_commands'):
            self.connection.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))

    def purge_expired(self, now):
        anchors = self.connection.execute('DELETE FROM p0_daily_draws WHERE expires_at<=%s', (now,)).rowcount
        commands = self.connection.execute('DELETE FROM p0_reading_commands WHERE expires_at<=%s', (now,)).rowcount
        return anchors + commands


class PostgresReadingV2UnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def repository_for(self, transaction):
        return PostgresReadingV2Repository(transaction)

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.readings = self.repository_for(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
