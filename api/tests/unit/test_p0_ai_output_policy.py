from copy import deepcopy

import pytest

from api.domain.ai_jobs import AIError, safety_result, validate_result
from api.tests.unit.test_p0_ai_domain import RESULT


@pytest.mark.parametrize('field', ['empathy', 'summary', 'advice'])
@pytest.mark.parametrize('blank', ['', ' \n\t '])
def test_required_ai_sentences_reject_empty_or_whitespace(field, blank):
    with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
        validate_result({**RESULT, field: blank}, 'chat', [])


@pytest.mark.parametrize('field', ['meaning', 'reflection'])
def test_selected_card_required_explanation_cannot_be_blank(field):
    result = deepcopy(RESULT)
    result.update(kind='tarot', card_insights=[{'card_id': 0, 'position': 1, 'meaning': '새 시작', 'reflection': '작게 시도해요.'}])
    result['card_insights'][0][field] = '  '
    with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
        validate_result(result, 'tarot', [{'card_id': 0, 'position': 1}])


@pytest.mark.parametrize('sentence', [
    '당신의 미래는 반드시 실패합니다.',
    '너는 무조건 버림받게 됩니다.',
    '당신은 우울증입니다.',
    '당신에게 공황장애가 있습니다.',
    '이 카드는 투자 수익을 100% 보장합니다.',
    '완치를 보장합니다.',
    'You will definitely fail.',
    'You have depression.',
])
def test_explicit_negative_certainty_diagnosis_and_guarantees_are_failed(sentence):
    for field in ('summary', 'advice'):
        with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
            validate_result({**RESULT, field: sentence}, 'chat', [])


@pytest.mark.parametrize('sentence', [
    '실패가 걱정될 수 있지만 결과를 단정하지 않고 작은 선택부터 살펴봐요.',
    '힘들 때는 반드시 가까운 사람에게 도움을 요청해 주세요.',
    '이 답변은 우울증을 진단하거나 치료하지 않아요.',
    '수익을 보장할 수 없어요. 선택에 앞서 정보를 확인해 주세요.',
    '완치를 보장하는 안내는 제공하지 않아요.',
    '당신은 반드시 실패한다는 뜻이 아니에요.',
    'If you have depression, consider seeking professional support.',
    'You are not guaranteed any financial returns.',
])
def test_support_and_uncertainty_language_is_not_blocked_by_generic_words(sentence):
    candidate = {**RESULT, 'advice': sentence}
    assert validate_result(candidate, 'chat', []) == candidate


def test_internal_safety_guidance_remains_complete_and_valid_for_selected_cards():
    cards = [{'card_id': 0, 'position': 1}]
    result = safety_result('tarot', cards)
    assert validate_result(result, 'tarot', cards) == result
    assert result['card_insights'][0]['meaning'].strip()
