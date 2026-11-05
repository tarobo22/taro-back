from typing import Annotated, Literal
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
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


class DataRequestInput(StrictModel):
    kind: Literal['export', 'erase']
    confirmation: str | None = None


class DataRequestAccepted(StrictModel):
    id: UUID
    kind: Literal['export', 'erase']
    state: str
    receipt: str
    expires_at: datetime


class DataRequest(StrictModel):
    id: UUID
    kind: Literal['export', 'erase']
    state: Literal['queued', 'running', 'completed', 'failed']
    download_available: bool
    expires_at: datetime


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
    StatusOwner = Annotated[UUID | None, Depends(status_owner_dependency or owner_dependency)]
    RequestKey = Annotated[UUID, Depends(request_identity)]

    @router.get('/me', response_model=Me, operation_id='getMe')
    def get_me(owner: Owner):
        return data_call(lambda: service.get_me(owner))

    @router.patch('/me/preferences', response_model=Me, operation_id='updatePreferences')
    def update_preferences(body: PreferencePatch, owner: Owner):
        return data_call(lambda: service.update_preferences(owner, body.model_dump(exclude_unset=True)))

    @router.get('/decks', response_model=DeckList, operation_id='listDecks')
    def list_decks(owner: Owner, cursor: str | None = Query(None, max_length=1024),
                    limit: int = Query(service.policy.catalog_page_default, ge=1, le=service.policy.catalog_page_max)):
        return data_call(lambda: service.list_decks(cursor=cursor, limit=limit))

    @router.get('/decks/{deck_id}', response_model=Deck, operation_id='getDeck')
    def get_deck(deck_id: UUID, owner: Owner):
        return data_call(lambda: service.get_deck(deck_id))

    @router.get('/cards', response_model=CardList, operation_id='listCards')
    def list_cards(owner: Owner, deck_id: UUID | None = None, deck_version_id: UUID | None = None,
                   cursor: str | None = Query(None, max_length=1024),
                   limit: int = Query(service.policy.catalog_page_default, ge=1, le=service.policy.catalog_page_max),
                   arcana: Literal['major', 'minor'] | None = None,
                   suit: Literal['wands', 'cups', 'swords', 'pentacles'] | None = None):
        return data_call(lambda: service.list_cards(deck_id=deck_id, deck_version_id=deck_version_id,
                                                  arcana=arcana, suit=suit, cursor=cursor, limit=limit))

    @router.get('/cards/{card_id}', response_model=Card, operation_id='getCard')
    def get_card(card_id: int, owner: Owner, deck_version_id: UUID | None = None):
        return data_call(lambda: service.get_card(card_id, deck_version_id=deck_version_id))

    @router.post('/feedback', response_model=Feedback, status_code=201, operation_id='createFeedback')
    def create_feedback(body: FeedbackInput, owner: Owner, request_key: RequestKey):
        return data_call(lambda: service.create_feedback(owner, body.model_dump(exclude_none=True), request_key))

    @router.post('/me/data-requests', response_model=DataRequestAccepted, status_code=202, operation_id='createDataRequest')
    def create_data_request(body: DataRequestInput, owner: Owner, request_key: RequestKey,
                            background_tasks: BackgroundTasks, response: Response):
        accepted = data_call(lambda: service.create_data_request(owner, body.kind, body.confirmation, request_key))
        if accepted['state'] in ('queued', 'running'):
            background_tasks.add_task(service.process_request, accepted['id'])
        response.headers['Cache-Control'] = 'no-store'
        return accepted

    @router.get('/data-requests/{request_id}', response_model=DataRequest, operation_id='getDataRequest')
    def get_data_request(request_id: UUID, owner: StatusOwner, response: Response,
                         receipt: Annotated[str | None, Header(alias='X-Data-Receipt', max_length=128)] = None):
        response.headers['Cache-Control'] = 'no-store'
        return data_call(lambda: service.get_data_request(request_id, owner=owner, receipt=receipt))

    @router.get('/data-requests/{request_id}/download', operation_id='downloadExport',
                 response_class=Response, responses={200: {'content': {'application/zip': {}}}})
    def download_export(request_id: UUID, owner: Owner):
        archive = data_call(lambda: service.download_export(owner, request_id))
        return Response(archive, media_type='application/zip', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': f'attachment; filename="yeoun-export-{request_id}.zip"'})

    return router
