"""Owner-scoped data operations. Constructors never acquire a database connection."""
from copy import deepcopy

from api.domain.data_privacy import DataNotFound, InvalidDataInput


class DataPrivacyUseCases:
    def __init__(self, uow_factory, policy, catalog, *, export_callbacks=(), erase_callbacks=(),
                 feedback_target_exists=None, clock=None):
        self.uow_factory = uow_factory
        self.policy = policy
        self.catalog = deepcopy(catalog)
        self.export_callbacks = tuple(export_callbacks)
        self.erase_callbacks = tuple(erase_callbacks)
        self.feedback_target_exists = feedback_target_exists
        self.clock = clock

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
