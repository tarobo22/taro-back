"""P0 AI rules. No database or provider operations live in this module."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError


class AIError(ValueError):
    def __init__(self, code, status=422, message=None, retryable=False):
        self.code, self.status = code, status
        self.message = message or code
        self.retryable = retryable
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CardInsight(StrictModel):
    card_id: StrictInt = Field(ge=0, le=77)
    position: StrictInt = Field(ge=1, le=3)
    meaning: str = Field(max_length=300)
    reflection: str = Field(max_length=400)


class AIResult(StrictModel):
    kind: Literal['tarot', 'chat', 'insight']
    empathy: str = Field(max_length=300)
    summary: str = Field(max_length=600)
    card_insights: list[CardInsight] = Field(max_length=3)
    advice: str = Field(max_length=800)
    actions: list[str] = Field(min_length=1, max_length=3)
    expression: Literal['calm', 'welcome', 'listen', 'encourage', 'joy', 'concern', 'serious']
    follow_up_questions: list[str] = Field(max_length=2)
    safety_mode: Literal['standard', 'support']


class ChoiceAnswer(StrictModel):
    question_id: Literal['control', 'support', 'action']
    answer: StrictBool


def load_policy():
    return json.loads((Path(__file__).parents[2] / 'shared' / 'ai-policy.json').read_text(encoding='utf-8'))


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     default=str, separators=(',', ':')).encode()).hexdigest()


def validate_turn(text, answers):
    try:
        parsed = [ChoiceAnswer.model_validate(answer) for answer in answers]
    except (ValidationError, TypeError):
        raise AIError('INVALID_TURN') from None
    if not isinstance(text, str) or len(text) > 2000 or len(parsed) > 3:
        raise AIError('INVALID_TURN')
    if len({answer.question_id for answer in parsed}) != len(parsed) or not (text.strip() or parsed):
        raise AIError('INVALID_TURN')
    return text.strip()


def validate_result(value, kind, cards):
    try:
        result = AIResult.model_validate(value).model_dump()
        if result['kind'] != kind:
            raise ValueError()
        if any(not action.strip() or len(action) > 300 for action in result['actions']):
            raise ValueError()
        if any(len(question) > 200 for question in result['follow_up_questions']):
            raise ValueError()
        expected = {(card['card_id'], card['position']) for card in cards} if kind == 'tarot' else set()
        actual = [(card['card_id'], card['position']) for card in result['card_insights']]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError()
        return result
    except (ValidationError, ValueError, TypeError, KeyError):
        raise AIError('AI_OUTPUT_INVALID', retryable=True) from None


def safety_result(kind, cards):
    return {'kind': kind, 'empathy': '지금 많이 힘든 마음을 들려주셔서 고마워요.',
            'summary': '지금은 안전을 먼저 살펴봐요.',
            'card_insights': [{'card_id': c['card_id'], 'position': c['position'],
                               'meaning': '', 'reflection': '지금은 카드 해석보다 안전이 먼저예요.'} for c in cards],
            'advice': '혼자 버티기 어렵다면 신뢰하는 가까운 사람에게 곁에 있어 달라고 해 주세요. 당장 위험하다면 지역 긴급 도움을 요청해 주세요.',
            'actions': ['위험한 물건에서 잠시 거리를 두고 안전한 곳으로 이동해 주세요.'],
            'expression': 'serious', 'follow_up_questions': ['지금 곁에 있어 줄 사람이 있나요?'],
            'safety_mode': 'support'}
