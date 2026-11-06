from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, create_model, model_validator

from api.adapters.http.data_privacy import request_identity
from api.adapters.http.experience import ReadingResponse, CounselResponse
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.journal_v2 import JournalV2Conflict, JournalV2Gone, JournalV2Invalid, JournalV2NotFound


Mood = Literal['calm', 'happy', 'thinking', 'settled', 'talk']


class StrictJournalModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class JournalInput(StrictJournalModel):
    local_day: date
    mood: Mood
    body: str = ''
    source_reading_id: UUID | None = None
    source_turn_id: UUID | None = None


class JournalPatch(StrictJournalModel):
    model_config = ConfigDict(extra='forbid', json_schema_extra={'minProperties': 2})
    expected_version: StrictInt = Field(ge=1)
    mood: Mood | None = None
    body: str | None = None

    @model_validator(mode='after')
    def changed_field(self):
        changed = self.model_fields_set - {'expected_version'}
        if not changed or any(getattr(self, key) is None for key in changed):
            raise ValueError('At least one non-null field must change')
        return self


class Journal(StrictJournalModel):
    id: UUID
    local_day: date
    mood: Mood
    body: str
    source_reading_id: UUID | None
    source_turn_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


class JournalList(StrictJournalModel):
    items: list[Journal]
    next_cursor: str | None


class JournalReadingSource(StrictJournalModel):
    kind: Literal['reading_v2']
    reading_id: UUID


class JournalTurnSource(StrictJournalModel):
    kind: Literal['turn_v2']
    conversation_id: UUID
    turn_id: UUID


class JournalLegacyReadingSource(StrictJournalModel):
    kind: Literal['legacy_reading']
    reading: ReadingResponse


class JournalLegacyCounselSource(StrictJournalModel):
    kind: Literal['legacy_counsel']
    counsel: CounselResponse


JournalSource = Annotated[JournalReadingSource | JournalTurnSource | JournalLegacyReadingSource |
                          JournalLegacyCounselSource, Field(discriminator='kind')]


def journal_v2_call(operation):
    try:
        return operation()
    except JournalV2Invalid:
        raise HTTPException(422, '마음과 날짜, 글자 수를 확인해 주세요.') from None
    except (JournalV2NotFound, DataNotFound):
        raise HTTPException(404, '기록을 찾지 못했어요.') from None
    except (JournalV2Conflict, OwnerDeleting):
        raise HTTPException(409, '다른 곳에서 바뀐 기록이에요. 최신 기록을 확인해 주세요.') from None
    except JournalV2Gone:
        raise HTTPException(410, '이 저장 요청의 기록이 삭제됐거나 요청 기간이 끝났어요.') from None


def journal_v2_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['journal'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    RequestKey = Annotated[UUID, Depends(request_identity)]
    CreateBody = create_model('JournalInput', __base__=JournalInput,
                               body=(str, Field(default='', max_length=service.policy.max_body_length)))
    PatchBody = create_model('JournalPatch', __base__=JournalPatch,
                              body=(str | None, Field(default=None, max_length=service.policy.max_body_length)))

    @router.post('/journal-entries', response_model=Journal, status_code=201, operation_id='createJournal')
    def create(body: CreateBody, owner: Owner, request_key: RequestKey):
        return journal_v2_call(lambda: service.create(owner, body.model_dump(), request_key))

    @router.get('/journal-entries', response_model=JournalList, operation_id='listJournals')
    def list_entries(owner: Owner, cursor: str | None = Query(None, max_length=1024),
                      limit: int = Query(service.policy.page_default, ge=1, le=service.policy.page_max),
                      start: date | None = Query(None, alias='from'), end: date | None = Query(None, alias='to'),
                      mood: Mood | None = None):
        return journal_v2_call(lambda: service.list(owner, cursor=cursor, limit=limit, start=start, end=end, mood=mood))

    @router.get('/journal-entries/{entry_id}', response_model=Journal, operation_id='getJournal')
    def get(entry_id: UUID, owner: Owner):
        return journal_v2_call(lambda: service.get(owner, entry_id))

    @router.get('/journal-entries/{entry_id}/source', response_model=JournalSource, operation_id='getJournalSource')
    def source(entry_id: UUID, owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return journal_v2_call(lambda: service.source(owner, entry_id))

    @router.patch('/journal-entries/{entry_id}', response_model=Journal, operation_id='updateJournal')
    def update(entry_id: UUID, body: PatchBody, owner: Owner):
        return journal_v2_call(lambda: service.update(owner, entry_id, body.model_dump(exclude_unset=True)))

    @router.delete('/journal-entries/{entry_id}', status_code=204, operation_id='deleteJournal')
    def delete(entry_id: UUID, owner: Owner):
        journal_v2_call(lambda: service.delete(owner, entry_id))
        return Response(status_code=204)

    return router
