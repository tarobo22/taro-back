from typing import Annotated, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field, StrictInt

from api.adapters.http.ai_jobs import InputModel, call, idempotency_key
from api.adapters.http.data_privacy import Card
from api.domain.ai_jobs import AIResult


class DailyReadingInput(InputModel):
    kind: Literal['daily']
    topic: Literal['relationship', 'work', 'self', 'daily', 'care', 'start']
    deck_version_id: UUID


class ThreeReadingInput(InputModel):
    kind: Literal['three_card']
    topic: Literal['relationship', 'work', 'self', 'daily', 'care', 'start']
    deck_version_id: UUID
    question: str = Field(default='', max_length=500)


ReadingInput = Annotated[Union[DailyReadingInput, ThreeReadingInput], Field(discriminator='kind')]


class Slot(InputModel):
    id: UUID
    index: int = Field(ge=1, le=12)


class SelectedCard(InputModel):
    card_id: int = Field(ge=0, le=77)
    position: int = Field(ge=1, le=3)
    position_label: str
    orientation: Literal['upright', 'reversed']
    card: Card


class Reading(InputModel):
    id: UUID
    kind: Literal['daily', 'three_card']
    topic: str
    question: str
    deck_version_id: UUID
    local_day: str | None
    captured_timezone: str
    state: Literal['draft', 'selected', 'generating', 'ready', 'failed']
    version: int
    slots: list[Slot]
    selected_cards: list[SelectedCard]
    result: AIResult | None
    created_at: str


class VersionInput(InputModel):
    expected_version: StrictInt = Field(ge=1)


class ReadingSelection(VersionInput):
    slot_ids: list[UUID] = Field(min_length=3, max_length=3)


class ReadingBasic(InputModel):
    reading_id: UUID
    source: Literal['basic']
    summary: str
    cards: list[SelectedCard]
    advice: str
    action: str


class ReadingList(InputModel):
    items: list[Reading]
    next_cursor: str | None


def create_reading_v2_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['readings'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Key = Annotated[UUID, Depends(idempotency_key)]

    @router.post('/readings', status_code=201, response_model=Reading)
    def create(body: ReadingInput, owner: Owner, key: Key):
        return call(lambda: service.create(owner, key, body.model_dump(mode='json')))

    @router.get('/readings', response_model=ReadingList)
    def readings(owner: Owner, limit: Annotated[int, Query(ge=1, le=50)] = 20,
                 cursor: str | None = None, kind: Literal['daily', 'three_card'] | None = None,
                 topic: str | None = None):
        return call(lambda: service.list(owner, limit, cursor, kind, topic))

    @router.get('/readings/{reading_id}', response_model=Reading)
    def reading(reading_id: UUID, owner: Owner):
        return call(lambda: service.get(owner, reading_id))

    @router.post('/readings/{reading_id}/shuffle', response_model=Reading)
    def shuffle(reading_id: UUID, body: VersionInput, owner: Owner, key: Key):
        return call(lambda: service.shuffle(owner, reading_id, key, body.expected_version))

    @router.post('/readings/{reading_id}/selection', response_model=Reading)
    def select(reading_id: UUID, body: ReadingSelection, owner: Owner, key: Key):
        return call(lambda: service.select(owner, reading_id, key, body.expected_version, body.slot_ids))

    @router.get('/readings/{reading_id}/basic', response_model=ReadingBasic)
    def basic(reading_id: UUID, owner: Owner):
        return call(lambda: service.basic(owner, reading_id))

    @router.delete('/readings/{reading_id}', status_code=204)
    def delete(reading_id: UUID, owner: Owner):
        call(lambda: service.delete(owner, reading_id))

    return router
