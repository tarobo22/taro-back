"""Non-AI operations policy, immutable public catalog snapshots, and paging."""
import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID


class AdminError(ValueError):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class AdminPolicy:
    session_seconds: int = 900
    audit_retention_days: int | None = None
    page_default: int = 20
    page_max: int = 50


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def revision(value):
    if type(value) is not int or value < 0:
        raise AdminError('INVALID_VERSION')
    return value


def snapshot_for(catalog, version_id):
    deck = next((value for value in catalog['decks'] if value['id'] == str(version_id)), None)
    if deck is None:
        raise AdminError('CATALOG_VERSION_NOT_FOUND', 404)
    cards = catalog.get('cards_by_deck', {}).get(str(version_id), catalog['cards'])
    if len(cards) != 78 or {card['id'] for card in cards} != set(range(78)) or deck['card_count'] != 78:
        raise AdminError('CATALOG_INCOMPLETE')
    assets = [deck.get('back_asset'), *[card.get('image_asset') for card in cards]]
    try:
        ids = [str(UUID(asset['id'])) for asset in assets]
        if len(set(ids)) != 79:
            raise ValueError
        for asset in assets:
            if (not re.fullmatch('[0-9a-f]{64}', asset['sha256'])
                    or not asset.get('url', '').startswith('https://') or asset['width'] <= 0 or asset['height'] <= 0):
                raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AdminError('CATALOG_ASSET_EVIDENCE_MISSING') from None
    frozen_deck = {**deepcopy(deck), 'status': 'candidate'}
    snapshot = {'deck': frozen_deck, 'cards': deepcopy(cards)}
    return snapshot, sha(json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


def require_rights_evidence(snapshot):
    assets = [snapshot['deck']['back_asset'], *[card['image_asset'] for card in snapshot['cards']]]
    if any(not asset.get('license_name', '').strip()
           or not (asset.get('source_page') or '').startswith('https://')
           or not (asset.get('license_url') or '').startswith('https://') for asset in assets):
        raise AdminError('CATALOG_ASSET_EVIDENCE_MISSING')


def ota_values(values):
    if set(values) != {'channel', 'min_native_version', 'updates_enabled'}:
        raise AdminError('OTA_POLICY_INVALID')
    if (not isinstance(values['channel'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,40}', values['channel'])
            or not isinstance(values['min_native_version'], str)
            or not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', values['min_native_version'])
            or type(values['updates_enabled']) is not bool):
        raise AdminError('OTA_POLICY_INVALID')
    return dict(values)


def page_input(policy, owner, kind, cursor, limit, filters):
    limit = policy.page_default if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= policy.page_max:
        raise AdminError('INVALID_PAGE_LIMIT')
    binding = {'owner': str(owner), 'kind': kind, 'filters': filters}
    after = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                raise ValueError
            value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
            position = value.pop('after')
            if value != binding or len(position) != 2:
                raise ValueError
            after = datetime.fromisoformat(position[0]), UUID(position[1])
            if after[0].tzinfo is None:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AdminError('INVALID_ADMIN_CURSOR') from None
    return limit, binding, after


def page_result(rows, limit, binding):
    page = rows[:limit]
    cursor = None
    if len(rows) > limit:
        last = page[-1]
        stamp = last['created_at'].isoformat() if hasattr(last['created_at'], 'isoformat') else last['created_at']
        cursor = base64.urlsafe_b64encode(json.dumps({**binding, 'after': [stamp, str(last['id'])]},
            sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    return {'items': page, 'next_cursor': cursor}
