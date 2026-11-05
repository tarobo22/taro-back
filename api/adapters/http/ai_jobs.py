"""P0 v2 HTTP contracts. SSE exposes committed status only, never model fragments."""
import asyncio
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from api.domain.ai_jobs import AIError, AIResult, ChoiceAnswer


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ConsentInput(InputModel):
    kind: Literal['ai_processing', 'journal_context', 'cloud_transcription']
    policy_version: str = Field(min_length=1, max_length=100)
    granted: StrictBool


class ConsentEvent(InputModel):
    id: UUID
    kind: str
    policy_version: str
    granted: bool
    occurred_at: str


class ConsentList(InputModel):
    items: list[ConsentEvent]
    next_cursor: str | None


class ConversationInput(InputModel):
    topic: Literal['relationship', 'work', 'self', 'daily', 'care', 'start']
    tone: Literal['warm', 'giga']


class Conversation(ConversationInput):
    id: UUID
    state: Literal['active', 'closed']
    created_at: str
    updated_at: str


class ConversationList(InputModel):
    items: list[Conversation]
    next_cursor: str | None


class TurnInput(InputModel):
    client_message_id: UUID
    input_source: Literal['text', 'choice', 'voice']
    text: str = Field(default='', max_length=2000)
    answers: list[ChoiceAnswer] = Field(default_factory=list, max_length=3)
    context_entry_ids: list[UUID] = Field(default_factory=list, max_length=3)
    consent_event_id: UUID | None = None
    use_ai: StrictBool = True


class InterpretationInput(InputModel):
    expected_version: StrictInt = Field(ge=1)
    context_entry_ids: list[UUID] = Field(default_factory=list, max_length=3)
    consent_event_id: UUID


class Turn(InputModel):
    id: UUID
    conversation_id: UUID
    turn_index: int
    text: str
    answers: list[ChoiceAnswer]
    input_source: str
    state: Literal['accepted', 'generating', 'ready', 'failed', 'cancelled']
    assistant_result: AIResult | None
    version: int
    created_at: str


class TurnList(InputModel):
    items: list[Turn]
    next_cursor: str | None


class ErrorDetail(InputModel):
    code: str
    message: str
    retryable: bool


class Job(InputModel):
    id: UUID
    kind: Literal['tarot', 'chat', 'transcription']
    state: Literal['queued', 'running', 'cancel_requested', 'succeeded', 'failed', 'cancelled']
    version: int
    resource_id: UUID
    result_url: str | None
    error: ErrorDetail | None
    created_at: str
    finished_at: str | None


class JobAccepted(InputModel):
    job_id: UUID
    resource_id: UUID
    state: str
    status_url: str
    events_url: str


class Transcription(InputModel):
    id: UUID
    state: Literal['queued', 'running', 'ready', 'failed', 'deleted']
    transcript: str | None
    expires_at: str


def call(operation):
    try:
        return operation()
    except AIError as error:
        raise HTTPException(error.status, {'code': error.code, 'message': error.message, 'retryable': error.retryable}) from None


def idempotency_key(primary: Annotated[UUID | None, Header(alias='X-Idempotency-Key')] = None,
                    compatibility: Annotated[UUID | None, Header(alias='Idempotency-Key')] = None):
    if primary and compatibility and primary != compatibility:
        raise HTTPException(422, {'code': 'INVALID_IDEMPOTENCY_KEY', 'message': '요청 키를 확인해 주세요.', 'retryable': False})
    if not (primary or compatibility):
        raise HTTPException(422, {'code': 'IDEMPOTENCY_KEY_REQUIRED', 'message': '요청 키가 필요해요.', 'retryable': False})
    return primary or compatibility


