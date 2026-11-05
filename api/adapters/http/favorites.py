from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.favorites import FavoriteConflict, FavoriteInvalid, FavoriteNotFound


class FavoriteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reading_id: UUID


class Favorite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reading_id: UUID
    created_at: datetime


class FavoriteList(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Favorite]
    next_cursor: str | None


def favorite_call(operation):
    try:
        return operation()
    except FavoriteInvalid:
        raise HTTPException(422, '즐겨찾기와 목록 범위를 확인해 주세요.') from None
    except (FavoriteNotFound, DataNotFound):
        raise HTTPException(404, '리딩 또는 즐겨찾기를 찾지 못했어요.') from None
    except (FavoriteConflict, OwnerDeleting):
        raise HTTPException(409, '저장 요청이나 기록 상태를 확인해 주세요.') from None


def favorites_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['journal'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    RequestKey = Annotated[UUID, Header(alias='X-Idempotency-Key')]

    @router.get('/favorites', response_model=FavoriteList, operation_id='listFavorites')
    def list_favorites(owner: Owner, cursor: str | None = Query(None, max_length=1024),
                       limit: int = Query(service.policy.page_default, ge=1, le=service.policy.page_max)):
        return favorite_call(lambda: service.list(owner, cursor=cursor, limit=limit))

    @router.post('/favorites', response_model=Favorite, status_code=201, operation_id='addFavorite')
    def add_favorite(body: FavoriteInput, owner: Owner, request_key: RequestKey):
        return favorite_call(lambda: service.add(owner, body.model_dump(), request_key))

    @router.delete('/favorites/{reading_id}', status_code=204, operation_id='removeFavorite')
    def remove_favorite(reading_id: UUID, owner: Owner):
        favorite_call(lambda: service.remove(owner, reading_id))
        return Response(status_code=204)

    return router
