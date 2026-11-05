import pytest

from api.domain.experience import ExperiencePolicy, ExperienceRuleViolation, reading_plan, validate_counsel

POLICY = ExperiencePolicy(('care',), tuple({'id': i, 'name': str(i), 'keyword': 'pause',
    'message': 'hello', 'action': 'rest', 'art': 'leaf'} for i in range(5)),
    {'one': {'positions': ['today']}, 'three': {'positions': ['now', 'around', 'next']}},
    ('first', 'second', 'third'), 40, 80)


def test_reading_preserves_selected_order_and_supplied_positions():
    plan = reading_plan(POLICY, 'care', '  hello  ', [4, 0, 2], 'three')
    assert [card['id'] for card in plan['cards']] == [4, 0, 2]
    assert [card['position'] for card in plan['cards']] == ['now', 'around', 'next']
    assert plan['question'] == 'hello'


@pytest.mark.parametrize('ids,spread', [([1, 1, 2], 'three'), ([0, 2], 'three'), ([99], 'one')])
def test_invalid_draws_are_rejected(ids, spread):
    with pytest.raises(ExperienceRuleViolation):
        reading_plan(POLICY, 'care', '', ids, spread)


def test_ai_requires_explicit_consent_and_bounds_answers():
    with pytest.raises(ExperienceRuleViolation):
        validate_counsel(POLICY, 'care', 'hi', [], 'warm', True, False)
    with pytest.raises(ExperienceRuleViolation):
        validate_counsel(POLICY, 'care', 'hi', [True] * 4, 'warm', False, False)
    assert validate_counsel(POLICY, 'care', '  hi  ', [False], 'giga', False, False) == 'hi'
