from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from api.adapters.http.ai_jobs import ConsentEvent, call
from api.adapters.http.data_privacy import Me, data_call


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Health(StrictModel):
    ok: bool
    service: str
    api_version: str


class OTAPolicy(StrictModel):
    channel: str
    min_native_version: str
    updates_enabled: bool


class Limits(StrictModel):
    daily_ai_requests: int
    question_max_chars: int
    journal_max_chars: int
    audio_max_seconds: int
    audio_max_bytes: int


class Bootstrap(StrictModel):
    api_version: str
    policy_version: str
    server_time: datetime
    user: Me
    consents: list[ConsentEvent]
    ota_policy: OTAPolicy
    limits: Limits


def bootstrap_router(data_service, ai_service, owner_dependency, check_database, *, limits, ota_policy):
    router = APIRouter(prefix='/v2', tags=['system'])
    Owner = Annotated[UUID, Depends(owner_dependency)]

    @router.get('/health', response_model=Health, operation_id='health')
    def health():
        check_database()
        return {'ok': True, 'service': 'yeoun-fastapi', 'api_version': '2.0'}

    @router.get('/bootstrap', response_model=Bootstrap, operation_id='bootstrap')
    def bootstrap(owner: Owner):
        latest, cursor = {}, None
        while len(latest) < 3:
            page = call(lambda: ai_service.list_consents(owner, 50, cursor))
            for consent in page['items']:
                latest.setdefault(consent['kind'], consent)
            cursor = page['next_cursor']
            if cursor is None:
                break
        return {'api_version': '2.0', 'policy_version': ai_service.policy['policy_version'],
                'server_time': datetime.now(timezone.utc), 'user': data_call(lambda: data_service.get_me(owner)),
                'consents': list(latest.values()), 'ota_policy': ota_policy() if callable(ota_policy) else ota_policy, 'limits': limits}

    return router
