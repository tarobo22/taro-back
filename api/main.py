"""Composition root. Configuration, SQL, HTTP and workers meet only at this boundary."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.adapters.ai_audio import PrivateAudioStore
from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.adapters.ai_openai import OpenAIResultProvider, OpenAITranscriptionProvider
from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork, initialize_profile, owner_guard
from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.favorites_postgres import PostgresFavoritesUnitOfWork
from api.adapters.journal_summary_postgres import PostgresJournalSummaryUnitOfWork
from api.adapters.journal_insights_postgres import PostgresJournalInsightsUnitOfWork
from api.adapters.http.ai_jobs import create_ai_jobs_router
from api.adapters.http.bootstrap import bootstrap_router
from api.adapters.http.data_privacy import data_privacy_router
from api.adapters.http.experience import experience_router
from api.adapters.http.favorites import favorites_router
from api.adapters.http.journal_summary import journal_summary_router
from api.adapters.http.journal_insights import create_journal_insights_router
from api.adapters.http.identity import identity_router
from api.adapters.http.journal import journal_router
from api.adapters.http.journal_v2 import journal_v2_router
from api.adapters.http.reading_v2 import create_reading_v2_router
from api.adapters.identity_postgres import PostgresIdentityUnitOfWork
from api.adapters.journal_v2_postgres import PostgresJournalV2UnitOfWork
from api.adapters.journal_source_postgres import PostgresJournalSourceLookup
from api.adapters.billing_postgres import PostgresBillingUnitOfWork
from api.adapters.google_play_billing import GooglePlayVerifier, GoogleApplicationCredentials, FernetReceiptCipher
from api.adapters.http.billing import billing_router
from api.adapters.social_auth_postgres import PostgresSocialUnitOfWork
from api.adapters.social_auth_runtime import runtime_social_provider, runtime_return_uris
from api.adapters.http.social_auth import social_auth_router
from api.adapters.story_postgres import PostgresStoryUnitOfWork
from api.adapters.story_catalog import load_story_catalog
from api.adapters.http.story import story_router
from api.adapters.local_guidance import LocalGuidance
from api.adapters.openai_guidance import OpenAIGuidance
from api.adapters.owned_references import PostgresOwnedReferences
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.adapters.reading_v2_postgres import PostgresReadingV2UnitOfWork
from api.application.ai_jobs import AIJobsService
from api.application.data_privacy import DataPrivacyUseCases
from api.application.experience import ExperienceUseCases
from api.application.favorites import FavoritesUseCases
from api.application.journal_summary import JournalSummaryUseCases
from api.application.journal_insights import JournalInsightsService
from api.application.identity import IdentityUseCases
from api.application.journal import InactiveOwnerError, JournalUseCases
from api.application.journal_v2 import JournalV2UseCases
from api.application.billing import BillingService
from api.application.social_auth import SocialAuthService
from api.application.story import StoryService
from api.application.reading_v2 import ReadingV2Service
from api.config import ROOT, Settings
from api.domain.ai_jobs import load_policy
from api.domain.data_privacy import DataPolicy
from api.domain.experience import ExperiencePolicy
from api.domain.identity import IdentityPolicy, SessionRejected
from api.domain.journal import JournalPolicy
from api.domain.journal_v2 import JournalV2Policy
from api.domain.billing import Product
from api.domain.social_auth import SocialAuthPolicy

DEFAULT_PROVIDER = object()
logger = logging.getLogger('yeoun')


class SessionResponse(BaseModel):
    token: str


def read_shared(filename):
    return json.loads((ROOT / 'shared' / filename).read_text(encoding='utf-8'))


def create_app(settings=None, *, result_provider=DEFAULT_PROVIDER, transcription_provider=DEFAULT_PROVIDER,
               audio_store=None, workers_enabled=True, social_provider=None, social_return_uris=None):
    settings = settings or Settings.from_env()
    content, rules = read_shared('content.json'), read_shared('journal-policy.json')
    experience_rules, identity_rules = read_shared('experience-policy.json'), read_shared('identity-policy.json')
    data_policy, ai_policy = DataPolicy.from_mapping(read_shared('data-policy.json')), load_policy()
    pool = ConnectionPool(settings.database_url, open=False, min_size=1, max_size=6,
                          kwargs={'row_factory': dict_row})
    ai_client = httpx.Client(timeout=ai_policy['provider_deadline_seconds'],
                            limits=httpx.Limits(max_connections=2), transport=httpx.HTTPTransport(retries=0))
    runtime_key = lambda: os.environ.get('OPENAI_API_KEY')
    if result_provider is DEFAULT_PROVIDER:
        result_provider = OpenAIResultProvider(ai_client, settings.openai_model, runtime_key,
            ai_policy['max_output_tokens'], ai_policy['provider_deadline_seconds'])
    if transcription_provider is DEFAULT_PROVIDER:
        transcription_provider = OpenAITranscriptionProvider(ai_client, settings.transcription_model, runtime_key,
            ai_policy['provider_deadline_seconds'])
    ai_available = bool(result_provider and result_provider.available())

    data = DataPrivacyUseCases(lambda: PostgresDataPrivacyUnitOfWork(pool), data_policy,
        read_shared('data-catalog.json'))
    auth_client = httpx.Client(timeout=15, transport=httpx.HTTPTransport(retries=0), follow_redirects=False)
    auth = SocialAuthService(lambda: PostgresSocialUnitOfWork(pool), social_provider or runtime_social_provider(auth_client),
        SocialAuthPolicy(tuple(social_return_uris) if social_return_uris is not None else runtime_return_uris()))
    data.owner_kind = auth.owner_kind
    story = StoryService(lambda: PostgresStoryUnitOfWork(pool), load_story_catalog())
    identity = IdentityUseCases(lambda: PostgresIdentityUnitOfWork(pool, initialize_profile, owner_guard),
        IdentityPolicy(identity_rules['session_days'], tuple(identity_rules['locales'])),
        lambda: datetime.now(timezone.utc), uuid4)
    store_rules = read_shared('billing-catalog.json')
    store_client = httpx.Client(timeout=10, transport=httpx.HTTPTransport(retries=0))
    billing = BillingService(lambda: PostgresBillingUnitOfWork(pool),
        [Product(**item) for item in store_rules['products']],
        GooglePlayVerifier(store_client, store_rules['package_name'], GoogleApplicationCredentials(),
                           enabled=os.environ.get('BILLING_PLAY_ENABLED') == 'true'),
        FernetReceiptCipher(lambda: os.environ.get('BILLING_RECEIPT_KEY')),
        owner_kind=lambda connection, owner: data.owner_kind(connection, owner) if getattr(data, 'owner_kind', None) else 'guest',
        max_receipts=store_rules['max_receipts_per_owner'], reverify_seconds=store_rules['reverify_seconds'],
        package_name=store_rules['package_name'])
    readings = ReadingV2Service(lambda: PostgresReadingV2UnitOfWork(pool), data,
        owner_guard=owner_guard, allow_candidate=settings.allow_candidate_decks, deck_access=billing.deck_access)
    ai_jobs = AIJobsService(lambda: PostgresAIJobsUnitOfWork(pool), provider=result_provider,
        transcription_provider=transcription_provider, reading_source=readings, owner_guard=owner_guard,
        audio_store=audio_store or PrivateAudioStore(settings.private_audio_dir), policy=ai_policy)
    readings.ai_jobs = ai_jobs
    references = PostgresOwnedReferences(readings, ai_jobs)
    data.feedback_target_exists = references.exists
    journal_v2_policy = JournalV2Policy.from_legacy(content['moods'], rules, read_shared('journal-v2-policy.json'))
    journals_v2 = JournalV2UseCases(lambda: PostgresJournalV2UnitOfWork(pool), journal_v2_policy,
        source_exists=references.exists, invalidate_context=ai_jobs.cancel_for_journal,
        source_lookup=PostgresJournalSourceLookup().resolve)
    summary = JournalSummaryUseCases(lambda: PostgresJournalSummaryUnitOfWork(pool), journal_v2_policy)
    favorites = FavoritesUseCases(lambda: PostgresFavoritesUnitOfWork(pool))
    insights = JournalInsightsService(lambda: PostgresJournalInsightsUnitOfWork(pool), ai_jobs=ai_jobs,
        owner_guard=owner_guard, policy=ai_policy)
    ai_jobs.insight_source = insights
    data.export_callbacks = (ai_jobs.export_owner, readings.export_owner, journals_v2.export_owner,
                             favorites.export_owner, insights.export_owner, billing.export_owner, auth.export_owner, story.export_owner)
    data.erase_callbacks = (readings.erase_owner, ai_jobs.erase_owner, journals_v2.erase_owner,
                            favorites.erase_owner, insights.erase_owner, billing.erase_owner, auth.erase_owner, story.erase_owner)

    policy = JournalPolicy(frozenset(content['moods']), rules['maxBodyLength'])
    journals = JournalUseCases(lambda: PostgresJournalUnitOfWork(pool), policy, uuid4, owner_guard=owner_guard)
    experience_policy = ExperiencePolicy(tuple(content['topics']), tuple(content['cards']),
        experience_rules['spreads'], tuple(content['talkQuestions']),
        experience_rules['maxTarotQuestion'], experience_rules['maxCounselQuestion'])
    legacy_ai = OpenAIGuidance(ai_client, settings.openai_model, runtime_key) if settings.openai_model and runtime_key() else None
    experiences = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pool), experience_policy,
        LocalGuidance(), legacy_ai, experience_rules['aiDailyLimit'], experience_rules['aiGlobalDailyLimit'],
        experience_rules['aiLeaseSeconds'], ai_guard=ai_jobs.legacy_guard(), owner_guard=owner_guard)
    configuration = {**experience_rules, 'topics': content['topics'], 'cards': content['cards'],
                     'questions': content['talkQuestions'], 'aiAvailable': ai_available}

    async def run_loop(stop, operation, interval):
        while not stop.is_set():
            try:
                await asyncio.to_thread(operation)
            except Exception as error:
                logger.error('Background operation failed: %s', type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    def maintain():
        ai_jobs.recover()
        data.process_pending()
        ai_jobs.purge_expired()
        data.purge_expired()
        readings.purge_expired()
        journals_v2.purge_expired_idempotency()
        billing.reconcile()
        auth.purge_expired()

    @asynccontextmanager
    async def lifespan(app):
        stop, tasks = asyncio.Event(), []
        try:
            await asyncio.to_thread(pool.open, wait=True)
            if workers_enabled:
                tasks = [asyncio.create_task(run_loop(stop, lambda: ai_jobs.run_next('api-worker'), settings.worker_poll_seconds)),
                         asyncio.create_task(run_loop(stop, maintain, settings.maintenance_seconds))]
            yield
        finally:
            stop.set()
            if tasks:
                await asyncio.gather(*tasks)
            pool.close()
            ai_client.close()
            store_client.close()
            auth_client.close()

    app = FastAPI(title='여운 API', version='2.0.0', lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
        allow_headers=['Content-Type', 'Authorization', 'X-Idempotency-Key', 'Idempotency-Key', 'X-Data-Receipt'])

    def error_response(request, status, detail):
        if request.url.path.startswith('/v2/'):
            default_codes = {401: 'SESSION_INVALID', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 409: 'CONFLICT',
                             410: 'EXPIRED', 422: 'INVALID_INPUT', 429: 'RATE_LIMITED', 503: 'SERVICE_UNAVAILABLE'}
            error = detail if isinstance(detail, dict) else {'code': default_codes.get(status, 'SERVER_ERROR'),
                'message': detail, 'retryable': status in (429, 500, 502, 503, 504)}
            return JSONResponse({'error': error, 'request_id': str(uuid4())}, status_code=status,
                                headers={'Cache-Control': 'no-store'})
        return JSONResponse({'error': detail if isinstance(detail, str) else detail['message']}, status_code=status)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        return error_response(request, 422, '입력한 날짜와 내용을 확인해 주세요.')

    @app.exception_handler(InactiveOwnerError)
    async def inactive_owner(request, exc):
        return error_response(request, 401, {'code': 'OWNER_UNAVAILABLE',
            'message': '삭제되거나 처리 중인 기록 연결이에요.', 'retryable': False})

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        logger.error('Request failed: %s', type(exc).__name__)
        return error_response(request, 500, '연결이 잠시 원활하지 않아요. 입력한 내용은 그대로 두고 다시 시도해 주세요.')

    def current_session(authorization: Annotated[str | None, Header()] = None):
        try:
            owner = auth.authenticate(authorization)
            return identity.authenticate(authorization) if owner is None else owner
        except SessionRejected as error:
            raise HTTPException(401, {'code': str(error), 'message': '기록 연결을 확인해 주세요. 새 연결로 자동 교체하지 않아요.',
                                      'retryable': False}) from None

    def status_session(authorization: Annotated[str | None, Header()] = None,
                       receipt: Annotated[str | None, Header(alias='X-Data-Receipt')] = None):
        return None if receipt is not None else current_session(authorization)

    def check_database():
        with pool.connection() as connection:
            connection.execute('SELECT 1')

    @app.get('/health', tags=['system'])
    def health():
        check_database()
        return {'ok': True, 'database': 'PostgreSQL', 'service': 'yeoun-fastapi'}

    @app.post('/v1/session', status_code=201, response_model=SessionResponse, tags=['session'])
    def create_session():
        return {'token': identity.create('web', data_policy.default_locale, data_policy.default_timezone).access_token}

    app.include_router(identity_router(identity, current_session,
        session_renewal=lambda authorization, owner: auth.refresh(authorization) or identity.refresh(owner)))
    app.include_router(social_auth_router(auth, current_session))
    app.include_router(story_router(story, current_session))
    app.include_router(bootstrap_router(data, ai_jobs, current_session, check_database,
        limits={'daily_ai_requests': ai_policy['owner_daily_limit'], 'question_max_chars': experience_rules['maxTarotQuestion'],
                'journal_max_chars': policy.max_body_length, 'audio_max_seconds': ai_policy['audio_max_duration_ms'] // 1000,
                'audio_max_bytes': ai_policy['audio_max_bytes']},
        ota_policy={'channel': settings.ota_channel, 'min_native_version': settings.min_native_version,
                    'updates_enabled': settings.ota_updates_enabled}))
    app.include_router(data_privacy_router(data, current_session, status_owner_dependency=status_session))
    app.include_router(create_ai_jobs_router(ai_jobs, current_session))
    app.include_router(create_reading_v2_router(readings, current_session))
    app.include_router(journal_v2_router(journals_v2, current_session))
    app.include_router(journal_summary_router(summary, current_session))
    app.include_router(favorites_router(favorites, current_session))
    app.include_router(create_journal_insights_router(insights, current_session))
    app.include_router(journal_router(journals, policy, current_session, mood_codes=journal_v2_policy.mood_labels))
    app.include_router(billing_router(billing, current_session))
    app.include_router(experience_router(experiences, configuration, current_session))
    return app


app = create_app()
