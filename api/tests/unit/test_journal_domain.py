from datetime import date
from uuid import UUID

import pytest

from api.domain.journal import JournalEntry, JournalPolicy, JournalRuleViolation


POLICY = JournalPolicy(moods=frozenset({'calm', 'curious'}), max_body_length=12)


def make_entry(**changes):
    values = dict(entry_id=UUID(int=1), owner_id=UUID(int=2),
                  day=date(2026, 11, 5), mood='calm', body='one line', policy=POLICY)
    values.update(changes)
    return JournalEntry.create(**values)


def test_rejects_mood_outside_supplied_catalog():
    with pytest.raises(JournalRuleViolation, match='invalid_mood'):
        make_entry(mood='not-in-catalog')


def test_accepts_valid_entry_and_preserves_owner_and_day():
    entry = make_entry()
    assert (entry.owner_id, entry.day, entry.mood, entry.body) == (
        UUID(int=2), date(2026, 11, 5), 'calm', 'one line')


def test_normalizes_body_without_changing_its_contents():
    assert make_entry(body='  one line  ').body == 'one line'


def test_accepts_configured_length_boundary():
    assert make_entry(body='x' * 12).body == 'x' * 12


def test_rejects_body_above_configured_limit():
    with pytest.raises(JournalRuleViolation, match='body_too_long'):
        make_entry(body='x' * 13)


def test_requires_day_before_persistence():
    with pytest.raises(JournalRuleViolation, match='invalid_day'):
        make_entry(day=None)
