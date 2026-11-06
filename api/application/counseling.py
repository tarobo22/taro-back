"""Confirm understanding before drawing cards; never hold a DB transaction over I/O."""
from datetime import datetime, timezone
import time
from uuid import uuid4

from api.application.counseling_ports import CounselingModel, CounselingStore
from api.domain.ai_jobs import fingerprint, load_policy, safety_result
from api.domain.counseling import (Analysis, CounselingError, draw_cards, grounded_result,
    personal_definitions, retrieve_definitions, search_queries, urgent_risk)


def public_definition(row):
    return dict(id=str(row['id']), term=row['term'], definition=row['definition'], version=row['version'])


def memory_revision(rows):
    return fingerprint(sorted((str(row['id']), row['version']) for row in rows))


def bounded_history(history):
    selected, remaining = [], 6000
    for item in reversed(history[-10:]):
        if len(item['text']) > remaining:
            break
        selected.append(item)
        remaining -= len(item['text'])
    return list(reversed(selected))


def conversation_definitions(state):
    return [dict(id=f'session:{index}', version=1, **item)
            for index, item in enumerate(state.get('session_definitions', []))]


class CounselingService:
    def __init__(self, store: CounselingStore, model: CounselingModel, cards, policy=None, sample=None):
        self.store, self.model, self.cards = store, model, cards
        self.policy, self.sample = {**load_policy(), **(policy or {})}, sample

    def _session(self, repo, owner, session_id):
        row = repo.session(owner, session_id)
        if row is None:
            raise CounselingError('NOT_FOUND', 404, '이야기를 찾지 못했어요.')
        return row

    def _view(self, repo, row):
        state = row['state']
        analysis = state.get('analysis') or {}
        job = repo.latest_command(row['owner_id'], row['id'])
        view = {key: state.get(key, default) for key, default in {
            'phase': 'listening', 'search_queries': [], 'search_status': 'not_requested',
            'cards': [], 'draw': None, 'result': None, 'glossary_used': [], 'history': []}.items()}
        view.update(id=str(row['id']), topic=row['topic'], version=row['version'],
            acknowledgement=analysis.get('acknowledgement', ''), understanding=analysis.get('understanding'),
            perspective=analysis.get('perspective'), response_mode=analysis.get('response_mode', 'advice'),
            clarification_question=analysis.get('clarification_question'),
            sources=[{key: source.get(key) for key in ('id', 'kind', 'title', 'url', 'retrieved_at')}
                     for source in state.get('sources', [])],
            job={key: str(job[key]) if key == 'id' else job[key]
                 for key in ('id', 'state', 'error_code')} if job else None)
        return view

    def create(self, owner, key, topic='care'):
        if topic not in ('relationship', 'work', 'self', 'daily', 'care', 'start'):
            raise CounselingError('INVALID_TOPIC')
        with self.store.transaction(owner) as repo:
            row = repo.session_for_key(owner, key)
            if row and row['topic'] != topic:
                raise CounselingError('IDEMPOTENCY_CONFLICT', 409)
            if not row:
                row = repo.create_session(owner, uuid4(), topic, {'phase': 'listening', 'history': []}, key)
            return self._view(repo, row)

    def get(self, owner, session_id):
        with self.store.transaction(owner) as repo:
            return self._view(repo, self._session(repo, owner, session_id))

    def list(self, owner):
        with self.store.transaction(owner) as repo:
            return [dict(id=str(row['id']), topic=row['topic'], phase=row['state'].get('phase', 'listening'),
                summary=(row['state'].get('analysis') or {}).get('understanding', '새 이야기'),
                updated_at=row['updated_at'].isoformat()) for row in repo.sessions(owner)]

    def delete(self, owner, session_id):
        with self.store.transaction(owner) as repo:
            self._session(repo, owner, session_id)
            repo.delete_session(owner, session_id)

    def glossary(self, owner):
        with self.store.transaction(owner) as repo:
            return [public_definition(row) for row in repo.definitions(owner)]

    def define(self, owner, term, definition):
        if (not isinstance(term, str) or not 1 <= len(term.strip()) <= 60
                or not isinstance(definition, str) or not 1 <= len(definition.strip()) <= 400):
            raise CounselingError('INVALID_DEFINITION')
        with self.store.transaction(owner) as repo:
            return public_definition(repo.save_definition(owner, dict(term=term, definition=definition)))

    def forget(self, owner, definition_id):
        with self.store.transaction(owner) as repo:
            entry = next((row for row in repo.definitions(owner) if str(row['id']) == str(definition_id)), None)
            if entry is None:
                raise CounselingError('NOT_FOUND', 404)
            repo.delete_definition(owner, definition_id)
            term = entry['term']
            for row in repo.sessions(owner):
                state = row['state']
                used = any(item['term'] == term for item in state.get('glossary_used', []))
                mentioned = any(term in item['text'] for item in state.get('history', []))
                if used or mentioned:
                    repo.update_session(owner, row['id'], dict(phase='listening',
                        history=[item for item in state.get('history', []) if term not in item['text']]))

    def enqueue(self, owner, session_id, key, kind, payload):
        if kind not in ('message', 'confirm'):
            raise CounselingError('INVALID_COMMAND')
        if kind == 'message':
            text = payload.get('text')
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise CounselingError('INVALID_MESSAGE', 422, '하고 싶은 이야기를 적어주세요.')
        elif payload.get('confirmed') is not True:
            raise CounselingError('CONFIRMATION_REQUIRED', 422, '이해한 내용이 맞는지 먼저 확인해 주세요.')
        elif type(payload.get('card_count', 3)) is not int or payload.get('card_count', 3) not in (1, 3):
            raise CounselingError('INVALID_CARD_COUNT')
        identity = fingerprint([str(session_id), kind, payload])
        with self.store.transaction(owner) as repo:
            previous = repo.command(owner, key)
            if previous:
                if previous['fingerprint'] != identity:
                    raise CounselingError('IDEMPOTENCY_CONFLICT', 409)
                return self._view(repo, self._session(repo, owner, session_id))
            row = self._session(repo, owner, session_id)
            if row['version'] != payload.get('expected_version'):
                raise CounselingError('VERSION_CONFLICT', 409, '이야기가 바뀌었어요. 최신 내용을 확인해 주세요.')
            repo.consent(owner, payload.get('consent_event_id'), self.policy['policy_version'])
            immediate = kind == 'message' and urgent_risk(payload['text'])
            if not immediate and (not self.model or not self.model.available()):
                raise CounselingError('AI_UNAVAILABLE', 503, 'AI 연결이 아직 준비되지 않았어요. 이야기는 그대로 두세요.')
            if kind == 'confirm':
                if row['state'].get('phase') != 'confirmation':
                    raise CounselingError('CONFIRMATION_REQUIRED', 409)
                if row['state'].get('memory_revision') != memory_revision(repo.definitions(owner)):
                    raise CounselingError('GLOSSARY_CHANGED', 409)
            command_id = uuid4()
            repo.reserve(owner, command_id, self.policy)
            repo.enqueue(owner, session_id, key, identity, kind, payload, payload['consent_event_id'], command_id)
            return self._view(repo, row)

    def _check(self, repo, command):
        owner, session_id = command['owner_id'], command['session_id']
        row = self._session(repo, owner, session_id)
        latest = repo.latest_command(owner, session_id)
        if not latest or latest['id'] != command['id'] or latest['state'] != 'running':
            raise CounselingError('COMMAND_EXPIRED', 409)
        repo.consent(owner, command['consent_event_id'], self.policy['policy_version'])
        return row

    def _guard(self, command, revision):
        with self.store.transaction(command['owner_id']) as repo:
            self._check(repo, command)
            if memory_revision(repo.definitions(command['owner_id'])) != revision:
                raise CounselingError('GLOSSARY_CHANGED', 409)

    @staticmethod
    def _support(history, text):
        result = safety_result('chat', [])
        return dict(phase='support', history=history + [dict(role='user', text=text)], result=dict(
            empathy=result['empathy'], facts=[], tarot=[], advice=result['advice'], actions=result['actions'],
            follow_up_question=result['follow_up_questions'][0], safety_mode='support'))

    def _analyze(self, command, state, definitions, revision):
        text = command['payload']['text'].strip()
        history = bounded_history(state.get('history', []))
        if urgent_risk(text):
            return self._support(history, text), []
        known = definitions + conversation_definitions(state)
        retrieved = retrieve_definitions(text, known)
        self._guard(command, revision)
        analysis = Analysis.model_validate(self.model.analyze(dict(text=text, history=history,
            dictionary=[public_definition(entry) for entry in retrieved])))
        if analysis.needs_immediate_support:
            return self._support(history, text), []
        if analysis.intent == 'general_information':
            analysis.explicit_definitions, analysis.unknown_terms = [], []
            analysis.search_purpose = 'none'
            analysis.clarification_question = analysis.clarification_question or '그 일과 관련해서 어떤 마음이나 고민이 있으셨는지 조금 들려주실래요?'
        proposals = personal_definitions(analysis, text)
        missing = [item for item in analysis.unknown_terms
                   if item.kind != 'public' and not retrieve_definitions(item.term, known + proposals)]
        if missing and not analysis.clarification_question:
            analysis.clarification_question = f'괜찮으시면, 말씀하신 “{missing[0].term}”가 어떤 뜻인지 알려주실래요?'
        temporary = analysis.memory_scope == 'conversation' or not command['payload'].get('remember_definitions', True)
        queries = search_queries(analysis, known + proposals)[:1] if (
            analysis.intent == 'tarot_counseling' and analysis.search_purpose == 'clarify_counseling_term') else []
        question = analysis.clarification_question
        if not question:
            question = '' if analysis.understanding.rstrip().endswith(('?', '？')) else '제가 이해한 내용이 맞을까요?'
        following = dict(phase='clarifying' if analysis.clarification_question else 'confirmation',
            analysis=analysis.model_dump(), search_queries=queries,
            session_definitions=(state.get('session_definitions', []) + (proposals if temporary else []))[-20:],
            glossary_used=[public_definition(entry) for entry in retrieved],
            evidence_cache=state.get('evidence_cache', []),
            history=bounded_history(history + [dict(role='user', text=text), dict(role='assistant',
                text=' '.join(filter(None, (analysis.acknowledgement, analysis.understanding, question))))]))
        return following, [] if temporary else proposals

    def _save_cache(self, command, state, revision):
        with self.store.transaction(command['owner_id']) as repo:
            self._check(repo, command)
            if revision != memory_revision(repo.definitions(command['owner_id'])):
                raise CounselingError('GLOSSARY_CHANGED', 409)
            repo.update_session(command['owner_id'], command['session_id'], state)

    def _reply(self, command, state, definitions, revision):
        mode = state['analysis'].get('response_mode', 'advice')
        known = definitions + conversation_definitions(state)
        relevant = retrieve_definitions(' '.join(item['text'] for item in state.get('history', [])), known)
        sources = [dict(id='personal:' + str(entry['id']), kind='user_definition',
            title=entry['term'] + ' · 말씀해주신 의미', text=entry['definition'], url=None) for entry in relevant]
        cache, status, changed = list(state.get('evidence_cache', [])), 'not_requested', False
        if mode != 'listen' and command['payload'].get('web_search') and state.get('search_queries'):
            status = 'completed'
            for query in state['search_queries'][:1]:
                self._guard(command, revision)
                hit = next((item for item in cache if item['query'] == query and time.time() - item['at'] < 600), None)
                if hit:
                    sources.extend(hit['sources'])
                    continue
                try:
                    found = [dict(item, retrieved_at=datetime.now(timezone.utc).isoformat())
                             for item in self.model.search(query)]
                    sources.extend(found)
                    cache.append(dict(query=query, at=time.time(), sources=found))
                    changed = True
                except CounselingError as error:
                    if error.code not in ('AI_UNAVAILABLE', 'AI_RATE_LIMITED', 'AI_PROVIDER_ERROR', 'UNKNOWN_PROVIDER_OUTCOME', 'AI_OUTPUT_INVALID'):
                        raise
                    status = 'unavailable'
            if status == 'completed' and not any(item['kind'] == 'web' for item in sources):
                status = 'no_sources'
        cards = state.get('cards', []) if mode != 'listen' else []
        for card in cards:
            sources.append(dict(id=f"tarot:{card['card_id']}", kind='tarot_reference',
                title=card['card'].get('name_ko', '타로 카드'),
                url=(card['card'].get('image_asset') or {}).get('source_page'),
                text=card['card'].get('basic_meaning', '생각을 돌아보는 상징이에요.')))
        sources = list({source['id']: source for source in sources}.values())
        state = dict(state, cards=cards, evidence_cache=cache[-12:])
        if changed:
            self._save_cache(command, state, revision)
        self._guard(command, revision)
        result = grounded_result(self.model.compose(dict(
            understanding=state['analysis']['understanding'], perspective=state['analysis'].get('perspective'),
            response_mode=mode, history=bounded_history(state.get('history', [])),
            cards=cards, sources=sources, search_status=status)), cards, sources, mode)
        return dict(state, phase='advice', sources=sources, result=result, search_status=status,
            history=bounded_history(state.get('history', []) + [dict(role='assistant',
                text=' '.join((result['empathy'], result['advice'], *result['actions'])))]))

    def run_next(self):
        meta = self.store.claim()
        if not meta:
            return False
        owner, session_id = meta['owner_id'], meta['session_id']
        started = time.monotonic()
        try:
            with self.store.transaction(owner) as repo:
                command = repo.claimed(meta['id'])
                if not command:
                    return True
                state = dict(self._check(repo, command)['state'])
                definitions = repo.definitions(owner)
                revision = memory_revision(definitions)
                if command['kind'] == 'confirm':
                    if state.get('memory_revision') != revision:
                        raise CounselingError('GLOSSARY_CHANGED', 409)
                    if state['analysis'].get('response_mode') != 'listen' and not state.get('cards'):
                        state['cards'] = draw_cards(self.cards, command['payload'].get('card_count', 3), self.sample)
                        state['draw'] = dict(id=str(uuid4()), method='python.SystemRandom.sample',
                            at=datetime.now(timezone.utc).isoformat(), without_replacement=True)
                        repo.update_session(owner, session_id, state)
            if command['kind'] == 'message':
                state, proposals = self._analyze(command, state, definitions, revision)
            else:
                state, proposals = self._reply(command, state, definitions, revision), []
            with self.store.transaction(owner) as repo:
                self._check(repo, command)
                if revision != memory_revision(repo.definitions(owner)):
                    raise CounselingError('GLOSSARY_CHANGED', 409)
                for proposal in proposals:
                    repo.save_definition(owner, proposal, command['id'])
                state['memory_revision'] = memory_revision(repo.definitions(owner))
                repo.update_session(owner, session_id, state)
                repo.finish(command['id'], timings={'run_ms': round((time.monotonic() - started) * 1000)})
        except Exception as error:
            code = error.code if isinstance(error, CounselingError) else 'AI_OUTPUT_INVALID'
            with self.store.transaction(owner, check_active=False) as repo:
                repo.finish(meta['id'], code)
        return True
