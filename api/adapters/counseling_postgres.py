"""Owner-bound persistence for counseling; implemented after B02 RED."""
from contextlib import contextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from api.adapters.ai_jobs_postgres import PostgresAIJobsRepository, default_owner_guard, owner_lock
from api.domain.counseling import CounselingError, normalized


class PostgresCounselingRepository:
    def __init__(self, connection):
        self.connection = connection

    def create_session(self, owner, session_id, topic, state, key):
        return self.connection.execute(
            'INSERT INTO counseling_sessions(id,owner_id,topic,state,create_key) '
            'VALUES(%s,%s,%s,%s,%s) RETURNING *',
            (session_id, owner, topic, Jsonb(state), key)).fetchone()

    def session(self, owner, session_id):
        return self.connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s AND id=%s',
                                       (owner, session_id)).fetchone()

    def session_for_key(self, owner, key):
        return self.connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s AND create_key=%s',
                                       (owner, key)).fetchone()

    def update_session(self, owner, session_id, state):
        return self.connection.execute(
            'UPDATE counseling_sessions SET state=%s,version=version+1,updated_at=clock_timestamp() '
            'WHERE owner_id=%s AND id=%s RETURNING *', (Jsonb(state), owner, session_id)).fetchone()

    def sessions(self, owner):
        return self.connection.execute(
            'SELECT * FROM counseling_sessions WHERE owner_id=%s ORDER BY updated_at DESC,id DESC LIMIT 50',
            (owner,)).fetchall()

    def delete_session(self, owner, session_id):
        self.connection.execute('DELETE FROM counseling_sessions WHERE owner_id=%s AND id=%s', (owner, session_id))

    def definitions(self, owner):
        return self.connection.execute(
            'SELECT * FROM personal_glossary WHERE owner_id=%s ORDER BY updated_at DESC,id DESC LIMIT 200',
            (owner,)).fetchall()

    def save_definition(self, owner, proposal, source=None):
        term = proposal['term'].strip()
        entries = self.definitions(owner)
        if len(entries) >= 200 and not any(row['normalized_term'] == normalized(term) for row in entries):
            raise CounselingError('GLOSSARY_LIMIT', 409, '사전은 200개까지 보관해요. 필요 없는 용어를 정리해 주세요.')
        return self.connection.execute('''
            INSERT INTO personal_glossary(id,owner_id,normalized_term,term,definition,evidence_quote,source_message_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(owner_id,normalized_term) DO UPDATE SET
                term=excluded.term, definition=excluded.definition, evidence_quote=excluded.evidence_quote,
                source_message_id=excluded.source_message_id, version=personal_glossary.version+1,
                updated_at=clock_timestamp()
            RETURNING *''',
            (uuid4(), owner, normalized(term), term, proposal['definition'].strip(),
             proposal.get('evidence_quote', ''), source)).fetchone()

    def delete_definition(self, owner, definition_id):
        self.connection.execute('DELETE FROM personal_glossary WHERE owner_id=%s AND id=%s',
                                (owner, definition_id))

    def enqueue(self, owner, session_id, key, fingerprint, kind, payload, consent, command_id=None):
        return self.connection.execute('''
            INSERT INTO counseling_commands(id,owner_id,session_id,request_key,fingerprint,kind,payload,consent_event_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (command_id or uuid4(), owner, session_id, key, fingerprint, kind, Jsonb(payload), consent)).fetchone()

    def command(self, owner, key):
        return self.connection.execute('SELECT * FROM counseling_commands WHERE owner_id=%s AND request_key=%s',
                                       (owner, key)).fetchone()

    def consent(self, owner, event_id, policy_version):
        latest = PostgresAIJobsRepository(self.connection).latest_consent(owner, 'ai_processing')
        if not (latest and latest['granted'] and latest['policy_version'] == policy_version
                and str(latest['id']) == str(event_id)):
            raise CounselingError('CONSENT_REQUIRED', 403, 'AI 이용 동의를 확인해 주세요.')

    def reserve(self, owner, command_id, policy):
        pending = self.connection.execute(
            "SELECT 1 FROM counseling_commands WHERE owner_id=%s AND state IN ('queued','running') LIMIT 1",
            (owner,)).fetchone()
        if pending:
            raise CounselingError('COUNSELING_BUSY', 409, '앞선 이야기를 살펴보고 있어요. 잠시만 기다려 주세요.')
        usage = PostgresAIJobsRepository(self.connection)
        usage.lock_usage()
        counts = usage.usage(owner)
        if counts['owned'] >= policy['owner_daily_limit'] or counts['total'] >= policy['global_daily_limit']:
            raise CounselingError('AI_DAILY_LIMIT', 429, '오늘의 AI 이용 한도에 도달했어요.')
        usage.reserve_usage(command_id, owner)

    def claimed(self, command_id):
        return self.connection.execute("SELECT * FROM counseling_commands WHERE id=%s AND state='running'",
                                       (command_id,)).fetchone()

    def latest_command(self, owner, session_id):
        return self.connection.execute(
            'SELECT * FROM counseling_commands WHERE owner_id=%s AND session_id=%s '
            'ORDER BY created_at DESC,id DESC LIMIT 1', (owner, session_id)).fetchone()

    def finish(self, command_id, error=None, timings=None):
        self.connection.execute(
            "UPDATE counseling_commands SET state=%s,error_code=%s,timings=%s,finished_at=clock_timestamp() "
            "WHERE id=%s AND state='running'",
            ('failed' if error else 'succeeded', error, Jsonb(timings or {}), command_id))


class PostgresCounselingStore:
    def __init__(self, pool):
        self.pool = pool

    @contextmanager
    def transaction(self, owner=None, check_active=True):
        with self.pool.connection() as connection:
            connection.execute('SET LOCAL ROLE yeoun_counseling_app')
            connection.execute("SELECT set_config('yeoun.owner',%s,true)",
                               (str(owner) if owner is not None else '',))
            if owner is not None:
                owner_lock(connection, owner)
                if check_active and not default_owner_guard(connection, owner):
                    raise CounselingError('OWNER_UNAVAILABLE', 401, '기록 연결을 확인해 주세요.')
            yield PostgresCounselingRepository(connection)

    def claim(self):
        with self.pool.connection() as connection:
            connection.execute('SET LOCAL ROLE yeoun_counseling_app')
            return connection.execute('SELECT * FROM public.claim_counseling_command()').fetchone()

    @staticmethod
    def export_owner(connection, owner):
        connection.execute("SELECT set_config('yeoun.owner',%s,true)", (str(owner),))
        return {name: connection.execute(f'SELECT * FROM {name} WHERE owner_id=%s', (owner,)).fetchall()
                for name in ('counseling_sessions', 'personal_glossary')}

    @staticmethod
    def erase_owner(connection, owner):
        connection.execute("SELECT set_config('yeoun.owner',%s,true)", (str(owner),))
        connection.execute('DELETE FROM counseling_sessions WHERE owner_id=%s', (owner,))
        connection.execute('DELETE FROM personal_glossary WHERE owner_id=%s', (owner,))
