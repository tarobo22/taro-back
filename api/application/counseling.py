"""Acknowledge -> clarify/confirm -> grounded tarot/advice -> follow-up."""
from datetime import datetime,timezone
import time
from uuid import uuid4

from api.application.counseling_ports import CounselingModel,CounselingStore
from api.domain.ai_jobs import fingerprint,load_policy,safety_result
from api.domain.counseling import (Analysis,CounselingError,draw_cards,grounded_result,
    personal_definitions,retrieve_definitions,search_queries,urgent_risk)


def public_definition(row):
    return {k:str(row[k]) if k=='id' else row[k] for k in ('id','term','definition','version')}


def memory_revision(rows):return fingerprint(sorted((str(r['id']),r['version']) for r in rows))


def bounded_history(history):
    result=[];budget=6000
    for item in reversed(history[-10:]):
        if len(item['text'])>budget:break
        result.insert(0,item);budget-=len(item['text'])
    return result


class CounselingService:
    def __init__(self,store:CounselingStore,model:CounselingModel,cards,policy=None,sample=None):
        self.store,self.model,self.cards=store,model,cards
        self.policy={**load_policy(),**(policy or {})};self.sample=sample

    def _session(self,r,owner,sid):
        row=r.session(owner,sid)
        if not row:raise CounselingError('NOT_FOUND',404,'이야기를 찾지 못했어요.')
        return row

    def _view(self,r,row):
        state=row['state'];job=r.latest_command(row['owner_id'],row['id']);a=state.get('analysis') or {}
        return {'id':str(row['id']),'topic':row['topic'],'version':row['version'],
            'phase':state.get('phase','listening'),'understanding':a.get('understanding'),
            'acknowledgement':a.get('acknowledgement',''),'perspective':a.get('perspective'),
            'response_mode':a.get('response_mode','advice'),'clarification_question':a.get('clarification_question'),
            'search_queries':state.get('search_queries',[]),'search_status':state.get('search_status','not_requested'),
            'cards':state.get('cards',[]),'draw':state.get('draw'),'result':state.get('result'),
            'sources':[{k:s.get(k) for k in ('id','kind','title','url','retrieved_at')} for s in state.get('sources',[])],
            'glossary_used':state.get('glossary_used',[]),'history':state.get('history',[]),
            'job':{'id':str(job['id']),'state':job['state'],'error_code':job['error_code']} if job else None}

    def create(self,owner,key,topic='care'):
        if topic not in ('relationship','work','self','daily','care','start'):raise CounselingError('INVALID_TOPIC')
        with self.store.transaction(owner) as r:
            row=r.session_for_key(owner,key)
            if row and row['topic']!=topic:raise CounselingError('IDEMPOTENCY_CONFLICT',409)
            row=row or r.create_session(owner,uuid4(),topic,{'phase':'listening','history':[]},key)
            return self._view(r,row)

    def get(self,owner,sid):
        with self.store.transaction(owner) as r:return self._view(r,self._session(r,owner,sid))

    def list(self,owner):
        with self.store.transaction(owner) as r:
            return [{'id':str(v['id']),'topic':v['topic'],'phase':v['state'].get('phase','listening'),
                'summary':(v['state'].get('analysis') or {}).get('understanding','새 이야기'),
                'updated_at':v['updated_at'].isoformat()} for v in r.sessions(owner)]

    def delete(self,owner,sid):
        with self.store.transaction(owner) as r:
            self._session(r,owner,sid);r.delete_session(owner,sid)

    def glossary(self,owner):
        with self.store.transaction(owner) as r:return [public_definition(d) for d in r.definitions(owner)]

    def define(self,owner,term,definition):
        if not isinstance(term,str) or not 1<=len(term.strip())<=60 or not isinstance(definition,str) or not 1<=len(definition.strip())<=400:
            raise CounselingError('INVALID_DEFINITION')
        with self.store.transaction(owner) as r:
            return public_definition(r.save_definition(owner,{'term':term,'definition':definition}))

    def forget(self,owner,definition_id):
        with self.store.transaction(owner) as r:
            definition=next((d for d in r.definitions(owner) if str(d['id'])==str(definition_id)),None)
            if not definition:raise CounselingError('NOT_FOUND',404)
            r.delete_definition(owner,definition_id)
            for row in r.sessions(owner):
                state=row['state'];term=definition['term']
                if any(d['term']==term for d in state.get('glossary_used',[])) or any(term in h['text'] for h in state.get('history',[])):
                    r.update_session(owner,row['id'],{'phase':'listening','history':[h for h in state.get('history',[]) if term not in h['text']]})

    def enqueue(self,owner,sid,key,kind,payload):
        if kind not in ('message','confirm'):raise CounselingError('INVALID_COMMAND')
        if kind=='message' and (not isinstance(payload.get('text'),str) or not 1<=len(payload['text'].strip())<=2000):
            raise CounselingError('INVALID_MESSAGE',422,'하고 싶은 이야기를 적어주세요.')
        if kind=='confirm' and payload.get('confirmed') is not True:
            raise CounselingError('CONFIRMATION_REQUIRED',422,'이해한 내용이 맞는지 먼저 확인해 주세요.')
        if kind=='confirm' and payload.get('card_count',3) not in (1,3):raise CounselingError('INVALID_CARD_COUNT')
        mark=fingerprint([str(sid),kind,payload])
        with self.store.transaction(owner) as r:
            old=r.command(owner,key)
            if old:
                if old['fingerprint']!=mark:raise CounselingError('IDEMPOTENCY_CONFLICT',409)
                return self._view(r,self._session(r,owner,sid))
            row=self._session(r,owner,sid)
            if row['version']!=payload.get('expected_version'):
                raise CounselingError('VERSION_CONFLICT',409,'이야기가 바뀌었어요. 최신 내용을 확인해 주세요.')
            r.consent(owner,payload.get('consent_event_id'),self.policy['policy_version'])
            if (not self.model or not self.model.available()) and not (kind=='message' and urgent_risk(payload.get('text',''))):
                raise CounselingError('AI_UNAVAILABLE',503,'AI 연결이 아직 준비되지 않았어요. 입력한 이야기는 그대로 두세요.')
            if kind=='confirm':
                if row['state'].get('phase')!='confirmation':raise CounselingError('CONFIRMATION_REQUIRED',409)
                if row['state'].get('memory_revision')!=memory_revision(r.definitions(owner)):
                    raise CounselingError('GLOSSARY_CHANGED',409,'사전이 바뀌었어요. 이야기를 다시 보내 의미를 확인해 주세요.')
            command_id=uuid4();r.reserve(owner,command_id,self.policy)
            r.enqueue(owner,sid,key,mark,kind,payload,payload['consent_event_id'],command_id)
            return self._view(r,row)

    def _check(self,r,command):
        row=self._session(r,command['owner_id'],command['session_id'])
        latest=r.latest_command(command['owner_id'],command['session_id'])
        if not latest or latest['id']!=command['id'] or latest['state']!='running':raise CounselingError('COMMAND_EXPIRED',409)
        r.consent(command['owner_id'],command['consent_event_id'],self.policy['policy_version'])
        return row

    def _guard(self,command,snapshot):
        with self.store.transaction(command['owner_id']) as r:
            self._check(r,command)
            if snapshot!=memory_revision(r.definitions(command['owner_id'])):raise CounselingError('GLOSSARY_CHANGED',409)

    @staticmethod
    def _support(history,text):
        s=safety_result('chat',[])
        return {'phase':'support','history':history+[{'role':'user','text':text}],
            'result':{'empathy':s['empathy'],'facts':[],'tarot':[],'advice':s['advice'],'actions':s['actions'],
                'follow_up_question':s['follow_up_questions'][0],'safety_mode':'support'}}

    def run_next(self):
        meta=self.store.claim()
        if not meta:return False
        owner,sid=meta['owner_id'],meta['session_id'];started=time.monotonic()
        try:
            with self.store.transaction(owner) as r:
                command=r.claimed(meta['id'])
                if not command:return True
                row=self._check(r,command);state=dict(row['state']);definitions=r.definitions(owner)
                snapshot=memory_revision(definitions)
                if command['kind']=='confirm' and state.get('memory_revision')!=snapshot:raise CounselingError('GLOSSARY_CHANGED',409)
                mode=(state.get('analysis') or {}).get('response_mode','advice')
                if command['kind']=='confirm' and mode!='listen' and not state.get('cards'):
                    state['cards']=draw_cards(self.cards,command['payload'].get('card_count',3),sample=self.sample)
                    state['draw']={'id':str(uuid4()),'method':'python.SystemRandom.sample',
                        'at':datetime.now(timezone.utc).isoformat(),'without_replacement':True}
                    r.update_session(owner,sid,state)
            if command['kind']=='message':
                text=command['payload']['text'].strip();history=bounded_history(state.get('history',[]))
                temporary=[{'id':'session:'+str(i),'version':1,**d} for i,d in enumerate(state.get('session_definitions',[]))]
                known=[*definitions,*temporary];retrieved=retrieve_definitions(text,known)
                if urgent_risk(text):state=self._support(history,text);proposals=[]
                else:
                    self._guard(command,snapshot)
                    a=Analysis.model_validate(self.model.analyze({'text':text,'history':history,
                        'dictionary':[public_definition(d) for d in retrieved]}))
                    if a.intent=='general_information':
                        a.search_purpose='none';a.explicit_definitions=[];a.unknown_terms=[]
                        if not a.clarification_question:
                            a.clarification_question='그 일과 관련해서 어떤 마음이나 고민이 있으셨는지 조금 들려주실래요?'
                    proposals=personal_definitions(a,text)
                    unresolved=[t for t in a.unknown_terms if t.kind!='public' and not retrieve_definitions(t.term,[*known,*proposals])]
                    if unresolved and not a.clarification_question:
                        a.clarification_question=f'괜찮으시면, 말씀하신 “{unresolved[0].term}”가 어떤 뜻인지 알려주실래요?'
                    question=a.clarification_question or ('' if a.understanding.rstrip().endswith(('?','？')) else '제가 이해한 내용이 맞을까요?')
                    temporary_only=a.memory_scope=='conversation' or not command['payload'].get('remember_definitions',True)
                    state={'phase':'clarifying' if a.clarification_question else 'confirmation',
                        'analysis':a.model_dump(),'search_queries':search_queries(a,[*known,*proposals])[:1] if a.search_purpose=='clarify_counseling_term' and a.intent=='tarot_counseling' else [],
                        'session_definitions':[*state.get('session_definitions',[]),*(proposals if temporary_only else [])][-20:],
                        'glossary_used':[public_definition(d) for d in retrieved],
                        'evidence_cache':state.get('evidence_cache',[]),
                        'history':bounded_history(history+[{'role':'user','text':text},
                            {'role':'assistant','text':a.acknowledgement+' '+a.understanding+' '+question}])}
                    if temporary_only:proposals=[]
                    if a.needs_immediate_support:state=self._support(history,text);proposals=[]
            else:
                temporary=[{'id':'session:'+str(i),'version':1,**d} for i,d in enumerate(state.get('session_definitions',[]))]
                sources=[{'id':'personal:'+str(d['id']),'kind':'user_definition','title':d['term']+' · 말씀해주신 의미',
                    'url':None,'text':d['definition']} for d in retrieve_definitions(
                        ' '.join(v['text'] for v in state.get('history',[])),[*definitions,*temporary])]
                search_status='not_requested';cache=list(state.get('evidence_cache',[]));cache_updated=False
                if mode!='listen' and command['payload'].get('web_search') and state.get('search_queries'):
                    search_status='completed'
                    for query in state['search_queries']:
                        self._guard(command,snapshot)
                        hit=next((c for c in cache if c['query']==query and time.time()-c['at']<600),None)
                        if hit:sources.extend(hit['sources']);continue
                        try:
                            found=self.model.search(query);now=datetime.now(timezone.utc).isoformat()
                            found=[{**s,'retrieved_at':now} for s in found]
                            sources.extend(found);cache.append({'query':query,'at':time.time(),'sources':found});cache_updated=True
                        except CounselingError as e:
                            if e.code in ('AI_UNAVAILABLE','AI_RATE_LIMITED','AI_PROVIDER_ERROR','UNKNOWN_PROVIDER_OUTCOME','AI_OUTPUT_INVALID'):search_status='unavailable'
                            else:raise
                    if not any(s['kind']=='web' for s in sources) and search_status=='completed':search_status='no_sources'
                cards=state.get('cards',[]) if mode!='listen' else []
                sources.extend({'id':'tarot:'+str(c['card_id']),'kind':'tarot_reference','title':c['card'].get('name_ko','타로 카드'),
                    'url':(c['card'].get('image_asset') or {}).get('source_page'),
                    'text':c['card'].get('basic_meaning','생각을 돌아보는 상징이에요.')} for c in cards)
                sources=list({s['id']:s for s in sources}.values())
                state={**state,'evidence_cache':cache[-12:],'cards':cards}
                if cache_updated:
                    # Preserve only scoped evidence before generation: a reply retry must not buy the same search again.
                    with self.store.transaction(owner) as r:
                        self._check(r,command)
                        if snapshot!=memory_revision(r.definitions(owner)):raise CounselingError('GLOSSARY_CHANGED',409)
                        r.update_session(owner,sid,state)
                self._guard(command,snapshot)
                output=self.model.compose({'understanding':state['analysis']['understanding'],
                    'perspective':state['analysis'].get('perspective'),'response_mode':mode,
                    'history':bounded_history(state.get('history',[])),'cards':cards,'sources':sources,'search_status':search_status})
                result=grounded_result(output,cards,sources,mode)
                state={**state,'phase':'advice','sources':sources,'result':result,'search_status':search_status,
                    'history':bounded_history(state.get('history',[])+[{'role':'assistant','text':result['empathy']+' '+result['advice']+' '+' '.join(result['actions'])}])}
                proposals=[]
            with self.store.transaction(owner) as r:
                self._check(r,command)
                if snapshot!=memory_revision(r.definitions(owner)):raise CounselingError('GLOSSARY_CHANGED',409)
                for p in proposals:r.save_definition(owner,p,command['id'])
                state['memory_revision']=memory_revision(r.definitions(owner))
                r.update_session(owner,sid,state);r.finish(command['id'],timings={'run_ms':round((time.monotonic()-started)*1000)})
        except Exception as error:
            code=error.code if isinstance(error,CounselingError) else 'AI_OUTPUT_INVALID'
            with self.store.transaction(owner,check_active=False) as r:r.finish(meta['id'],code)
        return True
