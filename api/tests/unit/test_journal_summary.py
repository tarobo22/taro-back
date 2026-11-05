import ast
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from api.application.journal_summary import JournalSummaryUseCases
from api.application.journal_summary_ports import JournalSummaryRepository
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.journal_summary import JournalMoodBucket, JournalSummaryInvalid
from api.domain.journal_v2 import JournalV2Policy


OWNER = UUID(int=1)
START, END = date(2026, 10, 8), date(2026, 11, 6)
POLICY = JournalV2Policy.from_legacy(
    ['편안해요', '기뻐요', '생각이 많아요', '조금 정리됐어요', '이야기하고 싶어요'],
    {'maxBodyLength': 4000}, {'idempotencyHours': 24, 'pageDefault': 20, 'pageMax': 50})


class FakeSummaryUnit:
    """A summary use case must work without any database/execute capability."""
    def __init__(self, repository):
        self.summary = repository

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def context(rows=()):
    repository = Mock(spec=JournalSummaryRepository)
    repository.groups.return_value = list(rows)
    factory = Mock(return_value=FakeSummaryUnit(repository))
    service = JournalSummaryUseCases(factory, POLICY)
    return service, repository, factory


def test_summary_reads_the_port_and_counts_shared_legacy_and_v2_moods():
    service, repository, factory = context([
        JournalMoodBucket(START, '편안해요', 2), JournalMoodBucket(START, 'calm', 1),
        JournalMoodBucket(END, '기뻐요', 1), JournalMoodBucket(END, 'talk', 2)])
    result = service.summary(OWNER, start=START, end=END)
    assert result == {'range_start': START.isoformat(), 'range_end': END.isoformat(),
                      'entry_count': 6, 'days_with_entries': [START.isoformat(), END.isoformat()],
                      'mood_counts': {'calm': 3, 'happy': 1, 'thinking': 0, 'settled': 0, 'talk': 2}}
    assert [call[0] for call in repository.method_calls] == ['lock', 'active', 'groups']
    repository.groups.assert_called_once_with(OWNER, START, END)
    factory.assert_called_once_with()


def test_empty_summary_is_zero_without_scores_ai_metadata_or_fabricated_days():
    service, _, _ = context()
    assert service.summary(OWNER, start=START, end=END) == {
        'range_start': START.isoformat(), 'range_end': END.isoformat(), 'entry_count': 0,
        'days_with_entries': [],
        'mood_counts': dict.fromkeys(('calm', 'happy', 'thinking', 'settled', 'talk'), 0)}


def test_summary_dates_are_unique_sorted_and_a_single_day_is_valid():
    service, _, _ = context([JournalMoodBucket(END, 'happy', 3), JournalMoodBucket(START, 'talk', 1),
                             JournalMoodBucket(END, 'calm', 2)])
    assert service.summary(OWNER, start=START, end=END)['days_with_entries'] == [START.isoformat(), END.isoformat()]
    service, repository, _ = context()
    service.summary(OWNER, start=END, end=END)
    repository.groups.assert_called_once_with(OWNER, END, END)


@pytest.mark.parametrize('start,end', [(END, START), (None, END), (START, None),
    ('2026-11-06', END), (START, datetime(2026, 11, 6, tzinfo=timezone.utc))])
def test_invalid_ranges_are_rejected_before_opening_a_unit(start, end):
    service, _, factory = context()
    with pytest.raises(JournalSummaryInvalid):
        service.summary(OWNER, start=start, end=end)
    factory.assert_not_called()


@pytest.mark.parametrize('failure', [OwnerDeleting('owner_deleting'), DataNotFound('owner_not_found')])
def test_owner_lifecycle_denial_stops_before_reading_diary_data(failure):
    service, repository, _ = context()
    repository.active.side_effect = failure
    with pytest.raises(type(failure)):
        service.summary(OWNER, start=START, end=END)
    repository.groups.assert_not_called()


def test_constructor_is_database_free_and_application_has_no_driver_or_sql_operations():
    service, _, factory = context()
    factory.assert_not_called()
    root = Path(__file__).parents[3]
    tree = ast.parse((root / 'api/application/journal_summary.py').read_text(encoding='utf-8'))
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                   'psycopg' in ast.unparse(node) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Attribute) and node.attr in ('execute', 'cursor', 'connection')
                   for node in ast.walk(tree))
    assert service.policy is POLICY
