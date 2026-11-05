"""P1 explicitly selected journal insights; reads never invoke a provider."""
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field, StrictInt, field_validator

from api.adapters.http.ai_jobs import InputModel, JobAccepted, call, idempotency_key
from api.domain.ai_jobs import AIResult


class InsightInput(InputModel):
    range_start: date
    range_end: date
    entry_ids: list[UUID] = Field(min_length=1, max_length=10, json_schema_extra={'uniqueItems': True})
    consent_event_id: UUID
    entry_versions: dict[UUID, Annotated[StrictInt, Field(ge=1)]] | None = None

    @field_validator('range_start', 'range_end', mode='before')
    @classmethod
    def dates_are_iso_calendar_strings(cls, value):
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError('Supply an ISO calendar date')
        return value

    @field_validator('entry_versions', mode='before')
    @classmethod
    def supplied_versions_are_a_map(cls, value):
        if value is None:
            raise ValueError('Omit entry_versions or supply the exact selected version map')
        if not isinstance(value, dict) or len({str(UUID(str(key))) for key in value}) != len(value):
            raise ValueError('Supply unique UUID version keys')
        return value


class Insight(InputModel):
    id: UUID
    range_start: date
    range_end: date
    state: Literal['draft', 'generating', 'ready', 'failed', 'cancelled', 'invalidated']
    based_on_count: int = Field(ge=1, le=10)
    result: AIResult | None
    entry_ids: list[UUID] = Field(min_length=1, max_length=10)
    entry_versions: dict[UUID, Annotated[StrictInt, Field(ge=1)]]


def create_journal_insights_router(service, owner_dependency):
    router = APIRouter(prefix='/v2/journal-insights', tags=['journal-insights'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Key = Annotated[UUID, Depends(idempotency_key)]

    @router.post('', status_code=202, response_model=JobAccepted, operation_id='createInsight')
    def create(body: InsightInput, owner: Owner, key: Key):
        payload = body.model_dump(mode='json')
        if 'entry_versions' not in body.model_fields_set:
            payload.pop('entry_versions', None)
        return call(lambda: service.create(owner, key, payload))

    @router.get('/{insight_id}', response_model=Insight, operation_id='getInsight')
    def get(insight_id: UUID, owner: Owner):
        return call(lambda: service.get(owner, insight_id))

    return router