def create_ai_jobs_router(service, owner_dependency):
    router = APIRouter(prefix='/v2', tags=['ai'])
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Key = Annotated[UUID, Depends(idempotency_key)]
    Limit = Annotated[int, Query(ge=1, le=50)]

    @router.get('/me/consents', response_model=ConsentList)
    def consents(owner: Owner, limit: Limit = 20, cursor: str | None = None):
        return call(lambda: service.list_consents(owner, limit, cursor))

    @router.post('/me/consents', status_code=201, response_model=ConsentEvent)
    def record(body: ConsentInput, owner: Owner, key: Key):
        return call(lambda: service.record_consent(owner, key, body.kind, body.policy_version, body.granted))

    @router.post('/conversations', status_code=201, response_model=Conversation)
    def create(body: ConversationInput, owner: Owner, key: Key):
        return call(lambda: service.create_conversation(owner, key, body.topic, body.tone))

    @router.get('/conversations', response_model=ConversationList)
    def conversations(owner: Owner, limit: Limit = 20, cursor: str | None = None):
        return call(lambda: service.list_conversations(owner, limit, cursor))

    @router.get('/conversations/{conversation_id}', response_model=Conversation)
    def conversation(conversation_id: UUID, owner: Owner):
        return call(lambda: service.get_conversation(owner, conversation_id))

    @router.post('/conversations/{conversation_id}/close', response_model=Conversation)
    def close(conversation_id: UUID, owner: Owner, key: Key):
        return call(lambda: service.close_conversation(owner, conversation_id, key))

    @router.delete('/conversations/{conversation_id}', status_code=204)
    def delete(conversation_id: UUID, owner: Owner):
        call(lambda: service.delete_conversation(owner, conversation_id))

    @router.post('/conversations/{conversation_id}/turns', status_code=202, response_model=JobAccepted)
    def create_turn(conversation_id: UUID, body: TurnInput, owner: Owner, key: Key):
        return call(lambda: service.create_turn(owner, conversation_id, key, body.model_dump(mode='json')))

    @router.get('/conversations/{conversation_id}/turns', response_model=TurnList)
    def turns(conversation_id: UUID, owner: Owner, limit: Limit = 20, cursor: str | None = None):
        return call(lambda: service.list_turns(owner, conversation_id, limit, cursor))

    @router.get('/conversations/{conversation_id}/turns/{turn_id}', response_model=Turn)
    def turn(conversation_id: UUID, turn_id: UUID, owner: Owner):
        return call(lambda: service.get_turn(owner, conversation_id, turn_id))

    @router.post('/readings/{reading_id}/interpretations', status_code=202, response_model=JobAccepted)
    def interpretation(reading_id: UUID, body: InterpretationInput, owner: Owner, key: Key):
        return call(lambda: service.create_interpretation(owner, reading_id, key, body.model_dump(mode='json')))

    @router.get('/ai/jobs/{job_id}', response_model=Job)
    def job(job_id: UUID, owner: Owner):
        return call(lambda: service.get_job(owner, job_id))

    @router.post('/ai/jobs/{job_id}/cancel', status_code=202, response_model=Job)
    def cancel(job_id: UUID, owner: Owner, key: Key):
        return call(lambda: service.cancel_job(owner, job_id, key))

    @router.post('/ai/jobs/{job_id}/retry', status_code=202, response_model=JobAccepted)
    def retry(job_id: UUID, owner: Owner, key: Key):
        return call(lambda: service.retry_job(owner, job_id, key))

    @router.get('/ai/jobs/{job_id}/events', response_class=StreamingResponse)
    def events(job_id: UUID, owner: Owner):
        first = call(lambda: service.get_job(owner, job_id))
        async def stream():
            current, previous = first, None
            while True:
                if previous != current['version']:
                    event = 'completed' if current['state'] == 'succeeded' else 'failed' if current['state'] in ('failed', 'cancelled') else 'status'
                    yield f'event: {event}\ndata: {json.dumps(current, ensure_ascii=False)}\n\n'
                    previous = current['version']
                else:
                    yield 'event: heartbeat\ndata: {}\n\n'
                if current['state'] in ('succeeded', 'failed', 'cancelled'):
                    return
                await asyncio.sleep(1)
                try:
                    current = await asyncio.to_thread(service.get_job, owner, job_id)
                except AIError:
                    yield 'event: failed\ndata: {"state":"cancelled"}\n\n'
                    return
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @router.post('/transcriptions', status_code=202, response_model=JobAccepted)
    async def transcribe(owner: Owner, key: Key, audio: Annotated[UploadFile, File()],
                         duration_ms: Annotated[int, Form(ge=1, le=60000)],
                         consent_event_id: Annotated[UUID, Form()]):
        try:
            content = await audio.read(service.policy['audio_max_bytes'] + 1)
            return await asyncio.to_thread(call, lambda: service.create_transcription(owner, key, content, audio.content_type or '', duration_ms, consent_event_id))
        finally:
            await audio.close()

    @router.get('/transcriptions/{transcription_id}', response_model=Transcription)
    def transcription(transcription_id: UUID, owner: Owner):
        return call(lambda: service.get_transcription(owner, transcription_id))

    @router.delete('/transcriptions/{transcription_id}', status_code=204)
    def delete_transcription(transcription_id: UUID, owner: Owner):
        call(lambda: service.delete_transcription(owner, transcription_id))

    return router
