"""ESConv-informed prompt strategies; no trained strategy tokens or soft prompts.

Research: Liu et al., ACL 2021, section 3.2 and appendix B.
https://aclanthology.org/2021.acl-long.269/
"""
from dataclasses import dataclass

from api.domain.counseling import CounselingError


PROMPT_VERSION = 'esc-strategy-20261107-v1'


@dataclass(frozen=True)
class SupportPromptStrategy:
    stage: str
    skills: str
    rules: str
    example: str

    def render(self, base):
        # Only static, server-owned policy enters instructions. Private RAG remains data.
        return (base + '\n아래 단계별 전략 계약을 우선 적용합니다. 예시는 말투 참고이며 출력은 JSON 스키마를 따릅니다.\n'
                f'<support_strategy stage="{self.stage}" version="{PROMPT_VERSION}">\n'
                f'사용할 기술: {self.skills}\n{self.rules}\n'
                f'직접 작성한 말투 예시: {self.example}\n</support_strategy>')


EXPLORATION = SupportPromptStrategy(
    stage='exploration',
    skills='reflection_of_feelings, restatement_or_paraphrasing, question',
    rules='''감정을 한 문장으로 먼저 반영하고, 현재 이야기에서 확인되는 고민만 짧게 요약합니다.
사연에 없는 감정의 원인·상대방 의도·진단을 덧붙이지 않습니다. 맞는지 조심스럽게 확인합니다.
사전·최근 대화에 이미 있는 답은 다시 묻지 않고, 정정한 내용은 앞선 해석보다 우선합니다.
뜻이 모호할 때만 개방형 질문 하나를 합니다. 이해가 분명하면 같은 질문을 반복하지 않습니다.
경청만 원하면 response_mode=listen으로 두고 조언·검색·카드 추첨을 서두르지 않습니다.
아직 제공되지 않은 카드·조언을 만들어내지 않습니다. 자신도 겪었다는 경험을 지어내지 않습니다.''',
    example='“그것 때문에 너무 화나.” → “화가 많이 나셨겠어요. 괜찮으시면, 어떤 일이 있었는지 조금 들려주실래요?”',
)

COMFORTING = SupportPromptStrategy(
    stage='comforting',
    skills='reflection_of_feelings, restatement_or_paraphrasing, affirmation',
    rules='''지금은 내담자가 듣기를 선택한 단계입니다. 앞선 해결책 제안 지침 대신 경청을 우선합니다.
사용자가 말한 구체적인 부담·노력 하나를 반영합니다. 억지 긍정이나 상대방 비난에 동조하지 않습니다.
empathy와 advice에는 질문·진단·행동 지시 없이 짧고 자연스러운 지지의 말을 적습니다.
actions=[], tarot=[], facts=[], follow_up_question=null입니다. 확인 질문도 덧붙이지 않습니다.
자신이 실제 사람·전문 상담가라거나 같은 경험이 있었다고 꾸미지 않습니다.''',
    example='“오늘은 질문 말고 그냥 들어줘.” → “그럼요. 바로 해결하려 하지 않고, 들려주시는 만큼 듣고 있을게요.”',
)

ACTION = SupportPromptStrategy(
    stage='action',
    skills='reflection_of_feelings, providing_suggestions',
    rules='''사용자가 확인한 고민과 원하는 도움에만 연결해 공감 뒤에 선택 가능한 작은 행동 하나를 제안합니다.
actions는 정확히 한 항목입니다. 해결을 장담하거나 꼭 하라고 지시하지 않습니다.
카드의 상징은 전달된 카드 의미에 근거한 관점으로만 설명하고, 타인의 속마음·미래의 증거로 쓰지 않습니다.
사전의 개인 의미를 외부 사실로 바꾸지 않습니다. 출처가 없는 사실을 채워 넣지 않습니다.
일반적인 산책·심호흡을 사연과 무관하게 반복하지 않습니다. 조언이 안 맞았다고 하면 사용자를 탓하지 않습니다.
상황을 더 알아야 하면 짧은 후속 질문 하나 또는 null로 두고 장문의 해설은 생략합니다.''',
    example='“거절하면 사이가 멀어질까 봐 걱정돼.” → “관계가 소중해서 더 조심스러우셨겠어요. 가능하면 거절과 함께 지금 할 수 있는 범위를 한 문장으로 전해보셔도 좋아요.”',
)


def exploration_prompt(base):
    return EXPLORATION.render(base)


def reply_strategy(mode):
    if mode == 'listen':
        return COMFORTING
    if mode == 'advice':
        return ACTION
    raise CounselingError('AI_OUTPUT_INVALID', 502)


def validate_reply_strategy(value, mode):
    reply_strategy(mode)
    if mode == 'listen':
        if value['actions'] or value['tarot'] or value['facts'] or value['follow_up_question'] is not None:
            raise CounselingError('AI_OUTPUT_INVALID', 502)
    elif len(value['actions']) != 1:
        raise CounselingError('AI_OUTPUT_INVALID', 502)
    return value
