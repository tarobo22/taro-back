from typing import Annotated,Literal
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt

from api.adapters.http.ai_jobs import idempotency_key
from api.adapters.http.reading_v2 import SelectedCard as ReadingSelectedCard
from api.domain.counseling import CounselingError,CounselResult


class Input(BaseModel):model_config=ConfigDict(extra='forbid')


class SessionInput(Input):
    topic:Literal['relationship','work','self','daily','care','start']='care'
    source_reading_id:UUID|None=None


class SelectedCard(ReadingSelectedCard):
    position:int=Field(ge=1,le=5)


class MessageInput(Input):
    text:str=Field(min_length=1,max_length=2000)
    expected_version:StrictInt=Field(ge=1)
    consent_event_id:UUID
    remember_definitions:StrictBool=True


class ConfirmationInput(Input):
    confirmed:StrictBool
    expected_version:StrictInt=Field(ge=1)
    consent_event_id:UUID
    web_search:StrictBool=False
    card_count:Literal[1,3,5]=3


class DefinitionInput(Input):
    term:str=Field(min_length=1,max_length=60)
    definition:str=Field(min_length=1,max_length=400)


class DefinitionView(Input):
    id:str
    term:str
    definition:str
    version:int


class SourceView(Input):
    id:str
    kind:Literal['web','user_definition','tarot_reference']
    title:str
    url:str|None
    retrieved_at:str|None


class DrawView(Input):
    id:UUID
    method:str
    at:str
    without_replacement:bool


class CommandView(Input):
    id:UUID
    state:Literal['queued','running','succeeded','failed']
    error_code:str|None


class HistoryItem(Input):
    role:Literal['user','assistant']
    text:str


class SessionView(Input):
    id:UUID
    topic:str
    version:int
    phase:Literal['listening','clarifying','confirmation','advice','support']
    understanding:str|None
    acknowledgement:str
    perspective:str|None
    response_mode:Literal['advice','listen']
    clarification_question:str|None
    search_queries:list[str]
    search_status:str
    cards:list[SelectedCard]
    draw:DrawView|None
    result:CounselResult|None
    sources:list[SourceView]
    glossary_used:list[DefinitionView]
    history:list[HistoryItem]
    job:CommandView|None



class PrivateRoute(APIRoute):
    def get_route_handler(self):
        endpoint = super().get_route_handler()
        async def no_cache(request):
            response = await endpoint(request)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return no_cache


def create_counseling_router(service,owner_dependency):
    router = APIRouter(prefix='/v2/counseling', tags=['counseling'], route_class=PrivateRoute)
    Owner = Annotated[UUID, Depends(owner_dependency)]
    Key = Annotated[UUID, Depends(idempotency_key)]

    def call(operation):
        try:
            return operation()
        except CounselingError as error:
            raise HTTPException(error.status, dict(code=error.code, message=error.message,
                retryable=error.status in (429, 502, 503))) from None

    @router.get('/capabilities')
    def capabilities():
        return dict(available=bool(service.model and service.model.available()),
            policy_version=service.policy['policy_version'], card_counts=[1, 3, 5],
            glossary_limit=200, max_message_chars=2000)

    @router.post('/sessions', status_code=201, response_model=SessionView)
    def create(body:SessionInput, owner:Owner, key:Key):
        return call(lambda: service.create(owner, key, body.topic, body.source_reading_id))

    @router.get('/sessions')
    def sessions(owner:Owner):
        return {'items': call(lambda: service.list(owner))}

    @router.get('/sessions/{session_id}', response_model=SessionView)
    def session(session_id:UUID, owner:Owner):
        return call(lambda: service.get(owner, session_id))

    @router.delete('/sessions/{session_id}', status_code=204)
    def delete(session_id:UUID, owner:Owner):
        call(lambda: service.delete(owner, session_id))

    @router.post('/sessions/{session_id}/messages', status_code=202, response_model=SessionView)
    def message(session_id:UUID, body:MessageInput, owner:Owner, key:Key):
        return call(lambda: service.enqueue(owner, session_id, key, 'message', body.model_dump(mode='json')))

    @router.post('/sessions/{session_id}/confirmation', status_code=202, response_model=SessionView)
    def confirm(session_id:UUID, body:ConfirmationInput, owner:Owner, key:Key):
        return call(lambda: service.enqueue(owner, session_id, key, 'confirm', body.model_dump(mode='json')))

    @router.get('/glossary')
    def glossary(owner:Owner):
        return {'items': call(lambda: service.glossary(owner))}

    @router.put('/glossary', response_model=DefinitionView)
    def define(body:DefinitionInput, owner:Owner):
        return call(lambda: service.define(owner, body.term, body.definition))

    @router.delete('/glossary/{definition_id}', status_code=204)
    def forget(definition_id:UUID, owner:Owner):
        call(lambda: service.forget(owner, definition_id))

    return router
