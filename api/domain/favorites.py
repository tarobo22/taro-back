"""Favorite validation and owner-bound pagination. No reading content is copied."""
import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from uuid import UUID


class FavoriteInvalid(ValueError):
    pass


class FavoriteNotFound(LookupError):
    pass


class FavoriteConflict(Exception):
    pass


@dataclass(frozen=True)
class FavoritesPolicy:
    page_default: int = 20
    page_max: int = 50

    def limit(self, value):
        value = self.page_default if value is None else value
        if type(value) is not int or not 1 <= value <= self.page_max:
            raise FavoriteInvalid('invalid_page_limit')
        return value


def favorite_uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise FavoriteInvalid('invalid_favorite_identifier') from None


def favorite_input(values):
    if not isinstance(values, dict) or set(values) != {'reading_id'}:
        raise FavoriteInvalid('invalid_favorite_input')
    return favorite_uuid(values['reading_id'])


def favorite_hash(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def decode_favorite_cursor(owner, cursor):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        position = value.pop('after')
        if value != {'kind': 'favorites', 'owner': str(owner)} or not isinstance(position, list) or len(position) != 2:
            raise ValueError
        created_at, reading_id = datetime.fromisoformat(position[0]), UUID(position[1])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return created_at, reading_id
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FavoriteInvalid('invalid_favorite_cursor') from None


def encode_favorite_cursor(owner, created_at, reading_id):
    value = {'kind': 'favorites', 'owner': str(owner), 'after': [created_at.isoformat(), str(reading_id)]}
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
