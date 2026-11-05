"""Composition root. Only this outer layer wires configuration, SQL and HTTP."""
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import httpx

from api.adapters.http.journal import journal_router
from api.adapters.http.experience import experience_router
from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.local_guidance import LocalGuidance
from api.adapters.openai_guidance import OpenAIGuidance
from api.application.experience import ExperienceUseCases
from api.domain.experience import ExperiencePolicy
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalUseCases
from api.config import ROOT, Settings
from api.domain.journal import JournalPolicy


class SessionResponse(BaseModel):
    token: str


def create_app(settings=None):
    settings = settings or Settings.from_env()
    content = json.loads((ROOT / 'shared/content.json').read_text(encoding='utf-8'))
    rules = json.loads((ROOT / 'shared/journal-policy.json').read_text(encoding='utf-8'))
    policy = JournalPolicy(frozenset(content['moods']), rules['maxBodyLength'])
    pool = ConnectionPool(settings.database_url, open=False, min_size=1, max_size=6,
                          kwargs={'row_factory': dict_row})
    journals = JournalUseCases(lambda: PostgresJournalUnitOfWork(pool), policy, uuid4)
    experience_rules = json.loads((ROOT / 'shared/experience-policy.json').read_text(encoding='utf-8'))
    experience_policy = ExperiencePolicy(tuple(content['topics']), tuple(content['cards']),
        experience_rules['spreads'], tuple(content['talkQuestions']),
        experience_rules['maxTarotQuestion'], experience_rules['maxCounselQuestion'])
    ai_client = httpx.Client(timeout=20, limits=httpx.Limits(max_connections=2))
    ai_available = bool(settings.openai_model and os.environ.get('OPENAI_API_KEY'))
    ai = OpenAIGuidance(ai_client, settings.openai_model, lambda: os.environ.get('OPENAI_API_KEY')) if ai_available else None
    experiences = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pool), experience_policy,
                                     LocalGuidance(), ai, experience_rules['aiDailyLimit'])
    configuration = {**experience_rules, 'topics': content['topics'], 'cards': content['cards'],
                     'questions': content['talkQuestions'], 'aiAvailable': ai_available}

    @asynccontextmanager
    async def lifespan(app):
        try:
            pool.open(wait=True)
            yield
        finally:
            pool.close()
            ai_client.close()

    app = FastAPI(title='여운 API', version='1.2.0', lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
                       allow_headers=['Content-Type', 'Authorization'])

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({'error': exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        return JSONResponse({'error': '입력한 날짜와 내용을 확인해 주세요.'}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        logging.getLogger('yeoun').error('Request failed: %s', type(exc).__name__)
        return JSONResponse({'error': '저장 연결이 잠시 원활하지 않아요. 입력한 내용은 그대로 두고 다시 시도해 주세요.'}, status_code=500)

    def current_session(authorization: Annotated[str | None, Header()] = None):
        token = (authorization or '').removeprefix('Bearer ')
        if not re.fullmatch('[0-9a-f]{64}', token):
            raise HTTPException(401, '기록 연결을 확인해 주세요.')
        with pool.connection() as connection:
            row = connection.execute('SELECT id FROM app_sessions WHERE token_hash=%s',
                                     (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise HTTPException(401, '기록 연결을 확인해 주세요.')
        return row['id']

    @app.get('/health', tags=['system'])
    def health():
        with pool.connection() as connection:
            connection.execute('SELECT 1')
        return {'ok': True, 'database': 'PostgreSQL', 'service': 'yeoun-fastapi'}

    @app.post('/v1/session', status_code=201, response_model=SessionResponse, tags=['session'])
    def create_session():
        token = secrets.token_hex(32)
        with pool.connection() as connection:
            connection.execute('INSERT INTO app_sessions(id,token_hash) VALUES(%s,%s)',
                               (uuid4(), hashlib.sha256(token.encode()).hexdigest()))
        return {'token': token}

    app.include_router(journal_router(journals, policy, current_session))
    app.include_router(experience_router(experiences, configuration, current_session))
    return app


app = create_app()
