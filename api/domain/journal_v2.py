from dataclasses import dataclass
from datetime import date


class JournalV2Invalid(ValueError):
    pass


class JournalV2NotFound(LookupError):
    pass


class JournalV2Conflict(Exception):
    pass


class JournalV2Gone(Exception):
    pass


@dataclass(frozen=True)
class JournalV2Policy:
    mood_labels: dict[str, str]
    max_body_length: int
    idempotency_hours: int
    page_default: int
    page_max: int

    @classmethod
    def from_legacy(cls, moods, rules, extra_rules):
        codes = ('calm', 'happy', 'thinking', 'settled', 'talk')
        if len(moods) != len(codes) or len(set(moods)) != len(codes):
            raise ValueError('Expected the five existing legacy mood labels')
        return cls(dict(zip(codes, moods)), rules['maxBodyLength'], extra_rules['idempotencyHours'],
                   extra_rules['pageDefault'], extra_rules['pageMax'])

    def stored_mood(self, mood):
        if mood not in self.mood_labels:
            raise JournalV2Invalid('invalid_mood')
        return self.mood_labels[mood]

    def public_mood(self, mood):
        if mood in self.mood_labels:
            return mood
        for code, label in self.mood_labels.items():
            if label == mood:
                return code
        raise JournalV2Invalid('unknown_stored_mood')

    def body(self, body):
        if not isinstance(body, str) or len(body) > self.max_body_length:
            raise JournalV2Invalid('invalid_body')
        return body.strip()

    def validate_day(self, day):
        if type(day) is not date:
            raise JournalV2Invalid('invalid_local_day')
        return day

    def create_values(self, values):
        if set(values) - {'local_day', 'mood', 'body', 'source_reading_id', 'source_turn_id'}:
            raise JournalV2Invalid('unknown_create_field')
        if values.get('source_reading_id') and values.get('source_turn_id'):
            raise JournalV2Invalid('one_journal_source')
        return {'local_day': self.validate_day(values.get('local_day')),
                'mood': self.stored_mood(values.get('mood')), 'body': self.body(values.get('body', '')),
                'source_reading_id': values.get('source_reading_id'), 'source_turn_id': values.get('source_turn_id')}

    def patch_values(self, values):
        if set(values) - {'expected_version', 'mood', 'body'} or len(values) < 2:
            raise JournalV2Invalid('invalid_patch')
        version = values.get('expected_version')
        if type(version) is not int or version < 1:
            raise JournalV2Invalid('expected_version_required')
        result = {'expected_version': version}
        if 'mood' in values:
            result['mood'] = self.stored_mood(values['mood'])
        if 'body' in values:
            result['body'] = self.body(values['body'])
        return result
