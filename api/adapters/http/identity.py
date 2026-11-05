from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.domain.identity import IdentityRuleViolation, SessionRejected


class SessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform: Literal['android', 'ios', 'web']
    locale: str = 'ko-KR'
    timezone: str = 'Asia/Seoul'


class SessionResponse(BaseModel):
    user_id: UUID
    session_id: UUID
    access_token: str = Field(repr=False)
    expires_at: datetime


class RenewalResponse(BaseModel):
    session_id: UUID
    expires_at: datetime


def identity_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['identity'])
    Owner = Annotated[UUID, Depends(owner_dependency)]

    @router.post('/sessions', status_code=201, response_model=SessionResponse)
    def create(body: SessionInput):
        try:
            return service.create(body.platform, body.locale, body.timezone)
        except IdentityRuleViolation:
            raise HTTPException(422, {'code': 'INVALID_PREFERENCES', 'message': '언어와 시간대를 확인해 주세요.', 'retryable': False}) from None

    @router.post('/sessions/refresh', response_model=RenewalResponse)
    def refresh(owner: Owner):
        try:
            return service.refresh(owner)
        except SessionRejected:
            raise HTTPException(401, {'code': 'SESSION_EXPIRED', 'message': '기록 연결을 확인해 주세요. 새 연결로 자동 교체하지 않아요.', 'retryable': False}) from None

    return router
