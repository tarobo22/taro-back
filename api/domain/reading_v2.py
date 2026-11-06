"""Server-owned P0 draws and ordered slot-selection rules."""
from copy import deepcopy
from uuid import UUID, uuid4

from api.domain.ai_jobs import AIError


TOPICS = ('relationship', 'work', 'self', 'daily', 'care', 'start')
POSITIONS = ('지금의 마음', '주변의 흐름', '다음 한 걸음')


class DeckAccessDenied(Exception):
    pass


def reading_input(body):
    if not isinstance(body, dict) or body.get('kind') not in ('daily', 'three_card'):
        raise AIError('INVALID_READING')
    allowed = {'kind', 'topic', 'deck_version_id'} | ({'question'} if body['kind'] == 'three_card' else set())
    if body.keys() - allowed or body.get('topic') not in TOPICS:
        raise AIError('INVALID_READING')
    try:
        deck = str(UUID(str(body['deck_version_id'])))
    except (KeyError, ValueError, TypeError):
        raise AIError('INVALID_READING') from None
    question = body.get('question', '')
    if not isinstance(question, str) or len(question) > 500:
        raise AIError('INVALID_READING')
    return {**body, 'deck_version_id': deck, **({'question': question.strip()} if body['kind'] == 'three_card' else {})}


def private_pool(card_ids, sample):
    cards = sample(card_ids, 12)
    if len(cards) != 12 or len(set(cards)) != 12 or not set(cards) <= set(card_ids):
        raise RuntimeError('Invalid server random source')
    return [{'id': str(uuid4()), 'index': index, 'card_id': card} for index, card in enumerate(cards, 1)]


def select_cards(pool, slot_ids, cards):
    ids = list(map(str, slot_ids))
    if len(ids) != 3 or len(set(ids)) != 3:
        raise AIError('INVALID_SLOT_SELECTION')
    by_id = {slot['id']: slot for slot in pool}
    if any(slot not in by_id for slot in ids):
        raise AIError('INVALID_SLOT_SELECTION')
    catalog = {card['id']: card for card in cards}
    return [selected_card(catalog[by_id[slot]['card_id']], position, POSITIONS[position - 1])
            for position, slot in enumerate(ids, 1)]


def selected_card(card, position=1, label='오늘의 한 장'):
    return {'card_id': card['id'], 'position': position, 'position_label': label,
            'orientation': 'upright', 'card': deepcopy(card)}
