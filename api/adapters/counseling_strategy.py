"""Server-owned support strategies inspired by ESConv and strategy planning.

https://aclanthology.org/2021.acl-long.269/
https://aclanthology.org/2024.acl-long.813/
"""
from dataclasses import dataclass

from api.domain.counseling import CounselingError

PROMPT_VERSION = 'esc-strategy-20261107-v3'


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
    'acknowledgement는 사용자가 표현한 감정이나 부담을 짚는 한 문장입니다. 질문을 섞지 않습니다.\n'
    'understanding은 확인할 상황을 한 문장으로 말합니다. 행위자와 시점은 사용자의 최신 정정을 따릅니다.\n'
    '이해에 꼭 필요한 빈칸만 clarification_question 하나로 확인합니다. 조언과 카드는 확인 뒤에 다룹니다.')
COMFORTING = SupportPromptStrategy('comforting',
    'reflection_of_feelings, restatement_or_paraphrasing, affirmation',
    '경청에서는 empathy에 확인된 상황과 감정을 한 문장으로 반영하고 advice에는 부담을 덜어주는 한 문장을 씁니다.\n'
    '두 문장의 역할을 나눕니다. empathy와 advice 모두 질문, 숙제, 당위, 억지 칭찬을 넣지 않습니다.\n'
    'actions=[], tarot=[], facts=[], follow_up_question=null입니다.')
ACTION = SupportPromptStrategy('action', 'reflection_of_feelings, providing_suggestions',
    '행동 문장은 actions에 정확히 하나만 씁니다. 한 항목 안에 순서가 있는 여러 행동도 넣지 않습니다.\n'
    '사용자가 이미 시도한 방법과 어렵다고 밝힌 조건을 반영합니다. 여유 시간, 돈, 상대의 반응을 가정하지 않습니다.\n'
    'advice는 그 한 가지 제안이 고민에 맞는 이유를 설명합니다. empathy, advice, 카드 reflection에 행동을 더 쓰지 않습니다.\n'
    'follow_up_question은 기본 null입니다. 새 정보 없이는 도움을 줄 수 없고 사용자가 질문을 열어둔 경우만 한 문장으로 묻습니다.')


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
