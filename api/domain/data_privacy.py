"""Rules for owner preferences, catalog access, and portable private data."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DataPrivacyError(Exception):
    pass


class InvalidDataInput(DataPrivacyError):
    pass


class DataNotFound(DataPrivacyError):
    pass


class OwnerDeleting(DataPrivacyError):
    pass


class DataConflict(DataPrivacyError):
    pass


class DataExpired(DataPrivacyError):
    pass


class DataRateLimited(DataPrivacyError):
    pass


@dataclass(frozen=True)
class DataPolicy:
    version: str
    locales: tuple[str, ...]
    default_locale: str
    default_timezone: str
    feedback_comment_max_chars: int
    feedback_reason_max_chars: int
    feedback_daily_limit: int
    data_request_daily_limit: int
    export_file_hours: int
    receipt_days: int
    erase_confirmation: str
    catalog_page_default: int = 20
    catalog_page_max: int = 50

    @classmethod
    def from_mapping(cls, values):
        return cls(values['version'], tuple(values['locales']), values['defaultLocale'],
                   values['defaultTimezone'], values['feedbackCommentMaxChars'],
                   values['feedbackReasonMaxChars'], values['feedbackDailyLimit'],
                   values['dataRequestDailyLimit'], values['exportFileHours'],
                   values['receiptDays'], values['eraseConfirmation'],
                   values['catalogPageDefault'], values['catalogPageMax'])

    def preference_patch(self, values):
        allowed = {'tone', 'reduced_motion', 'voice_mode', 'timezone', 'locale'}
        if not values or values.keys() - allowed or any(value is None for value in values.values()):
            raise InvalidDataInput('invalid_preferences')
        if 'tone' in values and values['tone'] not in ('warm', 'giga'):
            raise InvalidDataInput('invalid_tone')
        if 'voice_mode' in values and values['voice_mode'] not in ('device', 'cloud'):
            raise InvalidDataInput('invalid_voice_mode')
        if 'reduced_motion' in values and type(values['reduced_motion']) is not bool:
            raise InvalidDataInput('invalid_reduced_motion')
        if 'locale' in values and values['locale'] not in self.locales:
            raise InvalidDataInput('invalid_locale')
        if 'timezone' in values:
            try:
                value = values['timezone']
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError
                ZoneInfo(value)
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                raise InvalidDataInput('invalid_timezone') from None
        return values

    def feedback(self, values):
        if values.get('rating') not in ('helpful', 'unhelpful'):
            raise InvalidDataInput('invalid_rating')
        reason = values.get('reason')
        comment = values.get('comment', '')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= self.feedback_reason_max_chars:
            raise InvalidDataInput('invalid_reason')
        if not isinstance(comment, str) or len(comment) > self.feedback_comment_max_chars:
            raise InvalidDataInput('invalid_comment')
        if values.get('reading_id') and values.get('turn_id'):
            raise InvalidDataInput('feedback_one_target')
        return {**values, 'reason': reason.strip(), 'comment': comment}

    def data_request(self, kind, confirmation):
        if kind not in ('export', 'erase'):
            raise InvalidDataInput('invalid_data_request')
        if kind == 'erase' and confirmation != self.erase_confirmation:
            raise InvalidDataInput('erase_confirmation_required')
        if confirmation is not None and len(confirmation) > len(self.erase_confirmation):
            raise InvalidDataInput('invalid_confirmation')
