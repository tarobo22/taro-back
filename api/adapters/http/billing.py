from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from api.domain.billing import BillingInvalid, BillingRejected, BillingUnavailable
from api.domain.data_privacy import DataNotFound, OwnerDeleting


class BillingModel(BaseModel):model_config = ConfigDict(extra='forbid')
class BillingProduct(BillingModel):
    product_id: str
    kind: Literal['inapp', 'subs']
    entitlement_key: str
class BillingCatalog(BillingModel):
    store: Literal['google_play']
    available: bool
    reason: Literal['catalog_not_configured', 'store_not_configured'] | None
    can_purchase: bool
    account_id: str
    package_name: str
    products: list[BillingProduct]
class Entitlement(BillingModel):
    key: str
    product_id: str
    expires_at: datetime | None
    verified_at: datetime
class Entitlements(BillingModel):
    items: list[Entitlement]
    checked_at: datetime
class VerifyPurchase(BillingModel):
    product_id: str = Field(min_length=1, max_length=150)
    purchase_token: str = Field(min_length=1, max_length=4096)
class VerifiedPurchase(BillingModel):
    state: Literal['active', 'pending', 'expired', 'revoked']
    acknowledged: bool
    entitlements: list[Entitlement]


def billing_call(operation):
    try:return operation()
    except BillingInvalid:raise HTTPException(422, '등록된 상품과 구매 정보를 확인해 주세요.') from None
    except BillingRejected:raise HTTPException(403, '이 계정의 유효한 구매를 확인하지 못했어요. 계정 연결과 스토어 구매를 확인해 주세요.') from None
    except BillingUnavailable:raise HTTPException(503, '스토어 구매를 확인할 수 없어요. 잠시 뒤 복원을 다시 시도해 주세요.') from None
    except DataNotFound:raise HTTPException(404, '기록 연결을 찾지 못했어요.') from None
    except OwnerDeleting:raise HTTPException(409, '데이터 삭제 중에는 구매를 변경할 수 없어요.') from None


def billing_router(service, owner_dependency):
    router = APIRouter(prefix='/v2/billing', tags=['billing'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    @router.get('/catalog', response_model=BillingCatalog, operation_id='getBillingCatalog')
    def catalog(owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return billing_call(lambda: service.catalog(owner))
    @router.get('/entitlements', response_model=Entitlements, operation_id='getBillingEntitlements')
    def entitlements(owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return billing_call(lambda: service.entitlements(owner))
    @router.post('/purchases/verify', response_model=VerifiedPurchase, operation_id='verifyStorePurchase')
    def verify(body: VerifyPurchase, owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return billing_call(lambda: service.verify(owner, body.product_id, body.purchase_token))
    return router
