"""Durable P0 jobs, explicit consent and owner-scoped conversation operations."""
import base64
import json
import re
import time
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from api.domain.ai_jobs import AIError, fingerprint, load_policy, safety_result, validate_result, validate_turn


TERMINAL = ('succeeded', 'failed', 'cancelled')
TOPICS = ('relationship', 'work', 'self', 'daily', 'care', 'start')


def owner_lock(connection, owner):
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'yeoun:owner:{owner}',))


def default_owner_guard(connection, owner):
    if not connection.execute('SELECT 1 FROM app_sessions WHERE id=%s', (owner,)).fetchone():
        raise AIError('OWNER_UNAVAILABLE', 401)
    if connection.execute("SELECT to_regclass('data_privacy_profiles') AS t").fetchone()['t']:
        row = connection.execute('SELECT state FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()
        if row and row['state'] != 'active':
            raise AIError('OWNER_UNAVAILABLE', 401)


def _public(row, fields):
    return {field: (str(row[field]) if isinstance(row[field], UUID) else
                    row[field].isoformat() if hasattr(row[field], 'isoformat') else row[field]) for field in fields}


def conversation_view(row):
    return _public(row, ('id', 'topic', 'tone', 'state', 'created_at', 'updated_at'))


def consent_view(row):
    return _public(row, ('id', 'kind', 'policy_version', 'granted', 'occurred_at'))


def turn_view(row):
    return _public(row, ('id', 'conversation_id', 'turn_index', 'text', 'answers', 'input_source',
                         'state', 'assistant_result', 'version', 'created_at'))


def job_view(row):
    result = _public(row, ('id', 'kind', 'state', 'version', 'resource_id', 'result_url', 'created_at', 'finished_at'))
    code = row['error_code']
    result['error'] = {'code': code, 'message': code, 'retryable': code not in ('AI_REFUSAL', 'RESOURCE_CHANGED')} if code else None
    return result


def accepted(row):
    return {'job_id': str(row['id']), 'resource_id': str(row['resource_id']), 'state': row['state'],
            'status_url': f"/v2/ai/jobs/{row['id']}", 'events_url': f"/v2/ai/jobs/{row['id']}/events"}


class AIJobsService:
    def __init__(self, uow_factory, provider=None, transcription_provider=None, reading_source=None,
                 owner_guard=None, audio_store=None, policy=None):
        self._uow = uow_factory
        self.provider, self.transcription_provider = provider, transcription_provider
        self.reading_source = reading_source
        self.owner_guard = owner_guard or default_owner_guard
        self.audio_store = audio_store
        self.policy = {**load_policy(), **(policy or {})}

    def _owner(self, connection, owner):
        owner_lock(connection, owner)
        if self.owner_guard(connection, owner) is False:
            raise AIError('OWNER_UNAVAILABLE', 401)

    def _once(self, connection, owner, scope, key, payload):
        row = connection.execute('SELECT fingerprint,response FROM p0_ai_idempotency WHERE owner_id=%s AND scope=%s AND key=%s AND expires_at>clock_timestamp()', (owner, scope, key)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint(payload):
                raise AIError('IDEMPOTENCY_CONFLICT', 409)
            return row['response']
        connection.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND scope=%s AND key=%s', (owner, scope, key))

    def _remember(self, connection, owner, scope, key, payload, resource, response):
        connection.execute('INSERT INTO p0_ai_idempotency(owner_id,scope,key,fingerprint,resource_id,response) VALUES(%s,%s,%s,%s,%s,%s)',
                           (owner, scope, key, fingerprint(payload), resource, Jsonb(response)))

    def _require_consent(self, connection, owner, kind, event_id=None):
        row = connection.execute('SELECT * FROM ai_consent_events WHERE owner_id=%s AND kind=%s ORDER BY occurred_at DESC,id DESC LIMIT 1', (owner, kind)).fetchone()
        if not row or not row['granted'] or row['policy_version'] != self.policy['policy_version'] or (event_id and str(row['id']) != str(event_id)):
            raise AIError('CONSENT_REQUIRED', 403)
        return row['id']

    def record_consent(self, owner, key, kind, policy_version, granted):
        if kind not in ('ai_processing', 'journal_context', 'cloud_transcription') or type(granted) is not bool:
            raise AIError('INVALID_CONSENT')
        if policy_version != self.policy['policy_version']:
            raise AIError('CONSENT_POLICY_OUTDATED', 409)
        payload = [kind, policy_version, granted]
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, 'consent', key, payload)
            if previous:
                return previous
            row = c.execute('INSERT INTO ai_consent_events(id,owner_id,kind,policy_version,granted) VALUES(%s,%s,%s,%s,%s) RETURNING *', (uuid4(), owner, kind, policy_version, granted)).fetchone()
            # A new consent event supersedes the captured event of pending jobs as well.
            if kind == 'journal_context':
                jobs = c.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND context_consent_id IS NOT NULL AND state IN ('queued','running','cancel_requested')", (owner,)).fetchall()
            else:
                target = 'transcription' if kind == 'cloud_transcription' else 'ai'
                jobs = c.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND ((kind='transcription')=%s) AND state IN ('queued','running','cancel_requested')", (owner, target == 'transcription')).fetchall()
            for job in jobs:
                self._cancel(c, job)
            result = consent_view(row)
            self._remember(c, owner, 'consent', key, payload, row['id'], result)
            uow.commit()
            return result

    def _cursor(self, value, scope, owner):
        if not value:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
            if data['scope'] != scope or data['owner'] != str(owner):
                raise ValueError()
            return data['after']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise AIError('INVALID_CURSOR') from None

    def _page(self, rows, limit, scope, owner, view, key):
        more = len(rows) > limit
        rows = rows[:limit]
        cursor = None
        if more:
            cursor = base64.urlsafe_b64encode(json.dumps({'scope': scope, 'owner': str(owner), 'after': key(rows[-1])}, default=str).encode()).decode()
        return {'items': [view(row) for row in rows], 'next_cursor': cursor}

    def _limit(self, limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise AIError('INVALID_LIMIT')

    def list_consents(self, owner, limit=20, cursor=None):
        self._limit(limit)
        after = self._cursor(cursor, 'consents', owner)
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            sql, args = 'SELECT * FROM ai_consent_events WHERE owner_id=%s', [owner]
            if after:
                if not isinstance(after, list) or len(after) != 2:
                    raise AIError('INVALID_CURSOR')
                sql += ' AND (occurred_at,id)<(%s,%s)'
                args += after
            rows = c.execute(sql + ' ORDER BY occurred_at DESC,id DESC LIMIT %s', args + [limit + 1]).fetchall()
            return self._page(rows, limit, 'consents', owner, consent_view, lambda r: [r['occurred_at'].isoformat(), str(r['id'])])

    def create_conversation(self, owner, key, topic, tone):
        if topic not in TOPICS or tone not in ('warm', 'giga'):
            raise AIError('INVALID_CONVERSATION')
        payload = [topic, tone]
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, 'conversation', key, payload)
            if previous:
                return previous
            row = c.execute('INSERT INTO ai_conversations(id,owner_id,topic,tone) VALUES(%s,%s,%s,%s) RETURNING *', (uuid4(), owner, topic, tone)).fetchone()
            result = conversation_view(row)
            self._remember(c, owner, 'conversation', key, payload, row['id'], result)
            uow.commit()
            return result

    def _conversation(self, c, owner, cid, active=False):
        row = c.execute('SELECT * FROM ai_conversations WHERE owner_id=%s AND id=%s', (owner, cid)).fetchone()
        if not row:
            raise AIError('NOT_FOUND', 404)
        if active and row['state'] != 'active':
            raise AIError('CONVERSATION_CLOSED', 409)
        return row

    def get_conversation(self, owner, cid):
        with self._uow() as uow:
            self._owner(uow.connection, owner)
            return conversation_view(self._conversation(uow.connection, owner, cid))

    def list_conversations(self, owner, limit=20, cursor=None):
        self._limit(limit)
        after = self._cursor(cursor, 'conversations', owner)
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            sql, args = 'SELECT * FROM ai_conversations WHERE owner_id=%s', [owner]
            if after:
                if not isinstance(after, list) or len(after) != 2:
                    raise AIError('INVALID_CURSOR')
                sql += ' AND (created_at,id)<(%s,%s)'
                args += after
            rows = c.execute(sql + ' ORDER BY created_at DESC,id DESC LIMIT %s', args + [limit + 1]).fetchall()
            return self._page(rows, limit, 'conversations', owner, conversation_view, lambda r: [r['created_at'].isoformat(), str(r['id'])])

    def close_conversation(self, owner, cid, key):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'close:{cid}', key, [])
            if previous:
                return previous
            self._conversation(c, owner, cid)
            row = c.execute("UPDATE ai_conversations SET state='closed',version=version+1,updated_at=clock_timestamp() WHERE id=%s AND owner_id=%s RETURNING *", (cid, owner)).fetchone()
            for job in c.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND conversation_id=%s AND state IN ('queued','running','cancel_requested')", (owner, cid)).fetchall():
                self._cancel(c, job)
            result = conversation_view(row)
            self._remember(c, owner, f'close:{cid}', key, [], cid, result)
            uow.commit()
            return result

    def delete_conversation(self, owner, cid):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            resources = [r['id'] for r in c.execute('SELECT id FROM ai_conversation_turns WHERE conversation_id=%s', (cid,))]
            resources += [r['id'] for r in c.execute('SELECT id FROM p0_ai_jobs WHERE conversation_id=%s', (cid,))] + [cid]
            c.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND resource_id=ANY(%s)', (owner, resources))
            c.execute('DELETE FROM ai_conversations WHERE id=%s AND owner_id=%s', (cid, owner))
            uow.commit()

    def _available(self, kind):
        provider = self.transcription_provider if kind == 'transcription' else self.provider
        if provider is None or not provider.available():
            raise AIError('AI_UNAVAILABLE', 503, 'AI 연결이 아직 준비되지 않았어요. 기본 기능을 이용할 수 있어요.', True)

    def _sources(self, c, owner, ids):
        if len(ids) > self.policy['context_entries_max'] or len(set(map(str, ids))) != len(ids):
            raise AIError('INVALID_CONTEXT')
        rows = []
        for entry in ids:
            row = c.execute('SELECT id,version,body,day,mood FROM journal_entries WHERE session_id=%s AND id=%s', (owner, entry)).fetchone()
            if not row:
                raise AIError('NOT_FOUND', 404)
            rows.append(row)
        return rows

    def _reserve(self, c, owner):
        if c.execute("SELECT 1 FROM p0_ai_jobs WHERE owner_id=%s AND state IN ('queued','running','cancel_requested') LIMIT 1", (owner,)).fetchone():
            raise AIError('AI_CONCURRENT_LIMIT', 429, retryable=True)
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('ai:global-daily-reservations',))
        row = c.execute("""SELECT count(*) AS total,count(*) FILTER(WHERE owner_id=%s) AS owned FROM (
          SELECT owner_id,created_at FROM p0_ai_usage_attempts UNION ALL
          SELECT session_id AS owner_id,created_at FROM ai_usage_attempts
        ) AS attempts WHERE created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""", (owner,)).fetchone()
        if row['owned'] >= self.policy['owner_daily_limit'] or row['total'] >= self.policy['global_daily_limit']:
            raise AIError('AI_DAILY_LIMIT', 429, retryable=True)

    def _new_job(self, c, owner, kind, resource, version, consent_id, context, payload, conversation=None):
        use_ai = kind != 'chat' or payload.get('use_ai', True)
        if use_ai:
            self._available(kind)
            if consent_id is None:
                raise AIError('CONSENT_REQUIRED', 403)
            consent_id = self._require_consent(c, owner, 'cloud_transcription' if kind == 'transcription' else 'ai_processing', consent_id)
        elif context:
            raise AIError('BASIC_CONTEXT_UNSUPPORTED')
        context_consent = self._require_consent(c, owner, 'journal_context') if context else None
        sources = self._sources(c, owner, context)
        if use_ai:
            self._reserve(c, owner)
        jid = uuid4()
        payload = {**payload, 'context_entry_ids': list(map(str, context))}
        row = c.execute('''INSERT INTO p0_ai_jobs(id,owner_id,kind,resource_id,resource_version,conversation_id,consent_event_id,context_consent_id,payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (jid, owner, kind, resource, version, conversation, consent_id, context_consent, Jsonb(payload))).fetchone()
        if use_ai:
            c.execute('INSERT INTO p0_ai_usage_attempts(id,owner_id) VALUES(%s,%s)', (jid, owner))
        for source in sources:
            c.execute('INSERT INTO p0_ai_context_sources(job_id,entry_id,entry_version) VALUES(%s,%s,%s)', (jid, source['id'], source['version']))
        return row

    def create_turn(self, owner, cid, key, body):
        body = {**body, 'text': validate_turn(body.get('text', ''), body.get('answers', []))}
        if body.get('input_source') not in ('text', 'choice', 'voice'):
            raise AIError('INVALID_TURN')
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'turn:{cid}', key, body)
            if previous:
                return previous
            self._conversation(c, owner, cid, active=True)
            old = c.execute('SELECT id,fingerprint FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND client_message_id=%s', (owner, cid, body['client_message_id'])).fetchone()
            if old:
                if old['fingerprint'] != fingerprint(body):
                    raise AIError('IDEMPOTENCY_CONFLICT', 409)
                job = c.execute('SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND resource_id=%s ORDER BY created_at DESC,id DESC LIMIT 1', (owner, old['id'])).fetchone()
                return accepted(job)
            tid = uuid4()
            index = c.execute('SELECT coalesce(max(turn_index),0)+1 AS n FROM ai_conversation_turns WHERE conversation_id=%s', (cid,)).fetchone()['n']
            job = self._new_job(c, owner, 'chat', tid, 1, body.get('consent_event_id'), body.get('context_entry_ids', []), {'use_ai': body.get('use_ai', True)}, cid)
            c.execute('''INSERT INTO ai_conversation_turns(id,owner_id,conversation_id,client_message_id,fingerprint,turn_index,text,answers,input_source)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (tid, owner, cid, body['client_message_id'], fingerprint(body), index, body['text'], Jsonb(body.get('answers', [])), body['input_source']))
            c.execute('UPDATE ai_conversations SET updated_at=clock_timestamp() WHERE id=%s', (cid,))
            result = accepted(job)
            self._remember(c, owner, f'turn:{cid}', key, body, tid, result)
            uow.commit()
            return result

    def get_turn(self, owner, cid, tid):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            row = c.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND id=%s', (owner, cid, tid)).fetchone()
            if not row:
                raise AIError('NOT_FOUND', 404)
            return turn_view(row)

    def list_turns(self, owner, cid, limit=20, cursor=None):
        self._limit(limit)
        scope = f'turns:{cid}'
        after = self._cursor(cursor, scope, owner)
        if after is not None and (type(after) is not int or after < 1):
            raise AIError('INVALID_CURSOR')
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            rows = c.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND turn_index>%s ORDER BY turn_index LIMIT %s', (owner, cid, after or 0, limit + 1)).fetchall()
            return self._page(rows, limit, scope, owner, turn_view, lambda r: r['turn_index'])

    def create_interpretation(self, owner, rid, key, body):
        if self.reading_source is None:
            raise AIError('AI_UNAVAILABLE', 503)
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'interpret:{rid}', key, body)
            if previous:
                return previous
            reading = self.reading_source.snapshot(c, owner, rid)
            if not reading:
                raise AIError('NOT_FOUND', 404)
            if reading['version'] != body['expected_version'] or not reading.get('cards'):
                raise AIError('READING_VERSION_CONFLICT', 409)
            job = self._new_job(c, owner, 'tarot', rid, reading['version'], body['consent_event_id'], body.get('context_entry_ids', []), {'cards': reading['cards']})
            if hasattr(self.reading_source, 'mark_generating') and not self.reading_source.mark_generating(c, owner, rid, reading['version']):
                raise AIError('READING_VERSION_CONFLICT', 409)
            result = accepted(job)
            self._remember(c, owner, f'interpret:{rid}', key, body, rid, result)
            uow.commit()
            return result

    def _job(self, c, owner, jid):
        row = c.execute('SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND id=%s', (owner, jid)).fetchone()
        if not row:
            raise AIError('NOT_FOUND', 404)
        return row

    def get_job(self, owner, jid):
        with self._uow() as uow:
            self._owner(uow.connection, owner)
            return job_view(self._job(uow.connection, owner, jid))

    def _cancel(self, c, job):
        state = 'cancel_requested' if job['state'] == 'running' else 'cancelled'
        c.execute("UPDATE p0_ai_jobs SET state=%s,version=version+1,result_url=NULL,finished_at=CASE WHEN %s='cancelled' THEN clock_timestamp() ELSE NULL END WHERE id=%s", (state, state, job['id']))
        c.execute('DELETE FROM p0_ai_context_sources WHERE job_id=%s', (job['id'],))
        if job['kind'] == 'chat':
            c.execute("UPDATE ai_conversation_turns SET state='cancelled',assistant_result=NULL WHERE owner_id=%s AND id=%s", (job['owner_id'], job['resource_id']))
        elif job['kind'] == 'transcription':
            self._remove_audio(c, job['resource_id'])
            c.execute("UPDATE p0_ai_transcriptions SET state='deleted',transcript=NULL,version=version+1 WHERE id=%s", (job['resource_id'],))

    def cancel_job(self, owner, jid, key):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'cancel:{jid}', key, [])
            if previous:
                return previous
            job = self._job(c, owner, jid)
            if job['state'] in TERMINAL:
                raise AIError('JOB_TERMINAL', 409)
            self._cancel(c, job)
            result = job_view(self._job(c, owner, jid))
            self._remember(c, owner, f'cancel:{jid}', key, [], jid, result)
            uow.commit()
            return result

    def retry_job(self, owner, jid, key):
        with self._uow() as uow:
            c = uow.connection
            self._owner(c, owner)
            previous = self._once(c, owner, f'retry:{jid}', key, [])
            if previous:
                return previous
            job = self._job(c, owner, jid)
            if job['state'] not in ('failed', 'cancelled'):
                raise AIError('JOB_NOT_RETRYABLE', 409)
            self._resource(c, job)
            if job['kind'] == 'transcription':
                # Audio is removed on every terminal path. A fresh upload is required.
                raise AIError('TRANSCRIPTION_REUPLOAD_REQUIRED', 409)
            context = job['payload'].get('context_entry_ids', [])
            consent_id = self._require_consent(c, owner, 'ai_processing') if job['payload'].get('use_ai', True) else None
            fresh = self._new_job(c, owner, job['kind'], job['resource_id'], job['resource_version'], consent_id, context, job['payload'], job['conversation_id'])
            if job['kind'] == 'chat':
                c.execute("UPDATE ai_conversation_turns SET state='accepted',assistant_result=NULL WHERE owner_id=%s AND id=%s", (owner, job['resource_id']))
            result = accepted(fresh)
            self._remember(c, owner, f'retry:{jid}', key, [], fresh['id'], result)
            uow.commit()
            return result

    def _resource(self, c, job):
        owner, rid = job['owner_id'], job['resource_id']
        if job['kind'] == 'chat':
            self._conversation(c, owner, job['conversation_id'], active=True)
            row = c.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s AND id=%s', (owner, rid)).fetchone()
            valid = row and row['version'] == job['resource_version']
        elif job['kind'] == 'tarot':
            valid = self.reading_source and self.reading_source.validate(c, owner, rid, job['resource_version'])
        else:
            row = c.execute("SELECT * FROM p0_ai_transcriptions WHERE owner_id=%s AND id=%s AND expires_at>clock_timestamp() AND state!='deleted'", (owner, rid)).fetchone()
            valid = row and row['version'] == job['resource_version']
        if not valid:
            raise AIError('RESOURCE_CHANGED', 409)

    def _check(self, c, job):
        if self.owner_guard(c, job['owner_id']) is False:
            raise AIError('OWNER_UNAVAILABLE', 401)
        if job['kind'] != 'chat' or job['payload'].get('use_ai', True):
            self._require_consent(c, job['owner_id'], 'cloud_transcription' if job['kind'] == 'transcription' else 'ai_processing', job['consent_event_id'])
        if job['context_consent_id']:
            self._require_consent(c, job['owner_id'], 'journal_context', job['context_consent_id'])
        self._resource(c, job)
        sources = c.execute('SELECT * FROM p0_ai_context_sources WHERE job_id=%s', (job['id'],)).fetchall()
        expected = job['payload'].get('context_entry_ids', [])
        if {str(s['entry_id']) for s in sources} != set(expected):
            raise AIError('RESOURCE_CHANGED', 409)
        for source in sources:
            row = c.execute('SELECT version FROM journal_entries WHERE session_id=%s AND id=%s', (job['owner_id'], source['entry_id'])).fetchone()
            if not row or row['version'] != source['entry_version']:
                raise AIError('RESOURCE_CHANGED', 409)

    def _payload(self, c, job):
        if job['kind'] == 'transcription':
            return dict(c.execute('SELECT audio_path,mime_type FROM p0_ai_transcriptions WHERE id=%s', (job['resource_id'],)).fetchone())
        if job['kind'] == 'chat':
            conversation = self._conversation(c, job['owner_id'], job['conversation_id'], active=True)
            turn = c.execute('SELECT * FROM ai_conversation_turns WHERE id=%s', (job['resource_id'],)).fetchone()
            recent = c.execute("SELECT text,assistant_result FROM ai_conversation_turns WHERE owner_id=%s AND conversation_id=%s AND turn_index<%s AND state='ready' ORDER BY turn_index DESC LIMIT %s", (job['owner_id'], job['conversation_id'], turn['turn_index'], self.policy['history_turns'])).fetchall()
            history, budget = [], self.policy['history_chars']
            for previous in recent:
                item = {'text': previous['text'], 'assistant_result': previous['assistant_result']}
                cost = len(json.dumps(item, ensure_ascii=False))
                if cost > budget:
                    break
                history.insert(0, item)
                budget -= cost
            payload = {'kind': 'chat', 'topic': conversation['topic'], 'tone': conversation['tone'], 'text': turn['text'], 'answers': turn['answers'], 'cards': [], 'history': history}
        else:
            reading = self.reading_source.snapshot(c, job['owner_id'], job['resource_id'])
            payload = {'kind': 'tarot', 'topic': reading['topic'], 'tone': reading.get('tone', 'warm'), 'text': reading.get('question', ''), 'answers': [], 'cards': job['payload']['cards'], 'history': []}
        sources = self._sources(c, job['owner_id'], job['payload'].get('context_entry_ids', []))
        remaining, journals = self.policy['context_chars'], []
        for source in sources:
            body = source['body'][:remaining]
            journals.append({'id': str(source['id']), 'version': source['version'], 'day': str(source['day']), 'mood': source['mood'], 'body': body})
            remaining -= len(body)
        payload['journals'] = journals
        return payload

    def _finish(self, c, job, state, code=None, result=None):
        url = None
        if state == 'succeeded':
            if job['kind'] == 'chat':
                c.execute("UPDATE ai_conversation_turns SET state='ready',assistant_result=%s,version=version+1 WHERE id=%s AND owner_id=%s", (Jsonb(result), job['resource_id'], job['owner_id']))
                url = f"/v2/conversations/{job['conversation_id']}/turns/{job['resource_id']}"
            elif job['kind'] == 'tarot':
                if not self.reading_source.complete(c, job['owner_id'], job['resource_id'], job['resource_version'], result):
                    raise AIError('RESOURCE_CHANGED', 409)
                url = f"/v2/readings/{job['resource_id']}"
            else:
                c.execute("UPDATE p0_ai_transcriptions SET state='ready',transcript=%s WHERE id=%s", (result, job['resource_id']))
                url = f"/v2/transcriptions/{job['resource_id']}"
        elif job['kind'] == 'chat':
            c.execute('UPDATE ai_conversation_turns SET state=%s,assistant_result=NULL WHERE owner_id=%s AND id=%s', ('cancelled' if state == 'cancelled' else 'failed', job['owner_id'], job['resource_id']))
        elif job['kind'] == 'transcription':
            c.execute("UPDATE p0_ai_transcriptions SET state='failed',transcript=NULL WHERE id=%s AND state!='deleted'", (job['resource_id'],))
        elif job['kind'] == 'tarot' and hasattr(self.reading_source, 'mark_failed'):
            self.reading_source.mark_failed(c, job['owner_id'], job['resource_id'], job['resource_version'])
        if job['kind'] == 'transcription':
            self._remove_audio(c, job['resource_id'])
        c.execute('UPDATE p0_ai_jobs SET state=%s,error_code=%s,result_url=%s,finished_at=clock_timestamp(),version=version+1,lease_until=NULL WHERE id=%s', (state, code, url, job['id']))
        # Only source IDs/versions remain for explanation; no journal text is persisted here.

    def run_next(self, worker_id='worker'):
        lease = uuid4()
        with self._uow() as uow:
            c = uow.connection
            row = c.execute("SELECT * FROM p0_ai_jobs WHERE state='queued' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not row:
                return False
            c.execute("UPDATE p0_ai_jobs SET state='running',lease_id=%s,lease_until=clock_timestamp()+(%s * interval '1 second'),version=version+1 WHERE id=%s", (lease, self.policy['lease_seconds'], row['id']))
            uow.commit()
        with self._uow() as uow:
            c = uow.connection
            owner_lock(c, row['owner_id'])
            job = self._job(c, row['owner_id'], row['id'])
            if job['lease_id'] != lease or job['state'] != 'running':
                return True
            try:
                self._check(c, job)
                payload = self._payload(c, job)
                if job['kind'] != 'chat' or job['payload'].get('use_ai', True):
                    self._available(job['kind'])
            except AIError as error:
                self._finish(c, job, 'cancelled' if error.status in (401, 403, 404, 409) else 'failed', error.code)
                uow.commit()
                return True
            c.execute('UPDATE p0_ai_jobs SET provider_started=%s WHERE id=%s AND lease_id=%s', (job['kind'] != 'chat' or job['payload'].get('use_ai', True), job['id'], lease))
            if job['kind'] == 'chat':
                c.execute("UPDATE ai_conversation_turns SET state='generating' WHERE id=%s", (job['resource_id'],))
            elif job['kind'] == 'transcription':
                c.execute("UPDATE p0_ai_transcriptions SET state='running' WHERE id=%s", (job['resource_id'],))
            uow.commit()
        # No open unit of work, connection, transaction or owner lock across provider I/O.
        started, output, error = time.monotonic(), None, None
        try:
            if job['kind'] == 'transcription':
                output = self.transcription_provider.transcribe(payload['audio_path'], payload['mime_type'])
                if not isinstance(output, str) or not output.strip() or len(output) > 2000:
                    raise AIError('AI_OUTPUT_INVALID', retryable=True)
            else:
                if re.search(r'자살|죽고\s*싶|나를\s*해치|자해', payload['text']):
                    output = safety_result(job['kind'], payload['cards'])
                elif job['kind'] == 'chat' and not job['payload'].get('use_ai', True):
                    output = {'kind': 'chat', 'empathy': '마음을 나눠 주셔서 고마워요.',
                              'summary': '지금 내 마음에 필요한 것을 천천히 살펴봐요.', 'card_insights': [],
                              'advice': '바꿀 수 있는 것과 도움을 받을 수 있는 것을 하나씩 적어 보세요.',
                              'actions': ['지금 할 수 있는 작은 행동 하나를 정해 보세요.'],
                              'expression': 'encourage', 'follow_up_questions': ['어떤 도움이 있으면 좋을까요?'],
                              'safety_mode': 'standard'}
                else:
                    output = self.provider.generate(payload)
                output = validate_result(output, job['kind'], payload['cards'])
        except AIError as failure:
            error = failure.code
        except Exception:
            error = 'UNKNOWN_PROVIDER_OUTCOME'
        with self._uow() as uow:
            c = uow.connection
            owner_lock(c, row['owner_id'])
            current = c.execute('SELECT * FROM p0_ai_jobs WHERE id=%s AND owner_id=%s FOR UPDATE', (row['id'], row['owner_id'])).fetchone()
            if not current or current['lease_id'] != lease or current['state'] in TERMINAL:
                return True
            try:
                if current['state'] != 'running':
                    raise AIError('CANCELLED', 409)
                self._check(c, current)
            except AIError as failure:
                self._finish(c, current, 'cancelled', failure.code)
            else:
                self._finish(c, current, 'failed' if error else 'succeeded', error, output)
                provider = self.transcription_provider if current['kind'] == 'transcription' else self.provider
                c.execute('INSERT INTO p0_ai_runs(id,job_id,model_id,prompt_version,schema_version,elapsed_ms) VALUES(%s,%s,%s,%s,%s,%s)', (uuid4(), current['id'], getattr(provider, 'model', None), self.policy['prompt_version'], self.policy['schema_version'], int((time.monotonic() - started) * 1000)))
            uow.commit()
        return True

    def heartbeat(self, jid, lease):
        with self._uow() as uow:
            row = uow.connection.execute("UPDATE p0_ai_jobs SET lease_until=clock_timestamp()+(%s * interval '1 second') WHERE id=%s AND lease_id=%s AND state='running' RETURNING id", (self.policy['lease_seconds'], jid, lease)).fetchone()
            uow.commit()
            return row is not None

    def recover(self):
        with self._uow() as uow:
            rows = uow.connection.execute("SELECT id,owner_id FROM p0_ai_jobs WHERE state IN ('running','cancel_requested') AND lease_until<clock_timestamp()").fetchall()
        count = 0
        for row in rows:
            with self._uow() as uow:
                c = uow.connection
                owner_lock(c, row['owner_id'])
                job = c.execute("SELECT * FROM p0_ai_jobs WHERE id=%s AND state IN ('running','cancel_requested') AND lease_until<clock_timestamp() FOR UPDATE", (row['id'],)).fetchone()
                if not job:
                    continue
                if job['state'] == 'cancel_requested':
                    self._finish(c, job, 'cancelled', 'CANCELLED')
                elif job['provider_started']:
                    self._finish(c, job, 'failed', 'UNKNOWN_PROVIDER_OUTCOME')
                else:
                    c.execute("UPDATE p0_ai_jobs SET state='queued',lease_id=NULL,lease_until=NULL,version=version+1 WHERE id=%s", (job['id'],))
                uow.commit()
                count += 1
        return count

    def cancel_for_resource(self, connection, owner, resource_id):
        owner_lock(connection, owner)
        for job in connection.execute("SELECT * FROM p0_ai_jobs WHERE owner_id=%s AND resource_id=%s AND state IN ('queued','running','cancel_requested')", (owner, resource_id)).fetchall():
            self._cancel(connection, job)

    def cancel_for_journal(self, connection, owner, entry_id):
        owner_lock(connection, owner)
        for job in connection.execute("SELECT j.* FROM p0_ai_jobs j JOIN p0_ai_context_sources s ON s.job_id=j.id WHERE j.owner_id=%s AND s.entry_id=%s AND j.state IN ('queued','running','cancel_requested')", (owner, entry_id)).fetchall():
            self._cancel(connection, job)

    def _remove_audio(self, c, rid):
        row = c.execute('SELECT audio_path FROM p0_ai_transcriptions WHERE id=%s', (rid,)).fetchone()
        if row and row['audio_path'] and self.audio_store:
            self.audio_store.delete(row['audio_path'])
        c.execute('UPDATE p0_ai_transcriptions SET audio_path=NULL WHERE id=%s', (rid,))

    def erase_owner(self, owner, connection=None):
        # Accept both callback(connection, owner) and erase_owner(owner, connection=...).
        if hasattr(owner, 'execute'):
            owner, connection = connection, owner
        if connection is None:
            with self._uow() as uow:
                self.erase_owner(owner, connection=uow.connection)
                uow.commit()
            return
        owner_lock(connection, owner)
        for row in connection.execute('SELECT id FROM p0_ai_transcriptions WHERE owner_id=%s', (owner,)).fetchall():
            self._remove_audio(connection, row['id'])
        connection.execute('DELETE FROM p0_ai_jobs WHERE owner_id=%s', (owner,))
        connection.execute('DELETE FROM ai_conversations WHERE owner_id=%s', (owner,))
        connection.execute('DELETE FROM ai_consent_events WHERE owner_id=%s', (owner,))
        connection.execute('DELETE FROM p0_ai_transcriptions WHERE owner_id=%s', (owner,))
        connection.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s', (owner,))
        connection.execute('UPDATE p0_ai_usage_attempts SET owner_id=NULL WHERE owner_id=%s', (owner,))

    def export_owner(self, connection, owner):
        return {'consents': [consent_view(r) for r in connection.execute('SELECT * FROM ai_consent_events WHERE owner_id=%s ORDER BY occurred_at,id', (owner,))],
                'conversations': [conversation_view(r) for r in connection.execute('SELECT * FROM ai_conversations WHERE owner_id=%s ORDER BY created_at,id', (owner,))],
                'turns': [turn_view(r) for r in connection.execute('SELECT * FROM ai_conversation_turns WHERE owner_id=%s ORDER BY conversation_id,turn_index', (owner,))]}
