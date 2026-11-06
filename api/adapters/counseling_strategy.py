"""Server-owned support strategies inspired by ESConv, ACL 2021.

This is prompt/context engineering, not model training.
https://aclanthology.org/2021.acl-long.269/
"""
from dataclasses import dataclass

from api.domain.counseling import CounselingError

PROMPT_VERSION = 'esc-strategy-tdd-20261107-v2'


@dataclass(frozen=True)
class SupportPromptStrategy:
    stage: str
    skills: str
    rules: str

    def render(self, base):
        return '\n'.join((base, '아래 단계의 계약을 우선 적용하고 JSON 스키마로 답합니다.',
            f'<support_strategy stage="{self.stage}" version="{PROMPT_VERSION}">',
            self.skills, self.rules, '</support_strategy>'))


EXPLORATION = SupportPromptStrategy('exploration',
    'reflection_of_feelings, restatement_or_paraphrasing, question',
    '사용자의 감정을 짧게 받아주고 이해한 상황을 쉽게 정리합니다. 원인과 상대의 의도를 추측하지 않습니다.\n'
    '뜻이 모호한 부분만 질문 하나로 확인합니다. 사용자의 정정은 이전 해석보다 우선합니다.\n'
    '이미 알려준 내용을 다시 묻지 않고, 조언·카드 해석은 확인 이후로 미룹니다.')
COMFORTING = SupportPromptStrategy('comforting',
    'reflection_of_feelings, restatement_or_paraphrasing, affirmation',
    '지금은 경청을 선택한 단계입니다. 구체적인 부담과 노력을 인정하며 따뜻하게 듣습니다.\n'
    'empathy와 advice에 질문이나 행동 지시를 넣지 않습니다. 억지 긍정과 타인 비난에 동조하지 않습니다.\n'
    'actions=[], tarot=[], facts=[], follow_up_question=null입니다.')
ACTION = SupportPromptStrategy('action', 'reflection_of_feelings, providing_suggestions',
    '확인한 고민에 연결하여 선택할 수 있는 작은 행동 하나만 제안합니다. actions는 정확히 한 항목입니다.\n'
    '사용자가 할 수 있다고 말한 범위 안에서 제안하며, 이미 힘들다고 한 과제를 다시 권하지 않습니다.\n'
    '카드는 주어진 상징을 살펴보는 관점입니다. 타인의 속마음·미래·성공을 증명하지 않습니다.\n'
    '조언을 여러 문단과 카드 설명에 나눠 반복하지 않습니다. 후속 질문은 하나 또는 null입니다.')


def exploration_prompt(base):
    return EXPLORATION.render(base)


def reply_strategy(mode):
    strategies = {'listen': COMFORTING, 'advice': ACTION}
    if mode not in strategies:
        raise CounselingError('AI_OUTPUT_INVALID', 502)
    return strategies[mode]


def validate_reply_strategy(value, mode):
    reply_strategy(mode)
    if mode == 'listen':
        if any(value[field] for field in ('actions', 'tarot', 'facts')) or value['follow_up_question'] is not None:
            raise CounselingError('AI_OUTPUT_INVALID', 502)
    elif len(value['actions']) != 1:
        raise CounselingError('AI_OUTPUT_INVALID', 502)
    return value
