"""P0 AI rules. No database or provider operations live in this module."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal
from uuid import UUID

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
    meaning: str = Field(min_length=1, max_length=300)
    reflection: str = Field(min_length=1, max_length=400)


class AIResult(StrictModel):
    kind: Literal['tarot', 'chat', 'insight']
    empathy: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=600)
    card_insights: list[CardInsight] = Field(max_length=3)
    advice: str = Field(min_length=1, max_length=800)
    actions: list[str] = Field(min_length=1, max_length=3)
    expression: Literal['calm', 'welcome', 'listen', 'encourage', 'joy', 'concern', 'serious']
    follow_up_questions: list[str] = Field(max_length=2)
    safety_mode: Literal['standard', 'support']


class ChoiceAnswer(StrictModel):
    question_id: Literal['control', 'support', 'action']
    answer: StrictBool


# Match explicit affirmative claims, not generic words such as "failure",
# "diagnosis" or "guarantee" inside ordinary uncertainty/support language.
_CLAIM_END = r'(?=$|[\s.!?,])'
_UNSAFE_CLAIMS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r'(?:반드시|무조건|틀림없이|확실히|100\s*%)[^.!?\n]{0,32}'
    r'(?:실패(?:합니다|해요|한다|할\s*(?:겁니다|거예요|것입니다))'
    r'|망하게\s*(?:됩니다|돼요)|죽(?:습니다|게\s*됩니다|을\s*겁니다)'
    r'|불행해질\s*(?:겁니다|거예요)|(?:버림받게|배신당하게|헤어지게)\s*(?:됩니다|돼요))' + _CLAIM_END,
    r'(?:당신|너|상대방|사용자)(?:은|는|에게)?[^.!?\n]{0,24}'
    r'(?:우울증|조울증|양극성\s*장애|공황\s*장애|정신\s*질환|암|치매)'
    r'\s*(?:입니다|이에요|이야|이다|(?:이|가)\s*있(?:습니다|어요)|을\s*앓고\s*있(?:습니다|어요))' + _CLAIM_END,
    r'(?:투자\s*)?(?:수익|수익률|원금|합격|성공|완치|치료\s*효과)\s*(?:을|를|은|는|이|가)?\s*'
    r'(?:(?:100\s*%|백\s*퍼센트|반드시|무조건)\s*)?'
    r'(?:보장(?:합니다|해요|된다|됩니다|돼요)|확정(?:입니다|이에요|됐어요))' + _CLAIM_END,
    r'(?:반드시|무조건|100\s*%)\s*(?:성공합니다|합격합니다|완치됩니다|수익이\s*납니다)' + _CLAIM_END,
    r'(?:^|[.!?]\s*)(?:you|they)\s+(?:will\s+(?:definitely|certainly)|(?:definitely|certainly)\s+will)'
    r'\s+(?:fail|die|be\s+abandoned)\b',
    r'(?:^|[.!?]\s*)you\s+(?:have|are\s+suffering\s+from)\s+'
    r'(?:depression|bipolar\s+disorder|panic\s+disorder|cancer|dementia)\b',
    r'(?:^|[.!?]\s*)(?:I|we|this\s+(?:card|reading))\s+guarantee(?:s)?\s+'
    r'(?:your\s+)?(?:profits|returns|success|a\s+cure)\b',
))

# S26 forbids presenting an emotional/psychological score as an established fact.
# These bounded affirmative forms do not classify ordinary action or entry counts.
_SCORE_SUBJECT = r'(?:감정|기분|마음|심리|우울|불안|스트레스)'
_SCORE_NUMBER = r'\d+(?:\.\d+)?'
_SCORE_UNIT = r'(?:점|%|퍼센트|/\s*\d+(?:\.\d+)?)'
_SCORE_ASSERTION = (r'\s*(?:(?:으로|로)\s*)?'
                    r'(?:입니다|이에요|예요|이다|확정(?:입니다|이에요|됐어요|합니다|해요))' + _CLAIM_END)
_INSIGHT_SCORE_CLAIMS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    _SCORE_SUBJECT + r'\s*(?:상태\s*)?(?:점수|지수|수치|수준|강도)\s*(?:은|는|이|가|:|=)?\s*'
    + _SCORE_NUMBER + r'\s*(?:' + _SCORE_UNIT + r')?' + _SCORE_ASSERTION,
    _SCORE_SUBJECT + r'\s*(?:은|는|이|가|:|=)\s*' + _SCORE_NUMBER + r'\s*' + _SCORE_UNIT + _SCORE_ASSERTION,
    r'\b(?:emotion(?:al)?|mood|mental(?:\s+health)?|anxiety|depression|stress)\s+'
    r'(?:score|level|index|rating)\s*(?:is|=|:)\s*' + _SCORE_NUMBER
    + r'\s*(?:%|/\s*\d+(?:\.\d+)?|out\s+of\s+\d+(?:\.\d+)?)?' + _CLAIM_END,
))


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


def validate_context_entry_versions(ids, versions):
    """Optional caller-observed versions bind exactly the explicitly selected IDs."""
    if versions is None:
        return None
    try:
        if not isinstance(versions, dict):
            raise ValueError()
        selected = {str(UUID(str(entry))) for entry in ids}
        normalized = {str(UUID(str(entry))): version for entry, version in versions.items()}
        if (len(selected) != len(ids) or len(normalized) != len(versions)
                or set(normalized) != selected
                or any(type(version) is not int or version < 1 for version in normalized.values())):
            raise ValueError()
        return normalized
    except (ValueError, TypeError, AttributeError):
        raise AIError('INVALID_CONTEXT_VERSIONS') from None


def validate_result(value, kind, cards, result_model=AIResult):
    try:
        result = result_model.model_validate(value).model_dump()
        if result['kind'] != kind:
            raise ValueError()
        if any(not action.strip() or len(action) > 300 for action in result['actions']):
            raise ValueError()
        if any(len(question) > 200 for question in result['follow_up_questions']):
            raise ValueError()
        sentences = [result['empathy'], result['summary'], result['advice'], *result['actions'],
                     *result['follow_up_questions']]
        sentences.extend(text for card in result['card_insights'] for text in (card['meaning'], card['reflection']))
        if any(not sentence.strip() for sentence in sentences):
            raise ValueError()
        if any(pattern.search(unicodedata.normalize('NFKC', sentence))
               for sentence in sentences for pattern in _UNSAFE_CLAIMS):
            raise ValueError()
        if kind == 'insight' and any(pattern.search(unicodedata.normalize('NFKC', sentence))
                                    for sentence in sentences for pattern in _INSIGHT_SCORE_CLAIMS):
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
                               'meaning': '지금은 안전을 먼저 살펴봐요.',
                               'reflection': '지금은 카드 해석보다 안전이 먼저예요.'} for c in cards],
            'advice': '혼자 버티기 어렵다면 신뢰하는 가까운 사람에게 곁에 있어 달라고 해 주세요. 당장 위험하다면 지역 긴급 도움을 요청해 주세요.',
            'actions': ['위험한 물건에서 잠시 거리를 두고 안전한 곳으로 이동해 주세요.'],
            'expression': 'serious', 'follow_up_questions': ['지금 곁에 있어 줄 사람이 있나요?'],
            'safety_mode': 'support'}
