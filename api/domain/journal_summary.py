"""Read-only journal counts. A diary day, never created_at, defines the range."""
from dataclasses import dataclass
from datetime import date

from api.domain.journal_v2 import JournalV2Policy


class JournalSummaryInvalid(ValueError):
    pass


@dataclass(frozen=True)
class JournalMoodBucket:
    local_day: date
    mood: str
    entry_count: int


def validate_summary_range(start, end):
    if type(start) is not date or type(end) is not date or start > end:
        raise JournalSummaryInvalid('invalid_date_range')


def journal_summary(start: date, end: date, groups: list[JournalMoodBucket], policy: JournalV2Policy) -> dict:
    validate_summary_range(start, end)
    counts = dict.fromkeys(policy.mood_labels, 0)
    days = set()
    for group in groups:
        counts[policy.public_mood(group.mood)] += group.entry_count
        days.add(group.local_day)
    return {'range_start': start.isoformat(), 'range_end': end.isoformat(),
            'entry_count': sum(counts.values()), 'days_with_entries': [day.isoformat() for day in sorted(days)],
            'mood_counts': counts}
