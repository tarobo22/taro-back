from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from api.adapters.http.data_privacy import Asset
from api.domain.admin import AdminError
from api.domain.data_privacy import DataNotFound, OwnerDeleting


class AdminModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Eligibility(AdminModel):
    eligible: bool
    role: Literal['viewer','publisher'] | None
    session_seconds: int


class AdminGrant(AdminModel):
    access_token: str = Field(repr=False)
    session_id: UUID
    owner_id: UUID
    role: Literal['viewer','publisher']
    expires_at: datetime


class AdminMe(AdminModel):
    owner_id: UUID
    role: Literal['viewer','publisher']
    expires_at: datetime
    audit_retention_days: int | None


class CatalogVersion(AdminModel):
    version_id: UUID
    code: str
    name: str
    version: str
    rights_status: Literal['unreviewed','approved','rejected']
    review_version: int
    review_note: str
    published: bool
    publication_version: int
    current_publication_id: UUID | None
    assets: list[Asset]


class Publication(AdminModel):
    id: UUID
    version_id: UUID
    code: str
    action: Literal['publish','rollback']
    previous_id: UUID | None
    created_at: datetime


class CatalogState(AdminModel):
    items: list[CatalogVersion]
    publications: list[Publication]


Revision = Annotated[int, Field(strict=True,ge=0)]


class ReviewInput(AdminModel):
    expected_review_version: Revision
    rights_status: Literal['approved','rejected']
    notes: str = Field(min_length=1,max_length=1000)


class PublishInput(AdminModel):
    expected_review_version: Revision
    expected_publication_version: Revision


class RollbackInput(AdminModel):
    publication_id: UUID
    expected_publication_version: Revision


class FeedbackItem(AdminModel):
    id: UUID
    reading_id: UUID | None
    turn_id: UUID | None
    rating: Literal['helpful','unhelpful']
    reason: str
    comment: str
    created_at: datetime
    triage_status: Literal['new','reviewing','resolved','dismissed']
    triage_version: int


class FeedbackPage(AdminModel):
    items: list[FeedbackItem]
    next_cursor: str | None


class TriageInput(AdminModel):
    expected_version: Revision
    status: Literal['new','reviewing','resolved','dismissed']


class AuditItem(AdminModel):
    id: UUID
    action: str
    target_type: str
    target_id: str
    actor_owner_id: UUID | None
    details: dict[str,Any]
    created_at: datetime


class AuditPage(AdminModel):
    items: list[AuditItem]
    next_cursor: str | None


class OtaPolicy(AdminModel):
    channel: str
    min_native_version: str
    updates_enabled: bool
    version: int


class OtaInput(AdminModel):
    expected_version: Revision
    channel: str = Field(min_length=1,max_length=40)
    min_native_version: str = Field(max_length=17)
    updates_enabled: StrictBool


def admin_call(operation):
    try:
        return operation()
    except AdminError as error:
        raise HTTPException(error.status, {'code':error.code,'message':'관리 권한과 최신 상태를 확인해 주세요.',
            'retryable':False}, headers={'Cache-Control':'no-store'}) from None
    except (DataNotFound,OwnerDeleting):
        raise HTTPException(401, {'code':'ADMIN_SESSION_INVALID','message':'관리 연결을 다시 확인해 주세요.',
            'retryable':False}, headers={'Cache-Control':'no-store'}) from None


def admin_router(service):
    router = APIRouter(prefix='/v2/admin',tags=['admin'])
    Authorization = Annotated[str | None,Header()]
    Limit = Annotated[int | None,Query(ge=1,le=50)]

    @router.get('/eligibility',response_model=Eligibility,operation_id='getAdminEligibility')
    def eligibility(response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.eligibility(authorization))

    @router.post('/sessions',status_code=201,response_model=AdminGrant,operation_id='createAdminSession')
    def issue(response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.issue(authorization))

    @router.get('/me',response_model=AdminMe,operation_id='getAdminSession')
    def me(response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.me(authorization))

    @router.delete('/sessions/current',status_code=204,operation_id='deleteAdminSession')
    def logout(authorization:Authorization=None):
        admin_call(lambda:service.logout(authorization))
        return Response(status_code=204,headers={'Cache-Control':'no-store'})

    @router.get('/catalog',response_model=CatalogState,operation_id='getAdminCatalog')
    def catalog(response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.catalog(authorization))

    @router.post('/catalog/{version_id}/review',response_model=CatalogState,operation_id='reviewAdminCatalog')
    def review(version_id:UUID,body:ReviewInput,response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.review(authorization,version_id,body.expected_review_version,body.rights_status,body.notes))

    @router.post('/catalog/{version_id}/publish',response_model=CatalogState,operation_id='publishAdminCatalog')
    def publish(version_id:UUID,body:PublishInput,response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.publish(authorization,version_id,body.expected_review_version,body.expected_publication_version))

    @router.post('/catalog/{version_id}/rollback',response_model=CatalogState,operation_id='rollbackAdminCatalog')
    def rollback(version_id:UUID,body:RollbackInput,response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.rollback(authorization,version_id,body.publication_id,body.expected_publication_version))

    @router.get('/feedback',response_model=FeedbackPage,operation_id='listAdminFeedback')
    def feedback(response:Response,authorization:Authorization=None,cursor:str|None=None,limit:Limit=None,
                 status:Literal['new','reviewing','resolved','dismissed']|None=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.feedback(authorization,cursor=cursor,limit=limit,status=status))

    @router.patch('/feedback/{feedback_id}/triage',response_model=FeedbackItem,operation_id='triageAdminFeedback')
    def triage(feedback_id:UUID,body:TriageInput,response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.triage(authorization,feedback_id,body.expected_version,body.status))

    @router.get('/audit',response_model=AuditPage,operation_id='listAdminAudit')
    def audit(response:Response,authorization:Authorization=None,cursor:str|None=None,limit:Limit=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.audits(authorization,cursor=cursor,limit=limit))

    @router.get('/ota-policy',response_model=OtaPolicy,operation_id='getAdminOtaPolicy')
    def get_ota(response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.get_ota(authorization))

    @router.put('/ota-policy',response_model=OtaPolicy,operation_id='updateAdminOtaPolicy')
    def set_ota(body:OtaInput,response:Response,authorization:Authorization=None):
        response.headers['Cache-Control']='no-store'
        return admin_call(lambda:service.set_ota(authorization,body.expected_version,body.model_dump(exclude={'expected_version'})))

    return router
