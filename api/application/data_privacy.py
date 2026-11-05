"""Owner-scoped data operations. Constructors never acquire a database connection."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from api.domain.data_privacy import DataConflict, DataNotFound, DataRateLimited, InvalidDataInput


class DataPrivacyUseCases:
    def __init__(self, uow_factory, policy, catalog, *, export_callbacks=(), erase_callbacks=(),
                 feedback_target_exists=None, clock=None):
        self.uow_factory = uow_factory
        self.policy = policy
        self.catalog = deepcopy(catalog)
        self.export_callbacks = tuple(export_callbacks)
        self.erase_callbacks = tuple(erase_callbacks)
        self.feedback_target_exists = feedback_target_exists
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _me(profile):
        return {'id': str(profile['owner_id']), 'kind': 'guest', 'locale': profile['locale'],
                'timezone': profile['timezone'], 'preferences': {key: profile[key]
                    for key in ('tone', 'reduced_motion', 'voice_mode')}}

    def get_me(self, owner):
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            result = self._me(uow.data.profile(owner, self.policy))
            uow.commit()
            return result

    def update_preferences(self, owner, values):
        values = self.policy.preference_patch(values)
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            uow.data.profile(owner, self.policy)
            uow.data.update_profile(owner, values)
            result = self._me(uow.data.profile(owner, self.policy))
            uow.commit()
            return result

    def list_decks(self):
        return {'items': deepcopy(self.catalog['decks']), 'next_cursor': None}

    def get_deck(self, deck_id):
        for deck in self.catalog['decks']:
            if deck['id'] == str(deck_id):
                return deepcopy(deck)
        raise DataNotFound('deck_not_found')

    def list_cards(self, *, deck_id=None, arcana=None, suit=None):
        if deck_id is not None:
            self.get_deck(deck_id)
        if arcana not in (None, 'major', 'minor') or suit not in (None, 'wands', 'cups', 'swords', 'pentacles'):
            raise InvalidDataInput('invalid_card_filter')
        return {'items': [deepcopy(card) for card in self.catalog['cards']
                          if (arcana is None or card['arcana'] == arcana)
                          and (suit is None or card['suit'] == suit)], 'next_cursor': None}

    def get_card(self, card_id):
        for card in self.catalog['cards']:
            if card['id'] == card_id:
                return deepcopy(card)
        raise DataNotFound('card_not_found')

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    @classmethod
    def _fingerprint(cls, value):
        return cls._digest(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str))

    def create_feedback(self, owner, values, request_key):
        values = self.policy.feedback(values)
        key_hash = self._digest(str(request_key))
        fingerprint = self._fingerprint(values)
        with self.uow_factory() as uow:
            uow.data.lock(owner)
            uow.data.active(owner)
            existing = uow.data.feedback_operation(owner, key_hash)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise DataConflict('idempotency_conflict')
                return {'id': str(existing['id']), 'created_at': existing['created_at'].isoformat()}
            if uow.data.feedback_count(owner, self.clock().replace(hour=0, minute=0, second=0, microsecond=0)) >= self.policy.feedback_daily_limit:
                raise DataRateLimited('feedback_daily_limit')
            for kind in ('reading', 'turn'):
                target = values.get(kind + '_id')
                if target:
                    exists = (self.feedback_target_exists(uow.connection, owner, kind, target)
                              if self.feedback_target_exists else uow.data.legacy_target_exists(owner, kind, target))
                    if not exists:
                        raise DataNotFound('feedback_target_not_found')
            saved = uow.data.add_feedback(owner, uuid4(), values, key_hash, fingerprint)
            uow.commit()
            return {'id': str(saved['id']), 'created_at': saved['created_at'].isoformat()}
