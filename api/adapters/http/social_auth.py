from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.identity import SessionRejected
from api.domain.social_auth import SocialAuthError


class AuthInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SocialStart(AuthInput):
    return_uri: str = Field(max_length=2048)
    code_challenge: str = Field(min_length=43, max_length=43)
    platform: Literal['web', 'android', 'ios']
    intent: Literal['sign_in', 'link'] = 'sign_in'


class SocialStarted(AuthInput):
    authorization_url: str
    expires_at: datetime


class SocialExchange(AuthInput):
    ticket: str = Field(min_length=43, max_length=128, repr=False)
    code_verifier: str = Field(min_length=43, max_length=128, repr=False)


class SocialGrant(AuthInput):
    user_id: UUID
    session_id: UUID
    access_token: str = Field(repr=False)
    expires_at: datetime
    previous_user_id: UUID
    guest_records_preserved: Literal[True]


class SocialProviderStatus(AuthInput):
    provider: Literal['google', 'apple']
    available: bool
    reason: Literal['provider_not_configured'] | None


class SocialProviders(AuthInput):
    providers: list[SocialProviderStatus]


class SocialAccount(AuthInput):
    user_id: UUID
    kind: Literal['guest', 'member']
    providers: list[Literal['google', 'apple']]


class SocialDeviceSession(AuthInput):
    id: UUID
    platform: Literal['web', 'android', 'ios']
    created_at: datetime
    expires_at: datetime
    current: bool


class SocialDeviceSessions(AuthInput):
    items: list[SocialDeviceSession]


def social_call(operation):
    try:
        return operation()
    except SocialAuthError as error:
        raise HTTPException(error.status, {'code': error.code, 'message': '로그인 연결과 계정 상태를 확인해 주세요.',
                                          'retryable': error.status in (429, 503)}) from None
    except SessionRejected:
        raise HTTPException(401, {'code': 'SESSION_INVALID', 'message': '기록 연결을 다시 확인해 주세요.', 'retryable': False}) from None
    except DataNotFound:
        raise HTTPException(404, '기록 연결을 찾지 못했어요.') from None
    except OwnerDeleting:
        raise HTTPException(409, '데이터 삭제를 처리하고 있어요.') from None


def social_auth_router(service, owner_dependency):
    router = APIRouter(prefix='/v2/auth', tags=['social-auth'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Authorization = Annotated[str | None, Header()]

    @router.get('/providers', response_model=SocialProviders, operation_id='listSocialProviders')
    def providers(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.providers()

    @router.post('/social/{provider}/start', status_code=201, response_model=SocialStarted, operation_id='startSocialLogin')
    def start(provider: Literal['google', 'apple'], body: SocialStart, owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return social_call(lambda: service.start(owner, provider, body.return_uri, body.code_challenge, body.platform, intent=body.intent))

    async def callback_response(provider, request, values):
        if any(len(values.get(key, [])) != 1 for key in ('state',)) or any(len(values.get(key, [])) > 1 for key in ('code', 'error')):
            raise HTTPException(400, '로그인 응답을 확인해 주세요.')
        state = values['state'][0]
        code = values.get('code', [None])[0]
        error = values.get('error', [None])[0]
        if len(state) > 128 or code is not None and len(code) > 8192 or error is not None and len(error) > 128:
            raise HTTPException(400, '로그인 응답을 확인해 주세요.')
        target = await run_in_threadpool(lambda: social_call(lambda: service.callback(provider, state, code=code, error=error)))
        return RedirectResponse(target, status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @router.get('/social/{provider}/callback', operation_id='completeSocialLoginQuery')
    async def callback_get(provider: Literal['google', 'apple'], request: Request):
        raw = request.scope.get('query_string', b'')
        request.scope['query_string'] = b''  # Prevent authorization codes/states in ASGI access logs.
        if len(raw) > 16384:
            raise HTTPException(400, '로그인 응답을 확인해 주세요.')
        try:
            values = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=10)
        except (ValueError, UnicodeError):
            raise HTTPException(400, '로그인 응답을 확인해 주세요.') from None
        return await callback_response(provider, request, values)

    @router.post('/social/{provider}/callback', operation_id='completeSocialLoginForm')
    async def callback_post(provider: Literal['google', 'apple'], request: Request):
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, '로그인 응답 형식을 확인해 주세요.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, '로그인 응답을 확인해 주세요.')
        try:
            values = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=10)
        except (ValueError, UnicodeError):
            raise HTTPException(400, '로그인 응답을 확인해 주세요.') from None
        return await callback_response(provider, request, values)

    @router.post('/social/exchange', status_code=201, response_model=SocialGrant, operation_id='exchangeSocialTicket')
    def exchange(body: SocialExchange, owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return social_call(lambda: service.exchange(owner, body.ticket, body.code_verifier))

    @router.get('/account', response_model=SocialAccount, operation_id='getSocialAccount')
    def account(owner: Owner, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return social_call(lambda: service.account(owner))

    @router.get('/sessions', response_model=SocialDeviceSessions, operation_id='listAccountSessions')
    def sessions(owner: Owner, response: Response, authorization: Authorization = None):
        response.headers['Cache-Control'] = 'no-store'
        return social_call(lambda: service.sessions(owner, authorization))

    @router.post('/logout', status_code=204, operation_id='logoutAccountSession')
    def logout(owner: Owner, authorization: Authorization = None):
        social_call(lambda: service.logout(authorization))
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @router.delete('/sessions/{session_id}', status_code=204, operation_id='revokeAccountSession')
    def revoke(session_id: UUID, owner: Owner):
        social_call(lambda: service.revoke_session(owner, session_id))
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @router.post('/logout-all', status_code=204, operation_id='logoutAllAccountSessions')
    def logout_all(owner: Owner):
        social_call(lambda: service.logout_all(owner))
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    return router
