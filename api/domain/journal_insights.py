"""Explicit journal selection rules. Journal text never belongs in a command."""
from datetime import date
from uuid import UUID

from api.domain.ai_jobs import AIError, validate_context_entry_versions


def insight_input(body, maximum=10):
    try:
        required = {'range_start', 'range_end', 'entry_ids', 'consent_event_id'}
        if not isinstance(body, dict) or not required <= body.keys() or body.keys() - required - {'entry_versions'}:
            raise ValueError()
        for field in ('range_start', 'range_end'):
            if not isinstance(body[field], str) or date.fromisoformat(body[field]).isoformat() != body[field]:
                raise ValueError()
        if body['range_start'] > body['range_end']:
            raise ValueError()
        ids = body['entry_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= min(maximum, 10):
            raise ValueError()
        ids = [str(UUID(str(entry))) for entry in ids]
        if len(set(ids)) != len(ids):
            raise ValueError()
        result = {**body, 'entry_ids': ids, 'consent_event_id': str(UUID(str(body['consent_event_id'])))}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AIError('INVALID_INSIGHT') from None
    if 'entry_versions' in body:
        if body['entry_versions'] is None:
            raise AIError('INVALID_CONTEXT_VERSIONS')
        result['entry_versions'] = validate_context_entry_versions(ids, body['entry_versions'])
    return result


def insight_view(row):
    sources = row['sources']
    return {'id': str(row['id']), 'range_start': str(row['range_start']), 'range_end': str(row['range_end']),
            'state': row['state'], 'based_on_count': row['based_on_count'], 'result': row['result'],
            'entry_ids': [str(source['entry_id']) for source in sources],
            'entry_versions': {str(source['entry_id']): source['entry_version'] for source in sources}}
