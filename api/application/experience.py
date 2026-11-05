import hashlib
import json
import re
from uuid import uuid4

from api.domain.experience import ExperienceRuleViolation, reading_plan, validate_counsel


class RequestConflict(ExperienceRuleViolation):
    pass


class GuidanceUnavailable(RuntimeError):
    pass


class ExperienceUseCases:
    def __init__(self, uow_factory, policy, local_guidance, ai_guidance=None, ai_daily_limit=20):
        self._uow = uow_factory
        self.policy = policy
        self.local = local_guidance
        self.ai = ai_guidance
        self.ai_daily_limit = ai_daily_limit

    def _once(self, owner, kind, request_id, payload, execute):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._uow() as uow:
            repo = uow.experiences
            repo.lock(owner, kind, request_id)
            previous = repo.operation(owner, kind, request_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise RequestConflict('request_id_reused_for_different_input')
                return previous['result']
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
            result = uow.experiences.daily(owner, day, topic, plan)
            uow.commit()
            return result

    def counsel(self, owner, topic, question, answers, tone, request_id, use_ai=False, consent=False, parent=None):
        question = validate_counsel(self.policy, topic, question, answers, tone, use_ai, consent)

        def generate(repo):
            context = repo.context(owner, parent) if parent else []
            if parent and not context:
                raise ExperienceRuleViolation('conversation_not_found')
            if re.search(r'자살|죽고\s*싶|나를\s*해치|자해', question):
                advice = self.local.advise(topic, question, answers, tone, context)
                source = 'safety'
            elif use_ai:
                if self.ai is None:
                    raise GuidanceUnavailable('AI 연결이 아직 준비되지 않았어요. 기본 가이드로 이어갈 수 있어요.')
                if repo.ai_usage(owner) >= self.ai_daily_limit:
                    raise GuidanceUnavailable('오늘 AI 대화를 충분히 나누었어요. 기본 가이드로 계속할 수 있어요.')
                advice = self.ai.advise(topic, question, answers, tone, context)
                source = 'openai'
            else:
                advice = self.local.advise(topic, question, answers, tone, context)
                source = 'local'
            result = {**advice, 'topic': topic, 'question': question, 'tone': tone, 'source': source,
                      'parentId': str(parent) if parent else None}
            return repo.save_counsel(owner, uuid4(), result, answers, parent)

        return self._once(owner, 'counsel', request_id,
                          [topic, question, answers, tone, use_ai, consent, str(parent)], generate)

    def list(self, owner, kind):
        with self._uow() as uow:
            return uow.experiences.list(owner, kind)
