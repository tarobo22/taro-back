from copy import deepcopy

import pytest

from api.domain.ai_jobs import AIError, validate_result, validate_turn


RESULT = {'kind': 'chat', 'empathy': '들었어요.', 'summary': '한 걸음씩 살펴봐요.',
          'card_insights': [], 'advice': '선택은 직접 해도 괜찮아요.', 'actions': ['잠깐 쉬어요.'],
          'expression': 'listen', 'follow_up_questions': [], 'safety_mode': 'standard'}


def test_turn_preserves_choices_and_rejects_duplicate_question_and_empty_input():
    assert validate_turn(' ', [{'question_id': 'control', 'answer': False}]) == ''
    for text, answers in [('', []), ('a' * 2001, []), ('ok', [
            {'question_id': 'support', 'answer': True}, {'question_id': 'support', 'answer': False}])]:
        with pytest.raises(AIError):
            validate_turn(text, answers)


def test_structured_result_checks_exact_cards_positions_and_purpose():
    assert validate_result(RESULT, 'chat', []) == RESULT
    tarot = deepcopy(RESULT)
    tarot['kind'] = 'tarot'
    tarot['card_insights'] = [{'card_id': 3, 'position': 1, 'meaning': '뜻', 'reflection': '생각'}]
    assert validate_result(tarot, 'tarot', [{'card_id': 3, 'position': 1}]) == tarot
    for changed in [dict(tarot, card_insights=[]), dict(tarot, kind='chat'),
                    dict(tarot, card_insights=tarot['card_insights'] * 2)]:
        with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
            validate_result(changed, 'tarot', [{'card_id': 3, 'position': 1}])


@pytest.mark.parametrize('changes', [{'actions': []}, {'expression': 'angry'}, {'raw': 'secret'},
                                   {'advice': 'a' * 801}, {'safety_mode': 'unsafe'}])
def test_invalid_result_never_becomes_visible(changes):
    with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
        validate_result(dict(RESULT, **changes), 'chat', [])
