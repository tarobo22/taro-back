from typing import Annotated, Literal
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from api.domain.data_privacy import (DataConflict, DataExpired, DataNotFound,
    DataRateLimited, InvalidDataInput, OwnerDeleting)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Preferences(StrictModel):
    tone: Literal['warm', 'giga']
    reduced_motion: bool
    voice_mode: Literal['device', 'cloud']


class Me(StrictModel):
    id: UUID
    kind: Literal['guest', 'member']
    locale: str
    timezone: str
    preferences: Preferences


class PreferencePatch(StrictModel):
    tone: Literal['warm', 'giga'] | None = None
    reduced_motion: StrictBool | None = None
    voice_mode: Literal['device', 'cloud'] | None = None
    timezone: str | None = None
    locale: str | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('A non-null preference is required')
        return self


class Asset(StrictModel):
    id: UUID
    url: str
    sha256: str
    width: int
    height: int
    source_page: str | None
    license_name: str
    license_url: str | None


class Deck(StrictModel):
    id: UUID
    code: str
    version: str
    name: str
    card_count: Literal[78]
    status: Literal['candidate', 'published', 'retired']
    back_asset: Asset


class Card(StrictModel):
    id: int
    code: str
    name_ko: str
    name_en: str
    arcana: Literal['major', 'minor']
    suit: Literal['wands', 'cups', 'swords', 'pentacles'] | None
    basic_meaning: str
    image_asset: Asset | None


class DeckList(StrictModel):
    items: list[Deck]
    next_cursor: str | None


class CardList(StrictModel):
    items: list[Card]
    next_cursor: str | None


class FeedbackInput(StrictModel):
    reading_id: UUID | None = None
    turn_id: UUID | None = None
    rating: Literal['helpful', 'unhelpful']
    reason: str
    comment: str = ''


class Feedback(StrictModel):
    id: UUID
    created_at: datetime


def request_identity(x_idempotency_key: Annotated[str | None, Header(alias='X-Idempotency-Key')] = None,
                     idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    try:
        primary = UUID(x_idempotency_key) if x_idempotency_key is not None else None
        alias = UUID(idempotency_key) if idempotency_key is not None else None
    except ValueError:
        raise HTTPException(422, '요청 식별자를 확인해 주세요.') from None
    if primary is None and alias is None or primary is not None and alias is not None and primary != alias:
        raise HTTPException(422, '요청 식별자를 확인해 주세요.')
    return primary or alias


def data_call(operation):
    try:
        return operation()
    except InvalidDataInput:
        raise HTTPException(422, '입력한 설정과 내용을 확인해 주세요.') from None
    except DataNotFound:
        raise HTTPException(404, '요청한 기록을 찾지 못했어요.') from None
    except (OwnerDeleting, DataConflict):
        raise HTTPException(409, '기록을 처리하고 있어요. 상태를 확인해 주세요.') from None
    except DataExpired:
        raise HTTPException(410, '요청 또는 파일의 보관 기간이 끝났어요.') from None
    except DataRateLimited:
        raise HTTPException(429, '오늘 요청 한도에 도달했어요.') from None


def data_privacy_router(service, owner_dependency, *, status_owner_dependency=None):
    router = APIRouter(prefix='/v2', tags=['data-privacy'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    RequestKey = Annotated[UUID, Depends(request_identity)]

    @router.get('/me', response_model=Me, operation_id='getMe')
    def get_me(owner: Owner):
        return data_call(lambda: service.get_me(owner))

    @router.patch('/me/preferences', response_model=Me, operation_id='updatePreferences')
    def update_preferences(body: PreferencePatch, owner: Owner):
        return data_call(lambda: service.update_preferences(owner, body.model_dump(exclude_unset=True)))

    @router.get('/decks', response_model=DeckList, operation_id='listDecks')
    def list_decks(owner: Owner):
        return service.list_decks()

    @router.get('/decks/{deck_id}', response_model=Deck, operation_id='getDeck')
    def get_deck(deck_id: UUID, owner: Owner):
        return data_call(lambda: service.get_deck(deck_id))

    @router.get('/cards', response_model=CardList, operation_id='listCards')
    def list_cards(owner: Owner, deck_id: UUID | None = None,
                   arcana: Literal['major', 'minor'] | None = None,
                   suit: Literal['wands', 'cups', 'swords', 'pentacles'] | None = None):
        return data_call(lambda: service.list_cards(deck_id=deck_id, arcana=arcana, suit=suit))

    @router.get('/cards/{card_id}', response_model=Card, operation_id='getCard')
    def get_card(card_id: int, owner: Owner):
        return data_call(lambda: service.get_card(card_id))

    @router.post('/feedback', response_model=Feedback, status_code=201, operation_id='createFeedback')
    def create_feedback(body: FeedbackInput, owner: Owner, request_key: RequestKey):
        return data_call(lambda: service.create_feedback(owner, body.model_dump(exclude_none=True), request_key))

    return router
