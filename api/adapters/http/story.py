from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.story import StoryConflict, StoryInvalid, StoryNotFound


class StoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StoryStart(StoryModel):
    episode_id: str = Field(pattern=r'^[a-z][a-z0-9-]{0,47}$')
    episode_version: str = Field(pattern=r'^\d+\.\d+\.\d+$', max_length=32)


class StoryChoice(StoryModel):
    expected_version: StrictInt = Field(ge=1)
    node_id: str = Field(min_length=1, max_length=64)
    answer: StrictBool


class StoryRestart(StoryModel):
    expected_version: StrictInt = Field(ge=1)


class StoryEpisode(StoryModel):
    id: str
    version: str
    title: str
    summary: str
    min_choices: int = Field(ge=3, le=5)
    max_choices: int = Field(ge=3, le=5)


class StoryEpisodes(StoryModel):
    items: list[StoryEpisode]


class StoryNode(StoryModel):
    id: str
    prompt: str
    expression: Literal['calm', 'welcome', 'listen', 'encourage', 'joy', 'concern', 'serious']


class StoryEnding(StoryModel):
    id: str
    title: str
    body: str
    action: str


class StoryRun(StoryModel):
    id: UUID
    episode_id: str
    episode_version: str
    title: str
    state: Literal['active', 'completed', 'restarted']
    version: int = Field(ge=1)
    current_node: StoryNode | None
    ending: StoryEnding | None
    choice_count: int = Field(ge=0, le=5)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class StoryProgress(StoryModel):
    run: StoryRun | None


def story_call(operation):
    try: return operation()
    except StoryInvalid: raise HTTPException(422, '이야기 선택과 요청 내용을 확인해 주세요.') from None
    except (StoryNotFound, DataNotFound): raise HTTPException(404, '이어갈 이야기를 찾지 못했어요.') from None
    except (StoryConflict, OwnerDeleting): raise HTTPException(409, '다른 곳에서 이야기가 바뀌었어요. 현재 진행을 다시 확인해 주세요.') from None


def story_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['story'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Key = Annotated[UUID, Header(alias='X-Idempotency-Key')]
    @router.get('/story-episodes', response_model=StoryEpisodes, operation_id='listStoryEpisodes')
    def episodes(owner: Owner): return story_call(lambda: service.episodes(owner))
    @router.get('/story-progress', response_model=StoryProgress, operation_id='getStoryProgress')
    def progress(owner: Owner, episode_id: str = Query(pattern=r'^[a-z][a-z0-9-]{0,47}$')): return story_call(lambda: service.progress(owner, episode_id))
    @router.post('/story-runs', response_model=StoryRun, status_code=201, operation_id='startStory')
    def start(body: StoryStart, owner: Owner, key: Key): return story_call(lambda: service.start(owner, body.model_dump(), key))
    @router.get('/story-runs/{run_id}', response_model=StoryRun, operation_id='getStoryRun')
    def get(run_id: UUID, owner: Owner): return story_call(lambda: service.get(owner, run_id))
    @router.post('/story-runs/{run_id}/choices', response_model=StoryRun, operation_id='chooseStory')
    def choose(run_id: UUID, body: StoryChoice, owner: Owner, key: Key): return story_call(lambda: service.choose(owner, run_id, body.model_dump(), key))
    @router.post('/story-runs/{run_id}/restart', response_model=StoryRun, status_code=201, operation_id='restartStory')
    def restart(run_id: UUID, body: StoryRestart, owner: Owner, key: Key): return story_call(lambda: service.restart(owner, run_id, body.model_dump(), key))
    return router
