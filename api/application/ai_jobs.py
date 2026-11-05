"""Durable P0 jobs, explicit consent and owner-scoped conversation operations."""
import base64
import hashlib
import json
import re
import time
from datetime import datetime
from uuid import UUID, uuid4

from api.application.ai_jobs_ports import AIJobsRepository
from api.domain.ai_jobs import (AIError, fingerprint, load_policy, safety_result,
    validate_context_entry_versions, validate_result, validate_turn)


TERMINAL = ('succeeded', 'failed', 'cancelled')
TOPICS = ('relationship', 'work', 'self', 'daily', 'care', 'start')


class AIJobsService:
    def __init__(self, uow_factory, provider=None, transcription_provider=None, reading_source=None,
                 owner_guard=None, audio_store=None, policy=None):
        self._uow = uow_factory
        self.provider, self.transcription_provider = provider, transcription_provider
        self.reading_source = reading_source
        self.owner_guard = owner_guard
        self.audio_store = audio_store
        self.policy = {**load_policy(), **(policy or {})}

    def _repository(self, transaction) -> AIJobsRepository:
        if hasattr(transaction, 'lock_owner') and hasattr(transaction, 'transaction'):
            return transaction
        return self._uow().repository_for(transaction)

    def _owner(self, repository, owner):
        repository.lock_owner(owner)
        if not repository.owner_available(owner, self.owner_guard):
            raise AIError('OWNER_UNAVAILABLE', 401)

    def _once(self, repository, owner, scope, key, payload):
        row = repository.idempotency(owner, scope, key)
        if row:
            if row['fingerprint'] != fingerprint(payload):
                raise AIError('IDEMPOTENCY_CONFLICT', 409)
            return row['response']

    def _remember(self, repository, owner, scope, key, payload, resource, response):
        repository.remember(owner, scope, key, fingerprint(payload), resource, response)

    def _require_consent(self, repository, owner, kind, event_id=None):
        row = repository.latest_consent(owner, kind)
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
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, 'consent', key, payload)
            if previous:
                return previous
            row = c.add_consent(uuid4(), owner, kind, policy_version, granted)
            # A new consent event supersedes the captured event of pending jobs as well.
            jobs = c.pending_for_consent(owner, kind)
            for job in jobs:
                self._cancel(c, job)
            result = c.consent_view(row)
            self._remember(c, owner, 'consent', key, payload, row['id'], result)
            uow.commit()
            return result

    def _cursor(self, value, scope, owner):
        if not value:
            return None
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
            if data['scope'] != scope or data['owner'] != str(owner):
                raise ValueError()
            after = data['after']
            if scope in ('consents', 'conversations'):
                if not isinstance(after, list) or len(after) != 2:
                    raise ValueError()
                stamp = datetime.fromisoformat(after[0])
                if stamp.tzinfo is None:
                    raise ValueError()
                return [stamp, UUID(after[1])]
            return after
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
            c = uow.ai_jobs
            self._owner(c, owner)
            rows = c.consents(owner, after, limit + 1)
            return self._page(rows, limit, 'consents', owner, c.consent_view, c.consent_cursor)

    def create_conversation(self, owner, key, topic, tone):
        if topic not in TOPICS or tone not in ('warm', 'giga'):
            raise AIError('INVALID_CONVERSATION')
        payload = [topic, tone]
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, 'conversation', key, payload)
            if previous:
                return previous
            row = c.add_conversation(uuid4(), owner, topic, tone)
            result = c.conversation_view(row)
            self._remember(c, owner, 'conversation', key, payload, row['id'], result)
            uow.commit()
            return result

    def _conversation(self, c, owner, cid, active=False):
        row = c.conversation(owner, cid)
        if not row:
            raise AIError('NOT_FOUND', 404)
        if active and row['state'] != 'active':
            raise AIError('CONVERSATION_CLOSED', 409)
        return row

    def get_conversation(self, owner, cid):
        with self._uow() as uow:
            self._owner(uow.ai_jobs, owner)
            return uow.ai_jobs.conversation_view(self._conversation(uow.ai_jobs, owner, cid))

    def list_conversations(self, owner, limit=20, cursor=None):
        self._limit(limit)
        after = self._cursor(cursor, 'conversations', owner)
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            rows = c.conversations(owner, after, limit + 1)
            return self._page(rows, limit, 'conversations', owner, c.conversation_view, c.conversation_cursor)

    def close_conversation(self, owner, cid, key):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, f'close:{cid}', key, [])
            if previous:
                return previous
            self._conversation(c, owner, cid)
            row = c.close_conversation(owner, cid)
            for job in c.pending_for_conversation(owner, cid):
                self._cancel(c, job)
            result = c.conversation_view(row)
            self._remember(c, owner, f'close:{cid}', key, [], cid, result)
            uow.commit()
            return result

    def delete_conversation(self, owner, cid):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            c.delete_conversation(owner, cid)
            uow.commit()

    def _available(self, kind):
        provider = self.transcription_provider if kind == 'transcription' else self.provider
        if provider is None or not provider.available():
            raise AIError('AI_UNAVAILABLE', 503, 'AI 연결이 아직 준비되지 않았어요. 기본 기능을 이용할 수 있어요.', True)

    def _sources(self, c, owner, ids, versions=None):
        if len(ids) > self.policy['context_entries_max'] or len(set(map(str, ids))) != len(ids):
            raise AIError('INVALID_CONTEXT')
        expected_versions = validate_context_entry_versions(ids, versions)
        rows = []
        for entry in ids:
            row = c.journal(owner, entry)
            if not row:
                raise AIError('NOT_FOUND', 404)
            if expected_versions is not None and expected_versions[str(row['id'])] != row['version']:
                raise AIError('RESOURCE_CHANGED', 409,
                              '선택한 기록이 바뀌었어요. 내용을 확인한 뒤 다시 선택해 주세요.')
            rows.append(row)
        return rows

    def _reserve(self, c, owner):
        if c.active_job_exists(owner):
            raise AIError('AI_CONCURRENT_LIMIT', 429, retryable=True)
        c.lock_usage()
        row = c.usage(owner)
        if row['owned'] >= self.policy['owner_daily_limit'] or row['total'] >= self.policy['global_daily_limit']:
            raise AIError('AI_DAILY_LIMIT', 429, retryable=True)

    def _new_job(self, c, owner, kind, resource, version, consent_id, context, payload, conversation=None,
                 *, context_entry_versions=None):
        # This additive client precondition precedes availability and quota checks.
        # Omitting it retains the existing admission order and request contract.
        sources = self._sources(c, owner, context, context_entry_versions) if context_entry_versions is not None else None
        use_ai = kind != 'chat' or payload.get('use_ai', True)
        if use_ai:
            self._available(kind)
            if consent_id is None:
                raise AIError('CONSENT_REQUIRED', 403)
            consent_id = self._require_consent(c, owner, 'cloud_transcription' if kind == 'transcription' else 'ai_processing', consent_id)
        elif context:
            raise AIError('BASIC_CONTEXT_UNSUPPORTED')
        context_consent = self._require_consent(c, owner, 'journal_context') if context else None
        if sources is None:
            sources = self._sources(c, owner, context)
        if use_ai:
            self._reserve(c, owner)
        jid = uuid4()
        payload = {**payload, 'context_entry_ids': list(map(str, context)),
                   'context_versions': {str(source['id']): source['version'] for source in sources}}
        row = c.add_job({'id': jid, 'owner_id': owner, 'kind': kind, 'resource_id': resource,
                         'resource_version': version, 'conversation_id': conversation,
                         'consent_event_id': consent_id, 'context_consent_id': context_consent, 'payload': payload})
        if use_ai:
            c.reserve_usage(jid, owner)
        for source in sources:
            c.add_source(jid, source['id'], source['version'])
        return row

    def create_turn(self, owner, cid, key, body):
        body = {**body, 'text': validate_turn(body.get('text', ''), body.get('answers', []))}
        if body.get('input_source') not in ('text', 'choice', 'voice'):
            raise AIError('INVALID_TURN')
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, f'turn:{cid}', key, body)
            if previous:
                return previous
            self._conversation(c, owner, cid, active=True)
            old = c.duplicate_turn(owner, cid, body['client_message_id'])
            if old:
                if old['fingerprint'] != fingerprint(body):
                    raise AIError('IDEMPOTENCY_CONFLICT', 409)
                job = c.latest_job(owner, old['id'])
                return c.accepted(job)
            tid = uuid4()
            index = c.next_turn_index(cid)
            job = self._new_job(c, owner, 'chat', tid, 1, body.get('consent_event_id'), body.get('context_entry_ids', []),
                                {'use_ai': body.get('use_ai', True)}, cid,
                                context_entry_versions=body.get('context_entry_versions'))
            c.add_turn({'id': tid, 'owner_id': owner, 'conversation_id': cid,
                        'client_message_id': body['client_message_id'], 'fingerprint': fingerprint(body),
                        'turn_index': index, 'text': body['text'], 'answers': body.get('answers', []),
                        'input_source': body['input_source']})
            result = c.accepted(job)
            self._remember(c, owner, f'turn:{cid}', key, body, tid, result)
            uow.commit()
            return result

    def get_turn(self, owner, cid, tid):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            row = c.turn(owner, tid, cid)
            if not row:
                raise AIError('NOT_FOUND', 404)
            return c.turn_view(row)

    def list_turns(self, owner, cid, limit=20, cursor=None):
        self._limit(limit)
        scope = f'turns:{cid}'
        after = self._cursor(cursor, scope, owner)
        if after is not None and (type(after) is not int or after < 1):
            raise AIError('INVALID_CURSOR')
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            self._conversation(c, owner, cid)
            rows = c.turns(owner, cid, after or 0, limit + 1)
            return self._page(rows, limit, scope, owner, c.turn_view, lambda r: r['turn_index'])

    def create_interpretation(self, owner, rid, key, body):
        if self.reading_source is None:
            raise AIError('AI_UNAVAILABLE', 503)
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, f'interpret:{rid}', key, body)
            if previous:
                return previous
            reading = self.reading_source.snapshot(c.transaction, owner, rid)
            if not reading:
                raise AIError('NOT_FOUND', 404)
            if reading['version'] != body['expected_version'] or not reading.get('cards'):
                raise AIError('READING_VERSION_CONFLICT', 409)
            job = self._new_job(c, owner, 'tarot', rid, reading['version'], body['consent_event_id'], body.get('context_entry_ids', []),
                                {'cards': reading['cards']}, context_entry_versions=body.get('context_entry_versions'))
            if hasattr(self.reading_source, 'mark_generating') and not self.reading_source.mark_generating(c.transaction, owner, rid, reading['version']):
                raise AIError('READING_VERSION_CONFLICT', 409)
            result = c.accepted(job)
            self._remember(c, owner, f'interpret:{rid}', key, body, rid, result)
            uow.commit()
            return result

    def _job(self, c, owner, jid):
        row = c.job(owner, jid)
        if not row:
            raise AIError('NOT_FOUND', 404)
        return row

    def get_job(self, owner, jid):
        with self._uow() as uow:
            self._owner(uow.ai_jobs, owner)
            return uow.ai_jobs.job_view(self._job(uow.ai_jobs, owner, jid))

    def _cancel(self, c, job):
        state = 'cancel_requested' if job['state'] == 'running' else 'cancelled'
        c.cancel_job(job['id'], state)
        if job['kind'] == 'chat':
            c.set_turn_state(job['owner_id'], job['resource_id'], 'cancelled')
        elif job['kind'] == 'transcription':
            self._remove_audio(c, job['resource_id'])
            c.set_transcription_state(job['resource_id'], 'deleted', advance_version=True)
        elif job['kind'] == 'tarot' and hasattr(self.reading_source, 'mark_failed'):
            self.reading_source.mark_failed(c.transaction, job['owner_id'], job['resource_id'], job['resource_version'])

    def cancel_job(self, owner, jid, key):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, f'cancel:{jid}', key, [])
            if previous:
                return previous
            job = self._job(c, owner, jid)
            if job['state'] in TERMINAL:
                raise AIError('JOB_TERMINAL', 409)
            self._cancel(c, job)
            result = c.job_view(self._job(c, owner, jid))
            self._remember(c, owner, f'cancel:{jid}', key, [], jid, result)
            uow.commit()
            return result

    def retry_job(self, owner, jid, key):
        with self._uow() as uow:
            c = uow.ai_jobs
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
            if context:
                prior_versions = job['payload'].get('context_versions', {})
                for source in self._sources(c, owner, context):
                    if prior_versions.get(str(source['id'])) != source['version']:
                        raise AIError('CONTEXT_CHANGED', 409, '선택한 기록이 바뀌었어요. 내용을 확인한 뒤 새 요청을 보내 주세요.')
            consent_id = self._require_consent(c, owner, 'ai_processing') if job['payload'].get('use_ai', True) else None
            fresh = self._new_job(c, owner, job['kind'], job['resource_id'], job['resource_version'], consent_id, context, job['payload'], job['conversation_id'])
            if job['kind'] == 'tarot' and hasattr(self.reading_source, 'mark_generating'):
                if not self.reading_source.mark_generating(c.transaction, owner, job['resource_id'], job['resource_version']):
                    raise AIError('RESOURCE_CHANGED', 409)
            if job['kind'] == 'chat':
                c.set_turn_state(owner, job['resource_id'], 'accepted')
            result = c.accepted(fresh)
            self._remember(c, owner, f'retry:{jid}', key, [], fresh['id'], result)
            uow.commit()
            return result

    def _resource(self, c, job):
        owner, rid = job['owner_id'], job['resource_id']
        if job['kind'] == 'chat':
            self._conversation(c, owner, job['conversation_id'], active=True)
            row = c.turn(owner, rid)
            valid = row and row['version'] == job['resource_version']
        elif job['kind'] == 'tarot':
            valid = self.reading_source and self.reading_source.validate(c.transaction, owner, rid, job['resource_version'])
        else:
            row = c.transcription(owner, rid, live_only=True)
            valid = row and row['version'] == job['resource_version']
        if not valid:
            raise AIError('RESOURCE_CHANGED', 409)

    def _check(self, c, job):
        if not c.owner_available(job['owner_id'], self.owner_guard):
            raise AIError('OWNER_UNAVAILABLE', 401)
        if job['kind'] != 'chat' or job['payload'].get('use_ai', True):
            self._require_consent(c, job['owner_id'], 'cloud_transcription' if job['kind'] == 'transcription' else 'ai_processing', job['consent_event_id'])
        if job['context_consent_id']:
            self._require_consent(c, job['owner_id'], 'journal_context', job['context_consent_id'])
        self._resource(c, job)
        sources = c.context_sources(job['id'])
        expected = job['payload'].get('context_entry_ids', [])
        if {str(s['entry_id']) for s in sources} != set(expected):
            raise AIError('RESOURCE_CHANGED', 409)
        for source in sources:
            version = c.journal_version(job['owner_id'], source['entry_id'])
            if version != source['entry_version']:
                raise AIError('RESOURCE_CHANGED', 409)

    def _payload(self, c, job):
        if job['kind'] == 'transcription':
            return c.transcription(job['owner_id'], job['resource_id'])
        if job['kind'] == 'chat':
            conversation = self._conversation(c, job['owner_id'], job['conversation_id'], active=True)
            turn = c.turn(job['owner_id'], job['resource_id'])
            recent = c.history(job['owner_id'], job['conversation_id'], turn['turn_index'], self.policy['history_turns'])
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
            reading = self.reading_source.snapshot(c.transaction, job['owner_id'], job['resource_id'])
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
                c.set_turn_state(job['owner_id'], job['resource_id'], 'ready', result, advance_version=True)
                url = f"/v2/conversations/{job['conversation_id']}/turns/{job['resource_id']}"
            elif job['kind'] == 'tarot':
                if not self.reading_source.complete(c.transaction, job['owner_id'], job['resource_id'], job['resource_version'], result):
                    raise AIError('RESOURCE_CHANGED', 409)
                url = f"/v2/readings/{job['resource_id']}"
            else:
                c.set_transcription_state(job['resource_id'], 'ready', result)
                url = f"/v2/transcriptions/{job['resource_id']}"
        elif job['kind'] == 'chat':
            c.set_turn_state(job['owner_id'], job['resource_id'], 'cancelled' if state == 'cancelled' else 'failed')
        elif job['kind'] == 'transcription':
            c.set_transcription_state(job['resource_id'], 'failed', keep_deleted=True)
        elif job['kind'] == 'tarot' and hasattr(self.reading_source, 'mark_failed'):
            self.reading_source.mark_failed(c.transaction, job['owner_id'], job['resource_id'], job['resource_version'])
        if job['kind'] == 'transcription':
            self._remove_audio(c, job['resource_id'])
        c.finish_job(job['id'], state, code, url)
        # Only source IDs/versions remain for explanation; no journal text is persisted here.

    def run_next(self, worker_id='worker'):
        lease = uuid4()
        with self._uow() as uow:
            c = uow.ai_jobs
            row = c.claim_next(lease, self.policy['lease_seconds'])
            if not row:
                return False
            uow.commit()
        with self._uow() as uow:
            c = uow.ai_jobs
            c.lock_owner(row['owner_id'])
            job = c.job(row['owner_id'], row['id'])
            if not job:
                return True
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
            c.mark_provider_started(job['id'], lease, job['kind'] != 'chat' or job['payload'].get('use_ai', True))
            if job['kind'] == 'chat':
                c.set_turn_state(job['owner_id'], job['resource_id'], 'generating')
            elif job['kind'] == 'transcription':
                c.set_transcription_state(job['resource_id'], 'running')
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
            c = uow.ai_jobs
            c.lock_owner(row['owner_id'])
            current = c.job(row['owner_id'], row['id'], for_update=True)
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
                diagnostics = provider.diagnostics() if provider and hasattr(provider, 'diagnostics') else {}
                def diagnostic_id(name):
                    value = diagnostics.get(name)
                    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:\-]{1,200}', value) else None
                def diagnostic_tokens(name):
                    value = diagnostics.get(name)
                    return value if type(value) is int and 0 <= value < 2 ** 31 else None
                c.add_run({'id': uuid4(), 'job_id': current['id'], 'model_id': getattr(provider, 'model', None),
                           'prompt_version': self.policy['prompt_version'], 'schema_version': self.policy['schema_version'],
                           'elapsed_ms': int((time.monotonic() - started) * 1000), 'request_id': diagnostic_id('request_id'),
                           'response_id': diagnostic_id('response_id'), 'input_tokens': diagnostic_tokens('input_tokens'),
                           'output_tokens': diagnostic_tokens('output_tokens')})
            uow.commit()
        return True

    def heartbeat(self, jid, lease):
        with self._uow() as uow:
            renewed = uow.ai_jobs.heartbeat(jid, lease, self.policy['lease_seconds'])
            uow.commit()
            return renewed

    def recover(self):
        with self._uow() as uow:
            rows = uow.ai_jobs.recovery_candidates()
        count = 0
        for row in rows:
            with self._uow() as uow:
                c = uow.ai_jobs
                c.lock_owner(row['owner_id'])
                job = c.expired_job(row['id'])
                if not job:
                    continue
                if job['state'] == 'cancel_requested':
                    self._finish(c, job, 'cancelled', 'CANCELLED')
                elif job['provider_started']:
                    self._finish(c, job, 'failed', 'UNKNOWN_PROVIDER_OUTCOME')
                else:
                    c.requeue(job['id'])
                uow.commit()
                count += 1
        return count

    def cancel_for_resource(self, connection, owner, resource_id):
        repository = self._repository(connection)
        repository.lock_owner(owner)
        for job in repository.pending_for_resource(owner, resource_id):
            self._cancel(repository, job)

    def cancel_for_journal(self, connection, owner, entry_id):
        repository = self._repository(connection)
        repository.lock_owner(owner)
        for job in repository.pending_for_journal(owner, entry_id):
            self._cancel(repository, job)

    def _remove_audio(self, c, rid):
        path = c.audio_path(rid)
        if path and self.audio_store:
            self.audio_store.delete(path)
        c.detach_audio(rid)

    def create_transcription(self, owner, key, audio, mime_type, duration_ms, consent_event_id):
        if self.audio_store is None:
            raise AIError('AI_UNAVAILABLE', 503)
        payload = [hashlib.sha256(audio).hexdigest(), mime_type, duration_ms, str(consent_event_id)]
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            previous = self._once(c, owner, 'transcription', key, payload)
            if previous:
                return previous
            self._require_consent(c, owner, 'cloud_transcription', consent_event_id)
            self._available('transcription')
        # Bounded media decoding also holds no DB transaction.
        mime, actual_duration = self.audio_store.validate(audio, mime_type, duration_ms, self.policy['audio_max_bytes'], self.policy['audio_max_duration_ms'])
        path = None
        committed = False
        try:
            with self._uow() as uow:
                c = uow.ai_jobs
                self._owner(c, owner)
                previous = self._once(c, owner, 'transcription', key, payload)
                if previous:
                    return previous
                rid = uuid4()
                job = self._new_job(c, owner, 'transcription', rid, 1, consent_event_id, [], {})
                path = self.audio_store.save(audio, mime)
                c.add_transcription(rid, owner, path, mime, actual_duration, self.policy['temporary_seconds'])
                result = c.accepted(job)
                self._remember(c, owner, 'transcription', key, payload, rid, result)
                uow.commit()
                committed = True
                return result
        finally:
            if path and not committed:
                self.audio_store.delete(path)

    def get_transcription(self, owner, rid):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            row = c.transcription(owner, rid)
            if not row:
                raise AIError('NOT_FOUND', 404)
            if row['expired']:
                raise AIError('TRANSCRIPTION_EXPIRED', 410)
            if row['state'] == 'deleted':
                raise AIError('NOT_FOUND', 404)
            return c.transcription_view(row)

    def delete_transcription(self, owner, rid):
        with self._uow() as uow:
            c = uow.ai_jobs
            self._owner(c, owner)
            row = c.transcription(owner, rid)
            if not row:
                raise AIError('NOT_FOUND', 404)
            self.cancel_for_resource(c, owner, rid)
            self._remove_audio(c, rid)
            c.set_transcription_state(rid, 'deleted', advance_version=True)
            c.forget_resource_commands(owner, rid)
            uow.commit()

    def purge_expired(self):
        with self._uow() as uow:
            rows = uow.ai_jobs.expired_transcriptions()
        for row in rows:
            with self._uow() as uow:
                c = uow.ai_jobs
                c.lock_owner(row['owner_id'])
                self.cancel_for_resource(c, row['owner_id'], row['id'])
                self._remove_audio(c, row['id'])
                c.set_transcription_state(row['id'], 'deleted', advance_version=True)
                uow.commit()
        if self.audio_store:
            self.audio_store.purge(self.policy['temporary_seconds'])
        return len(rows)

    def erase_owner(self, owner, connection=None):
        # Accept both callback(connection, owner) and erase_owner(owner, connection=...).
        if connection is not None and not isinstance(owner, (UUID, str)):
            owner, connection = connection, owner
        if connection is None:
            with self._uow() as uow:
                self.erase_owner(owner, connection=uow.ai_jobs)
                uow.commit()
            return
        repository = self._repository(connection)
        repository.lock_owner(owner)
        for row in repository.transcriptions(owner):
            self._remove_audio(repository, row['id'])
        repository.erase_owner(owner)

    def export_owner(self, connection, owner):
        return self._repository(connection).export_owner(owner)

    def turn_exists(self, connection, owner, turn_id):
        return self._repository(connection).turn_exists(owner, turn_id)

    def legacy_guard(self):
        """Opt-in production v1 checks; default legacy service behavior remains unchanged."""
        service = self
        class LegacyGuard:
            def capture(self, connection, owner):
                from api.application.experience import GuidanceUnavailable, UsageLimitReached
                try:
                    repository = service._repository(connection)
                    service._owner(repository, owner)
                    stamp = service._require_consent(repository, owner, 'ai_processing')
                    # Legacy reservation follows in the same transaction and takes this same lock.
                    service._reserve(repository, owner)
                    return stamp
                except AIError as error:
                    if error.status == 429:
                        raise UsageLimitReached('오늘 AI 요청 한도나 진행 중인 작업을 확인해 주세요.') from None
                    raise GuidanceUnavailable('현재 AI 처리 동의를 확인해 주세요.') from None

            def validate(self, connection, owner, stamp):
                from api.application.experience import GuidanceUnavailable
                try:
                    repository = service._repository(connection)
                    service._owner(repository, owner)
                    service._require_consent(repository, owner, 'ai_processing', stamp)
                except AIError:
                    raise GuidanceUnavailable('동의나 계정 상태가 바뀌어 늦은 답변을 저장하지 않았어요.') from None
        return LegacyGuard()
