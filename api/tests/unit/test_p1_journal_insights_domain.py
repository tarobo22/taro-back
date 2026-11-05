from uuid import uuid4

import pytest

from api.domain.ai_jobs import AIError, load_policy, validate_result
from api.domain.journal_insights import insight_input


def body(count=1, **changes):
    return {'range_start': '2026-11-01', 'range_end': '2026-11-06',
            'entry_ids': [str(uuid4()) for _ in range(count)],
            'consent_event_id': str(uuid4()), **changes}


def test_insights_allow_ten_explicit_sources_without_raising_p0_context_limit():
    value = body(10)
    assert insight_input(value, 10) == value
    assert load_policy()['context_entries_max'] == 3
    assert load_policy()['insight_entries_max'] == 10


@pytest.mark.parametrize('count', [0, 11])
def test_insights_require_one_to_ten_sources(count):
    with pytest.raises(AIError, match='INVALID_INSIGHT'):
        insight_input(body(count), 10)


@pytest.mark.parametrize('changes', [
    {'range_start': '2026-11-07'}, {'range_start': '2026-02-30'},
    {'range_start': '2026-11-01T00:00:00'}, {'range_end': None},
    {'entry_ids': ['invalid']}, {'consent_event_id': None},
    {'extra': 'must not accept automatic context'},
])
def test_insight_input_rejects_invalid_or_implicit_selection(changes):
    with pytest.raises(AIError, match='INVALID_INSIGHT'):
        insight_input(body(**changes), 10)


def test_duplicate_and_non_exact_version_maps_are_rejected():
    entry = str(uuid4())
    with pytest.raises(AIError, match='INVALID_INSIGHT'):
        insight_input(body(entry_ids=[entry, entry.upper()]), 10)
    for versions in ({}, {str(uuid4()): 1}, {entry: True}, {entry: 1.0}, {entry: 0}, None):
        with pytest.raises(AIError, match='INVALID_CONTEXT_VERSIONS'):
            insight_input(body(entry_ids=[entry], entry_versions=versions), 10)
    value = body(entry_ids=[entry], entry_versions={entry: 2})
    assert insight_input(value, 10) == value


def result(summary, kind='insight'):
    return {'kind': kind, 'empathy': '마음을 나눠 주셨네요.', 'summary': summary, 'card_insights': [],
            'advice': '작은 행동을 골라 봐요.', 'actions': ['잠시 쉬어요.'], 'expression': 'listen',
            'follow_up_questions': [], 'safety_mode': 'standard'}


@pytest.mark.parametrize('claim', [
    '감정 점수는 98점입니다.', '당신의 불안 수치는 80%입니다.', '심리 점수는 8/10입니다.',
    '스트레스 지수는 ９８점으로 확정입니다.', '기분은 3점이에요.', '감정 점수는 98입니다.',
    'Your anxiety level is 80%.', 'Your mood score is 8/10.',
])
def test_insights_reject_clear_affirmative_emotional_score_claims_only(claim):
    with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
        validate_result(result(claim), 'insight', [])
    # P0 output behavior is unchanged by this insight-specific rule.
    assert validate_result(result(claim, 'chat'), 'chat', [])['summary'] == claim


@pytest.mark.parametrize('observation', [
    '최근 3번의 기록을 돌아보며 작은 행동 하나를 골라 보세요.',
    '하루에 3번 숨을 천천히 쉬어 보세요.', '기록 10개에서 비슷한 고민이 보여요.',
    '감정 점수를 98점으로 확정할 수 없어요.', '불안의 크기를 수치로 단정하지 않아요.',
    'Your mood score is not fixed at 80.', 'Try taking 3 slow breaths.',
])
def test_insights_allow_action_counts_and_non_definitive_observations(observation):
    assert validate_result(result(observation), 'insight', [])['summary'] == observation
