"""Short PostgreSQL units of work; workers close these before external I/O."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from uuid import UUID


def owner_lock(connection, owner):
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'yeoun:owner:{owner}',))


def default_owner_guard(connection, owner):
    if not connection.execute('SELECT 1 FROM app_sessions WHERE id=%s', (owner,)).fetchone():
        return False
    if connection.execute("SELECT to_regclass('data_privacy_profiles') AS t").fetchone()['t']:
        row = connection.execute('SELECT state FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()
        return not row or row['state'] == 'active'
    return True


def _public(row, fields):
    return {field: str(row[field]) if isinstance(row[field], UUID) else
            row[field].isoformat() if hasattr(row[field], 'isoformat') else row[field] for field in fields}


class PostgresAIJobsRepository:
    """Persistence snapshots and mutations; application supplies policy decisions."""
    def __init__(self, connection):
        self.transaction = connection

    def lock_owner(self, owner):
        owner_lock(self.transaction, owner)

    def owner_available(self, owner, guard=None):
        return (guard or default_owner_guard)(self.transaction, owner) is not False

    def idempotency(self, owner, scope, key):
        row = self.transaction.execute('SELECT fingerprint,response FROM p0_ai_idempotency WHERE owner_id=%s AND scope=%s AND key=%s AND expires_at>clock_timestamp()', (owner, scope, key)).fetchone()
        if not row:
            self.transaction.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND scope=%s AND key=%s', (owner, scope, key))
        return row

    def remember(self, owner, scope, key, fingerprint, resource, response):
        self.transaction.execute('INSERT INTO p0_ai_idempotency(owner_id,scope,key,fingerprint,resource_id,response) VALUES(%s,%s,%s,%s,%s,%s)', (owner, scope, key, fingerprint, resource, Jsonb(response)))

    def latest_consent(self, owner, kind):
        return self.transaction.execute('SELECT * FROM ai_consent_events WHERE owner_id=%s AND kind=%s ORDER BY occurred_at DESC,id DESC LIMIT 1', (owner, kind)).fetchone()

    def add_consent(self, event_id, owner, kind, policy_version, granted):
        return self.transaction.execute('INSERT INTO ai_consent_events(id,owner_id,kind,policy_version,granted) VALUES(%s,%s,%s,%s,%s) RETURNING *', (event_id, owner, kind, policy_version, granted)).fetchone()

    def pending_for_consent(self, owner, kind):
        if kind == 'journal_context':
            return self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND context_consent_id IS NOT NULL AND state IN ('queued','running','cancel_requested')", (owner,)).fetchall()
        return self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND consent_event_id IS NOT NULL AND ((kind='transcription')=%s) AND state IN ('queued','running','cancel_requested')", (owner, kind == 'cloud_transcription')).fetchall()

    def consents(self, owner, after, limit):
        sql, args = 'SELECT * FROM ai_consent_events WHERE owner_id=%s', [owner]
        if after:
            sql += ' AND (occurred_at,id)<(%s,%s)'
            args += after
        return self.transaction.execute(sql + ' ORDER BY occurred_at DESC,id DESC LIMIT %s', args + [limit]).fetchall()

    @staticmethod
    def consent_view(row):
        return _public(row, ('id', 'kind', 'policy_version', 'granted', 'occurred_at'))

    @staticmethod
    def consent_cursor(row):
        return [row['occurred_at'].isoformat(), str(row['id'])]

    def add_conversation(self, cid, owner, topic, tone):
        return self.transaction.execute('INSERT INTO ai_conversations(id,owner_id,topic,tone) VALUES(%s,%s,%s,%s) RETURNING *', (cid, owner, topic, tone)).fetchone()

    def conversation(self, owner, cid):
        return self.transaction.execute('SELECT * FROM ai_conversations WHERE owner_id=%s AND id=%s', (owner, cid)).fetchone()

    def conversations(self, owner, after, limit):
        sql, args = 'SELECT * FROM ai_conversations WHERE owner_id=%s', [owner]
        if after:
            sql += ' AND (created_at,id)<(%s,%s)'
            args += after
        return self.transaction.execute(sql + ' ORDER BY created_at DESC,id DESC LIMIT %s', args + [limit]).fetchall()

    @staticmethod
    def conversation_view(row):
        return _public(row, ('id', 'topic', 'tone', 'state', 'created_at', 'updated_at'))

    @staticmethod
    def conversation_cursor(row):
        return [row['created_at'].isoformat(), str(row['id'])]

    def close_conversation(self, owner, cid):
        return self.transaction.execute("UPDATE ai_conversations SET state='closed',version=version+1,updated_at=clock_timestamp() WHERE id=%s AND owner_id=%s RETURNING *", (cid, owner)).fetchone()

    def pending_for_conversation(self, owner, cid):
        return self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND conversation_id=%s AND state IN ('queued','running','cancel_requested')", (owner, cid)).fetchall()

    def delete_conversation(self, owner, cid):
        resources = [r['id'] for r in self.transaction.execute('SELECT id FROM ai_conversation_turns WHERE conversation_id=%s', (cid,))]
        resources += [r['id'] for r in self.transaction.execute('SELECT id FROM p0_ai_jobs WHERE conversation_id=%s', (cid,))] + [cid]
        self.transaction.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND resource_id=ANY(%s)', (owner, resources))
        self.transaction.execute('DELETE FROM ai_conversations WHERE id=%s AND owner_id=%s', (cid, owner))

    def journal(self, owner, entry_id):
        return self.transaction.execute('SELECT id,version,body,day,mood FROM journal_entries WHERE session_id=%s AND id=%s', (owner, entry_id)).fetchone()

    def journal_version(self, owner, entry_id):
        row = self.transaction.execute('SELECT version FROM journal_entries WHERE session_id=%s AND id=%s', (owner, entry_id)).fetchone()
        return row['version'] if row else None

    def active_job_exists(self, owner):
        if self.transaction.execute("SELECT 1 FROM p0_ai_jobs WHERE owner_id=%s AND state IN ('queued','running','cancel_requested') LIMIT 1", (owner,)).fetchone():
            return True
        return bool(self.transaction.execute("SELECT 1 FROM experience_ai_jobs WHERE session_id=%s AND state='pending' AND lease_until>clock_timestamp() LIMIT 1", (owner,)).fetchone())

    def lock_usage(self):
        self.transaction.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('ai:global-daily-reservations',))

    def usage(self, owner):
        return self.transaction.execute("""SELECT count(*) AS total,count(*) FILTER(WHERE owner_id=%s) AS owned FROM (
          SELECT owner_id,created_at FROM p0_ai_usage_attempts UNION ALL
          SELECT session_id AS owner_id,created_at FROM ai_usage_attempts
        ) AS attempts WHERE created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""", (owner,)).fetchone()

    def add_job(self, job):
        return self.transaction.execute('''INSERT INTO p0_ai_jobs(id,owner_id,kind,resource_id,resource_version,conversation_id,consent_event_id,context_consent_id,payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (job['id'], job['owner_id'], job['kind'], job['resource_id'], job['resource_version'], job.get('conversation_id'), job.get('consent_event_id'), job.get('context_consent_id'), Jsonb(job['payload']))).fetchone()

    def reserve_usage(self, job_id, owner):
        self.transaction.execute('INSERT INTO p0_ai_usage_attempts(id,owner_id) VALUES(%s,%s)', (job_id, owner))

    def add_source(self, job_id, entry_id, version):
        self.transaction.execute('INSERT INTO p0_ai_context_sources(job_id,entry_id,entry_version) VALUES(%s,%s,%s)', (job_id, entry_id, version))

    def duplicate_turn(self, owner, cid, message_id):
        return self.transaction.execute('SELECT id,fingerprint FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND client_message_id=%s', (owner, cid, message_id)).fetchone()

    def latest_job(self, owner, resource):
        return self.transaction.execute('SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND resource_id=%s ORDER BY created_at DESC,id DESC LIMIT 1', (owner, resource)).fetchone()

    def next_turn_index(self, cid):
        return self.transaction.execute('SELECT coalesce(max(turn_index),0)+1 AS n FROM ai_conversation_turns WHERE conversation_id=%s', (cid,)).fetchone()['n']

    def add_turn(self, turn):
        self.transaction.execute('''INSERT INTO ai_conversation_turns(id,owner_id,conversation_id,client_message_id,fingerprint,turn_index,text,answers,input_source)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (turn['id'], turn['owner_id'], turn['conversation_id'], turn['client_message_id'], turn['fingerprint'], turn['turn_index'], turn['text'], Jsonb(turn['answers']), turn['input_source']))
        self.transaction.execute('UPDATE ai_conversations SET updated_at=clock_timestamp() WHERE id=%s', (turn['conversation_id'],))

    def turn(self, owner, tid, conversation=None):
        sql, args = 'SELECT * FROM ai_conversation_turns WHERE owner_id=%s AND id=%s', [owner, tid]
        if conversation is not None:
            sql += ' AND conversation_id=%s'
            args.append(conversation)
        return self.transaction.execute(sql, args).fetchone()

    def turns(self, owner, cid, after, limit):
        return self.transaction.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND turn_index>%s ORDER BY turn_index LIMIT %s', (owner, cid, after, limit)).fetchall()

    @staticmethod
    def turn_view(row):
        return _public(row, ('id', 'conversation_id', 'turn_index', 'text', 'answers', 'input_source', 'state', 'assistant_result', 'version', 'created_at'))

    def set_turn_state(self, owner, tid, state, result=None, advance_version=False):
        increment = ',version=version+1' if advance_version else ''
        self.transaction.execute('UPDATE ai_conversation_turns SET state=%s,assistant_result=%s' + increment + ' WHERE owner_id=%s AND id=%s', (state, Jsonb(result) if result is not None else None, owner, tid))

    def job(self, owner, job_id, for_update=False):
        return self.transaction.execute('SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND id=%s' + (' FOR UPDATE' if for_update else ''), (owner, job_id)).fetchone()

    @staticmethod
    def job_view(row):
        result = _public(row, ('id', 'kind', 'state', 'version', 'resource_id', 'result_url', 'created_at', 'finished_at'))
        code = row['error_code']
        result['error'] = {'code': code, 'message': code, 'retryable': code not in ('AI_REFUSAL', 'RESOURCE_CHANGED')} if code else None
        return result

    @staticmethod
    def accepted(row):
        return {'job_id': str(row['id']), 'resource_id': str(row['resource_id']), 'state': row['state'],
                'status_url': f"/v2/ai/jobs/{row['id']}", 'events_url': f"/v2/ai/jobs/{row['id']}/events"}

    def cancel_job(self, job_id, state):
        self.transaction.execute("UPDATE p0_ai_jobs SET state=%s,version=version+1,result_url=NULL,finished_at=CASE WHEN %s='cancelled' THEN clock_timestamp() ELSE NULL END WHERE id=%s", (state, state, job_id))
        self.transaction.execute('DELETE FROM p0_ai_context_sources WHERE job_id=%s', (job_id,))

    def context_sources(self, job_id):
        return self.transaction.execute('SELECT * FROM p0_ai_context_sources WHERE job_id=%s', (job_id,)).fetchall()

    def history(self, owner, cid, before, limit):
        return self.transaction.execute("SELECT text,assistant_result FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND turn_index<%s AND state='ready' ORDER BY turn_index DESC LIMIT %s", (owner, cid, before, limit)).fetchall()

    def finish_job(self, job_id, state, code, url):
        self.transaction.execute('UPDATE p0_ai_jobs SET state=%s,error_code=%s,result_url=%s,finished_at=clock_timestamp(),version=version+1,lease_until=NULL WHERE id=%s', (state, code, url, job_id))

    def claim_next(self, lease, seconds):
        row = self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE state='queued' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if row:
            self.transaction.execute("UPDATE p0_ai_jobs SET state='running',lease_id=%s,lease_until=clock_timestamp()+(%s * interval '1 second'),version=version+1 WHERE id=%s", (lease, seconds, row['id']))
        return row

    def mark_provider_started(self, job_id, lease, started):
        self.transaction.execute('UPDATE p0_ai_jobs SET provider_started=%s WHERE id=%s AND lease_id=%s', (started, job_id, lease))

    def add_run(self, run):
        self.transaction.execute('''INSERT INTO p0_ai_runs(id,job_id,model_id,prompt_version,schema_version,elapsed_ms,request_id,response_id,input_tokens,output_tokens)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', tuple(run.get(k) for k in ('id', 'job_id', 'model_id', 'prompt_version', 'schema_version', 'elapsed_ms', 'request_id', 'response_id', 'input_tokens', 'output_tokens')))

    def heartbeat(self, job_id, lease, seconds):
        return self.transaction.execute("UPDATE p0_ai_jobs SET lease_until=clock_timestamp()+(%s * interval '1 second') WHERE id=%s AND lease_id=%s AND state='running' RETURNING id", (seconds, job_id, lease)).fetchone() is not None

    def recovery_candidates(self):
        return self.transaction.execute("SELECT id,owner_id FROM p0_ai_jobs WHERE state IN ('running','cancel_requested') AND lease_until<clock_timestamp()").fetchall()

    def expired_job(self, job_id):
        return self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE id=%s AND state IN ('running','cancel_requested') AND lease_until<clock_timestamp() FOR UPDATE", (job_id,)).fetchone()

    def requeue(self, job_id):
        self.transaction.execute("UPDATE p0_ai_jobs SET state='queued',lease_id=NULL,lease_until=NULL,version=version+1 WHERE id=%s", (job_id,))

    def pending_for_resource(self, owner, resource):
        return self.transaction.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND resource_id=%s AND state IN ('queued','running','cancel_requested')", (owner, resource)).fetchall()

    def pending_for_journal(self, owner, entry_id):
        return self.transaction.execute("SELECT j.* FROM p0_ai_jobs j JOIN p0_ai_context_sources s ON s.job_id=j.id WHERE j.owner_id=%s AND s.entry_id=%s AND j.state IN ('queued','running','cancel_requested')", (owner, entry_id)).fetchall()

    def audio_path(self, rid):
        row = self.transaction.execute('SELECT audio_path FROM p0_ai_transcriptions WHERE id=%s', (rid,)).fetchone()
        return row['audio_path'] if row else None

    def detach_audio(self, rid):
        self.transaction.execute('UPDATE p0_ai_transcriptions SET audio_path=NULL WHERE id=%s', (rid,))

    def add_transcription(self, rid, owner, path, mime, duration, seconds):
        self.transaction.execute("INSERT INTO p0_ai_transcriptions(id,owner_id,audio_path,mime_type,duration_ms,expires_at) VALUES(%s,%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))", (rid, owner, path, mime, duration, seconds))

    def transcription(self, owner, rid, live_only=False):
        suffix = " AND expires_at>clock_timestamp() AND state!='deleted'" if live_only else ''
        return self.transaction.execute('SELECT *,expires_at<=clock_timestamp() AS expired FROM p0_ai_transcriptions WHERE owner_id=%s AND id=%s' + suffix, (owner, rid)).fetchone()

    @staticmethod
    def transcription_view(row):
        return _public(row, ('id', 'state', 'transcript', 'expires_at'))

    def set_transcription_state(self, rid, state, transcript=None, advance_version=False, keep_deleted=False):
        increment = ',version=version+1' if advance_version else ''
        suffix = " AND state!='deleted'" if keep_deleted else ''
        self.transaction.execute('UPDATE p0_ai_transcriptions SET state=%s,transcript=%s' + increment + ' WHERE id=%s' + suffix, (state, transcript, rid))

    def forget_resource_commands(self, owner, rid):
        self.transaction.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND resource_id=%s', (owner, rid))

    def expired_transcriptions(self):
        return self.transaction.execute("SELECT id,owner_id FROM p0_ai_transcriptions WHERE expires_at<=clock_timestamp() AND (state!='deleted' OR transcript IS NOT NULL OR audio_path IS NOT NULL)").fetchall()

    def transcriptions(self, owner):
        return self.transaction.execute('SELECT id FROM p0_ai_transcriptions WHERE owner_id=%s', (owner,)).fetchall()

    def erase_owner(self, owner):
        for table in ('p0_ai_jobs', 'ai_conversations', 'ai_consent_events', 'p0_ai_transcriptions', 'p0_ai_idempotency'):
            self.transaction.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))
        self.transaction.execute('UPDATE p0_ai_usage_attempts SET owner_id=NULL WHERE owner_id=%s', (owner,))

    def export_owner(self, owner):
        return {'consents': [self.consent_view(r) for r in self.transaction.execute('SELECT * FROM ai_consent_events WHERE owner_id=%s ORDER BY occurred_at,id', (owner,))],
                'conversations': [self.conversation_view(r) for r in self.transaction.execute('SELECT * FROM ai_conversations WHERE owner_id=%s ORDER BY created_at,id', (owner,))],
                'turns': [self.turn_view(r) for r in self.transaction.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s ORDER BY conversation_id,turn_index', (owner,))],
                'transcriptions': [self.transcription_view(r) for r in self.transaction.execute("SELECT * FROM p0_ai_transcriptions WHERE owner_id=%s AND expires_at>clock_timestamp() AND state!='deleted'", (owner,))]}

    def turn_exists(self, owner, turn_id):
        return bool(self.transaction.execute('SELECT 1 FROM ai_conversation_turns WHERE owner_id=%s AND id=%s', (owner, turn_id)).fetchone())


class PostgresAIJobsUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.ai_jobs = self.repository_for(self.connection)
        return self

    @staticmethod
    def repository_for(transaction):
        return PostgresAIJobsRepository(transaction)

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
