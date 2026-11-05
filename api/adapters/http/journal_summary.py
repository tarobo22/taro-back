from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.journal_summary import JournalSummaryInvalid


class MoodCounts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    calm: int = Field(ge=0)
    happy: int = Field(ge=0)
    thinking: int = Field(ge=0)
    settled: int = Field(ge=0)
    talk: int = Field(ge=0)


class JournalSummary(BaseModel):
    model_config = ConfigDict(extra='forbid')
    range_start: date
    range_end: date
    entry_count: int = Field(ge=0)
    days_with_entries: list[date]
    mood_counts: MoodCounts


def journal_summary_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['journal'])
    Owner = Annotated[UUID, Depends(owner_dependency)]

    @router.get('/journal-summary', response_model=JournalSummary, operation_id='getJournalSummary')
    def summary(owner: Owner, start: date = Query(..., alias='from'), end: date = Query(..., alias='to')):
        try:
            return service.summary(owner, start=start, end=end)
        except JournalSummaryInvalid:
            raise HTTPException(422, '집계할 날짜 범위를 확인해 주세요.') from None
        except DataNotFound:
            raise HTTPException(404, '기록을 찾지 못했어요.') from None
        except OwnerDeleting:
            raise HTTPException(409, '기록을 처리하고 있어요. 잠시 후 다시 확인해 주세요.') from None

    return router
