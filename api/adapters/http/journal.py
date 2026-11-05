from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt

from api.application.journal import JournalNotFound
from api.domain.journal import JournalRequestConflict, JournalRuleViolation, JournalVersionConflict


class CreateEntry(BaseModel):
    day: date
    mood: str
    body: str = ''
    requestId: UUID | None = None


class UpdateEntry(BaseModel):
    mood: str
    body: str = ''
    expectedVersion: StrictInt | None = Field(default=None, ge=1)


class EntryResponse(BaseModel):
    id: UUID
    day: date
    mood: str
    body: str
    created_at: datetime
    updated_at: datetime
    version: int = 1

    model_config = {'from_attributes': True}


class JournalPolicyResponse(BaseModel):
    moods: list[str]
    maxBodyLength: int


class DeleteResponse(BaseModel):
    deleted: bool


def domain_call(operation):
    try:
        return operation()
    except JournalNotFound:
        raise HTTPException(404, '기록을 찾지 못했어요.') from None
    except JournalRequestConflict:
        raise HTTPException(409, '바뀐 기록은 새 요청으로 저장해 주세요.') from None
    except JournalVersionConflict:
        raise HTTPException(409, '다른 곳에서 수정된 기록이에요. 입력한 글을 남겨 두고 최신 기록을 확인해 주세요.') from None
    except JournalRuleViolation:
        raise HTTPException(422, '선택한 마음과 글자 수를 확인해 주세요.') from None


def journal_router(service, policy, owner_dependency):
    router = APIRouter(prefix='/v1', tags=['journal'])
    Owner = Annotated[UUID, Depends(owner_dependency)]

    @router.get('/journal-policy', response_model=JournalPolicyResponse)
    def journal_policy():
        return {'moods': sorted(policy.moods), 'maxBodyLength': policy.max_body_length}

    @router.get('/entries', response_model=list[EntryResponse])
    def entries(owner: Owner):
        return service.list(owner)

    @router.post('/entries', status_code=201, response_model=EntryResponse)
    def create(body: CreateEntry, owner: Owner):
        return domain_call(lambda: service.create(owner, body.day, body.mood, body.body, body.requestId))

    @router.put('/entries/{entry_id}', response_model=EntryResponse)
    def update(entry_id: UUID, body: UpdateEntry, owner: Owner):
        return domain_call(lambda: service.update(owner, entry_id, body.mood, body.body, body.expectedVersion))

    @router.delete('/entries/{entry_id}', response_model=DeleteResponse)
    def delete(entry_id: UUID, owner: Owner):
        return {'deleted': service.delete(owner, entry_id)}

    return router
