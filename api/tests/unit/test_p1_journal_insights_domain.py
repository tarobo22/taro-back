from uuid import uuid4

import pytest

from api.domain.ai_jobs import AIError, load_policy
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
