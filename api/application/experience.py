import hashlib
import json
import re
from typing import Callable
from uuid import uuid4

from api.application.experience_ports import ExperienceUnitOfWork, Guidance
from api.application.journal import guard_owner
from api.domain.experience import ExperiencePolicy, ExperienceRuleViolation, reading_plan, validate_counsel


class RequestConflict(ExperienceRuleViolation):
    pass


class GuidanceUnavailable(RuntimeError):
    pass


class OperationInProgress(GuidanceUnavailable):
    pass


class UsageLimitReached(GuidanceUnavailable):
    pass


def fingerprint_of(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ExperienceUseCases:
    def __init__(self, uow_factory: Callable[[], ExperienceUnitOfWork], policy: ExperiencePolicy,
                 local_guidance: Guidance, ai_guidance: Guidance | None = None, ai_daily_limit=20,
                 ai_global_daily_limit=200, ai_lease_seconds=90, ai_guard=None, owner_guard=None):
        self._uow = uow_factory
        self.policy = policy
        self.local = local_guidance
        self.ai = ai_guidance
        self.ai_daily_limit = ai_daily_limit
        self.ai_global_daily_limit = ai_global_daily_limit
        self.ai_lease_seconds = ai_lease_seconds
        self.ai_guard = ai_guard
        self._owner_guard = owner_guard

    def _once(self, owner, kind, request_id, payload, execute):
        fingerprint = fingerprint_of(payload)
        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            repo = uow.experiences
            repo.lock(owner, kind, request_id)
            previous = repo.operation(owner, kind, request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise RequestConflict('request_id_reused_for_different_input')
                if previous['result'].get('deleted') is True:
                    raise RequestConflict('result_deleted')
                return previous['result']
            if kind == 'counsel':
                job = repo.ai_job(owner, request_id)
                if job:
                    if job['fingerprint'] != fingerprint:
                        raise RequestConflict('request_id_reused_for_different_input')
                    raise OperationInProgress('이 요청의 답변을 정리 중이에요. 잠시 뒤 같은 내용으로 다시 시도해 주세요.')
            result = execute(repo)
            repo.remember(owner, kind, request_id, fingerprint, result)
            uow.commit()
            return result

    def reading(self, owner, topic, question, ids, spread, request_id):
        plan = reading_plan(self.policy, topic, question, ids, spread)
        plan['advice'] = self.local.advise(topic, question, [], 'warm', [])['advice']
        return self._once(owner, 'reading', request_id, [topic, question, ids, spread],
                          lambda repo: repo.save_reading(owner, uuid4(), plan))

    def daily(self, owner, day, topic):
        # Daily is owner/date/topic scoped and persisted with an atomic unique-key upsert.
        index = int.from_bytes(hashlib.sha256(f'{owner}:{day}:{topic}'.encode()).digest()[:4], 'big') % len(self.policy.cards)
        plan = reading_plan(self.policy, topic, '오늘 나에게 필요한 한마디', [self.policy.cards[index]['id']], 'one')
        plan['advice'] = self.local.advise(topic, '', [], 'warm', [])['advice']
        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            result = uow.experiences.daily(owner, day, topic, plan)
            uow.commit()
            return result

    def counsel(self, owner, topic, question, answers, tone, request_id, use_ai=False, consent=False, parent=None):
        question = validate_counsel(self.policy, topic, question, answers, tone, use_ai, consent)
        safety = bool(re.search(r'자살|죽고\s*싶|나를\s*해치|자해', question))
        payload = [topic, question, answers, tone, use_ai, consent, str(parent)]
        if use_ai and not safety:
            return self._ai_counsel(owner, topic, question, answers, tone, request_id, parent, payload)

        def generate(repo):
            context = repo.context(owner, parent) if parent else []
            if parent and not context:
                raise ExperienceRuleViolation('conversation_not_found')
            advice = self.local.advise(topic, question, answers, tone, context)
            source = 'safety' if safety else 'local'
            result = {**advice, 'topic': topic, 'question': question, 'tone': tone, 'source': source,
                      'parentId': str(parent) if parent else None}
            return repo.save_counsel(owner, uuid4(), result, answers, parent)

        return self._once(owner, 'counsel', request_id, payload, generate)

    def _ai_counsel(self, owner, topic, question, answers, tone, request_id, parent, payload):
        fingerprint = fingerprint_of(payload)
        lease_id = uuid4()
        guard_stamp = None
        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            repo = uow.experiences
            repo.lock(owner, 'counsel', request_id)
            previous = repo.operation(owner, 'counsel', request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise RequestConflict('request_id_reused_for_different_input')
                if previous['result'].get('deleted') is True:
                    raise RequestConflict('result_deleted')
                return previous['result']
            job = repo.ai_job(owner, request_id)
            if job:
                if job['fingerprint'] != fingerprint:
                    raise RequestConflict('request_id_reused_for_different_input')
                if job['state'] == 'pending' and job['active']:
                    raise OperationInProgress('답변을 정리 중이에요. 잠시 뒤 같은 내용으로 다시 시도해 주세요.')
            context = repo.context(owner, parent) if parent else []
            if parent and not context:
                raise ExperienceRuleViolation('conversation_not_found')
            if self.ai is None:
                raise GuidanceUnavailable('AI 연결이 아직 준비되지 않았어요. 기본 가이드로 이어갈 수 있어요.')
            if self.ai_guard is not None:
                guard_stamp = self.ai_guard.capture(uow.connection, owner)
            repo.reserve_ai(owner, request_id, fingerprint, lease_id, self.ai_daily_limit,
                            self.ai_global_daily_limit, self.ai_lease_seconds)
            uow.commit()

        # External I/O holds no database connection, transaction or advisory lock.
        try:
            advice = self.ai.advise(topic, question, answers, tone, context)
        except Exception:
            try:
                with self._uow() as uow:
                    guard_owner(uow, owner, self._owner_guard)
                    uow.experiences.finish_ai(owner, request_id, lease_id, 'failed')
                    uow.commit()
            except Exception:
                # A failed cleanup is recovered by lease expiry; a reservation still counts.
                pass
            raise

        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            repo = uow.experiences
            repo.lock(owner, 'counsel', request_id)
            previous = repo.operation(owner, 'counsel', request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise RequestConflict('request_id_reused_for_different_input')
                if previous['result'].get('deleted') is True:
                    raise RequestConflict('result_deleted')
                return previous['result']
            job = repo.ai_job(owner, request_id)
            if not job or job['lease_id'] != lease_id or job['state'] != 'pending':
                raise OperationInProgress('요청 상태가 바뀌었어요. 같은 내용으로 다시 확인해 주세요.')
            if self.ai_guard is not None:
                self.ai_guard.validate(uow.connection, owner, guard_stamp)
            result = {**advice, 'topic': topic, 'question': question, 'tone': tone, 'source': 'openai',
                      'parentId': str(parent) if parent else None}
            saved = repo.save_counsel(owner, uuid4(), result, answers, parent)
            repo.remember(owner, 'counsel', request_id, fingerprint, saved)
            repo.finish_ai(owner, request_id, lease_id, 'completed')
            uow.commit()
            return saved

    def list(self, owner, kind):
        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            return uow.experiences.list(owner, kind)

    def delete(self, owner, kind, result_id):
        if kind not in ('reading', 'counsel'):
            raise ExperienceRuleViolation('result_not_found')
        with self._uow() as uow:
            guard_owner(uow, owner, self._owner_guard)
            if not uow.experiences.delete(owner, kind, result_id):
                raise ExperienceRuleViolation('result_not_found')
            uow.commit()
