from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt

from api.application.experience import GuidanceUnavailable, RequestConflict
from api.domain.experience import ExperienceRuleViolation


class CatalogCard(BaseModel):
    id: int
    name: str
    keyword: str
    message: str
    action: str
    art: str


class DrawnCard(CatalogCard):
    position: str


class Spread(BaseModel):
    name: str
    positions: list[str]


class ExperienceConfig(BaseModel):
    topics: list[str]
    cards: list[CatalogCard]
    spreads: dict[str, Spread]
    questions: list[str]
    maxTarotQuestion: int
    maxCounselQuestion: int
    deckPreviewSize: int
    aiAvailable: bool


class ReadingInput(BaseModel):
    topic: str
    question: str = ''
    cardIds: list[StrictInt] = Field(min_length=1, max_length=5)
    spreadId: str = 'three'
    requestId: UUID = Field(default_factory=uuid4)


class ReadingResponse(BaseModel):
    id: UUID | None = None
    day: date | None = None
    topic: str
    question: str
    spreadId: str = 'three'
    cards: list[DrawnCard]
    summary: str
    advice: str
    action: str
    created_at: datetime | None = None


class DailyInput(BaseModel):
    day: date
    topic: str


class CounselInput(BaseModel):
    topic: str
    question: str = ''
    answers: list[StrictBool] = Field(default_factory=list)
    tone: Literal['warm', 'giga'] = 'warm'
    requestId: UUID = Field(default_factory=uuid4)
    useAI: bool = False
    consent: bool = False
    parentId: UUID | None = None


class CounselResponse(BaseModel):
    id: UUID
    topic: str
    question: str
    tone: Literal['warm', 'giga']
    empathy: str
    advice: str
    action: str
    expression: Literal['listen', 'encourage', 'joy', 'concern', 'serious']
    source: Literal['local', 'openai', 'safety'] = 'local'
    parentId: UUID | None = None
    created_at: datetime


def call(operation):
    try:
        return operation()
    except RequestConflict:
        raise HTTPException(409, '바뀐 내용은 새 요청으로 보내 주세요.') from None
    except ExperienceRuleViolation as error:
        if str(error) == 'conversation_not_found':
            raise HTTPException(404, '이전 대화를 찾지 못했어요.') from None
        raise HTTPException(422, '주제·선택한 카드·입력 내용과 AI 동의를 확인해 주세요.') from None
    except GuidanceUnavailable as error:
        raise HTTPException(503, str(error)) from None


def experience_router(service, configuration, owner_dependency):
    router = APIRouter(prefix='/v1', tags=['experience'])
    Owner = Annotated[UUID, Depends(owner_dependency)]

    @router.get('/experience-config', response_model=ExperienceConfig)
    def config():
        return configuration

    @router.post('/readings', status_code=201, response_model=ReadingResponse)
    def create_reading(body: ReadingInput, owner: Owner):
        return call(lambda: service.reading(owner, body.topic, body.question, body.cardIds, body.spreadId, body.requestId))

    @router.get('/readings', response_model=list[ReadingResponse])
    def list_readings(owner: Owner):
        return service.list(owner, 'reading')

    @router.post('/daily', response_model=ReadingResponse)
    def daily(body: DailyInput, owner: Owner):
        return call(lambda: service.daily(owner, body.day, body.topic))

    @router.post('/counsel', status_code=201, response_model=CounselResponse)
    def counsel(body: CounselInput, owner: Owner):
        return call(lambda: service.counsel(owner, body.topic, body.question, body.answers, body.tone,
                     body.requestId, body.useAI, body.consent, body.parentId))

    @router.get('/counsel', response_model=list[CounselResponse])
    def list_counsel(owner: Owner):
        return service.list(owner, 'counsel')

    return router
