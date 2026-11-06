"""Short PostgreSQL transactions; all provider calls happen after they close."""
from contextlib import contextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from api.adapters.ai_jobs_postgres import PostgresAIJobsRepository, default_owner_guard, owner_lock
from api.domain.counseling import CounselingError, normalized


class PostgresCounselingRepository:
    def __init__(self, connection):
        self.connection=connection
        self.ai=PostgresAIJobsRepository(connection)

    def session(self,owner,session_id):
        return self.connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s AND id=%s',
                                       (owner,session_id)).fetchone()

    def session_for_key(self,owner,key):
        return self.connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s AND create_key=%s',(owner,key)).fetchone()

    def create_session(self,owner,session_id,topic,state,key):
        return self.connection.execute('''INSERT INTO counseling_sessions(id,owner_id,topic,state,create_key)
            VALUES(%s,%s,%s,%s,%s) RETURNING *''',(session_id,owner,topic,Jsonb(state),key)).fetchone()

    def update_session(self,owner,session_id,state):
        return self.connection.execute('''UPDATE counseling_sessions SET state=%s,version=version+1,
            updated_at=clock_timestamp() WHERE owner_id=%s AND id=%s RETURNING *''',
            (Jsonb(state),owner,session_id)).fetchone()

    def definitions(self,owner):
        return self.connection.execute('SELECT * FROM personal_glossary WHERE owner_id=%s ORDER BY updated_at DESC LIMIT 200',(owner,)).fetchall()

    def save_definition(self,owner,proposal,source=None):
        rows=self.definitions(owner)
        if len(rows)>=200 and not any(d['normalized_term']==normalized(proposal['term']) for d in rows):
            raise CounselingError('GLOSSARY_LIMIT',409,'사전은 200개까지 보관해요. 필요 없는 용어를 먼저 정리해 주세요.')
        return self.connection.execute('''INSERT INTO personal_glossary
            (id,owner_id,normalized_term,term,definition,evidence_quote,source_message_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_id,normalized_term) DO UPDATE SET
            term=EXCLUDED.term,definition=EXCLUDED.definition,evidence_quote=EXCLUDED.evidence_quote,
            source_message_id=EXCLUDED.source_message_id,version=personal_glossary.version+1,
            updated_at=clock_timestamp() RETURNING *''',
            (uuid4(),owner,normalized(proposal['term']),proposal['term'].strip(),proposal['definition'].strip(),
             proposal.get('evidence_quote',''),source)).fetchone()

    def delete_definition(self,owner,definition_id):
        self.connection.execute('DELETE FROM personal_glossary WHERE owner_id=%s AND id=%s',(owner,definition_id))

    def command(self,owner,key):
        return self.connection.execute('SELECT * FROM counseling_commands WHERE owner_id=%s AND request_key=%s',(owner,key)).fetchone()

    def latest_command(self,owner,session_id):
        return self.connection.execute('''SELECT * FROM counseling_commands WHERE owner_id=%s AND session_id=%s
            ORDER BY created_at DESC,id DESC LIMIT 1''',(owner,session_id)).fetchone()

    def claimed(self,command_id):
        return self.connection.execute("SELECT * FROM counseling_commands WHERE id=%s AND state='running'",(command_id,)).fetchone()

    def delete_session(self,owner,sid):
        self.connection.execute('DELETE FROM counseling_sessions WHERE owner_id=%s AND id=%s',(owner,sid))

    def sessions(self,owner):
        return self.connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s ORDER BY updated_at DESC,id DESC LIMIT 50',(owner,)).fetchall()

    def enqueue(self,owner,session_id,key,fingerprint,kind,payload,consent,command_id=None):
        return self.connection.execute('''INSERT INTO counseling_commands
            (id,owner_id,session_id,request_key,fingerprint,kind,payload,consent_event_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (command_id or uuid4(),owner,session_id,key,fingerprint,kind,Jsonb(payload),consent)).fetchone()

    def finish(self,command_id,error=None,timings=None):
        self.connection.execute('''UPDATE counseling_commands SET state=%s,error_code=%s,
            timings=%s,finished_at=clock_timestamp() WHERE id=%s AND state='running' ''',
            ('failed' if error else 'succeeded',error,Jsonb(timings or {}),command_id))

    def consent(self,owner,event_id,policy_version):
        row=self.ai.latest_consent(owner,'ai_processing')
        if not row or not row['granted'] or row['policy_version']!=policy_version or str(row['id'])!=str(event_id):
            raise CounselingError('CONSENT_REQUIRED',403,'AI 이용 동의를 확인해 주세요.')

    def reserve(self,owner,command_id,policy):
        if self.connection.execute("SELECT 1 FROM counseling_commands WHERE owner_id=%s AND state IN ('queued','running') LIMIT 1",(owner,)).fetchone():
            raise CounselingError('COUNSELING_BUSY',409,'앞선 이야기를 살펴보고 있어요. 잠시만 기다려 주세요.')
        self.ai.lock_usage();usage=self.ai.usage(owner)
        if usage['owned']>=policy['owner_daily_limit'] or usage['total']>=policy['global_daily_limit']:
            raise CounselingError('AI_DAILY_LIMIT',429,'오늘의 AI 이용 한도에 도달했어요.')
        self.ai.reserve_usage(command_id,owner)


class PostgresCounselingStore:
    def __init__(self,pool):self.pool=pool

    @contextmanager
    def transaction(self,owner=None,check_active=True):
        with self.pool.connection() as c:
            c.execute('SET LOCAL ROLE yeoun_counseling_app')
            c.execute("SELECT set_config('yeoun.owner',%s,true)",(str(owner) if owner else '',))
            if owner is not None:
                owner_lock(c,owner)
                if check_active and not default_owner_guard(c,owner):
                    raise CounselingError('OWNER_UNAVAILABLE',401,'기록 연결을 확인해 주세요.')
            yield PostgresCounselingRepository(c)

    def claim(self):
        with self.pool.connection() as c:
            c.execute('SET LOCAL ROLE yeoun_counseling_app')
            return c.execute('SELECT * FROM public.claim_counseling_command()').fetchone()

    @staticmethod
    def export_owner(connection,owner):
        connection.execute("SELECT set_config('yeoun.owner',%s,true)",(str(owner),))
        return {'counseling_sessions':connection.execute('SELECT * FROM counseling_sessions WHERE owner_id=%s',(owner,)).fetchall(),
                'personal_glossary':connection.execute('SELECT * FROM personal_glossary WHERE owner_id=%s',(owner,)).fetchall()}

    @staticmethod
    def erase_owner(connection,owner):
        connection.execute("SELECT set_config('yeoun.owner',%s,true)",(str(owner),))
        connection.execute('DELETE FROM counseling_sessions WHERE owner_id=%s',(owner,))
        connection.execute('DELETE FROM personal_glossary WHERE owner_id=%s',(owner,))
