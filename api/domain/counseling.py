"""Counseling rules. Personal meanings, public evidence and tarot stay separate."""
import random
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.domain.ai_jobs import AIError, validate_result


class CounselingError(ValueError):
    def __init__(self, code, status=422, message=None):
        super().__init__(code)
        self.code,self.status,self.message=code,status,message or code


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')


class DefinitionProposal(StrictModel):
    term:str=Field(min_length=1,max_length=60)
    definition:str=Field(min_length=1,max_length=400)
    evidence_quote:str=Field(min_length=1,max_length=600)


class UnknownTerm(StrictModel):
    term:str=Field(min_length=1,max_length=80)
    kind:Literal['public','personal','unclear']


class Analysis(StrictModel):
    understanding:str=Field(min_length=1,max_length=700)
    clarification_question:str|None=Field(max_length=300)
    explicit_definitions:list[DefinitionProposal]=Field(max_length=5)
    unknown_terms:list[UnknownTerm]=Field(max_length=5)


class GroundedClaim(StrictModel):
    text:str=Field(min_length=1,max_length=600)
    source_ids:list[str]=Field(min_length=1,max_length=4)


class Reflection(StrictModel):
    card_id:int=Field(ge=0,le=77)
    reflection:str=Field(min_length=1,max_length=400)


class CounselResult(StrictModel):
    empathy:str=Field(min_length=1,max_length=300)
    facts:list[GroundedClaim]=Field(max_length=5)
    tarot:list[Reflection]=Field(max_length=3)
    advice:str=Field(min_length=1,max_length=800)
    actions:list[str]=Field(min_length=1,max_length=3)
    follow_up_question:str|None=Field(max_length=200)
    safety_mode:Literal['standard','support']


def normalized(text):
    return unicodedata.normalize('NFKC',text).strip().casefold()


def draw_cards(cards,count=3,sample=None):
    ids=[c['id'] for c in cards]
    if len(ids)!=78 or set(ids)!=set(range(78)) or count not in (1,3):
        raise CounselingError('DECK_INVALID',503)
    drawn=(sample or random.SystemRandom().sample)(ids,count)
    if len(drawn)!=count or len(set(drawn))!=count or not set(drawn)<=set(ids):
        raise CounselingError('DRAW_INVALID',503)
    catalog={c['id']:c for c in cards}
    labels=('지금의 마음','살펴볼 관점','다음 한 걸음')
    return [{'card_id':i,'position':p+1,'position_label':labels[p],
             'orientation':'upright','card':catalog[i]} for p,i in enumerate(drawn)]


def personal_definitions(analysis,text):
    # Model suggestions never become dictionary entries without a matching user quote.
    result=[]
    for p in analysis.explicit_definitions:
        if p.evidence_quote in text and p.term in p.evidence_quote and p.definition in p.evidence_quote:
            result.append(p.model_dump())
    return result


def retrieve_definitions(text,entries,limit=8):
    query=normalized(text)
    matches=[v for v in entries if normalized(v['term']) in query]
    return sorted(matches,key=lambda v:len(normalized(v['term'])),reverse=True)[:limit]


def search_queries(analysis,definitions):
    known={normalized(v['term']) for v in definitions};queries=[]
    for item in analysis.unknown_terms:
        term=item.term.strip()
        if item.kind!='public' or normalized(term) in known:
            continue
        if re.search(r'@|https?://|\d{2,3}[ -]?\d{3,4}[ -]?\d{4}|[\r\n]',term):
            continue
        if not re.fullmatch(r'[\w가-힣 ·+()/.-]{1,80}',term):
            continue
        query=term+' 공식 자료'
        if query not in queries:queries.append(query)
    return queries[:2]


def grounded_result(value,cards,sources):
    try:
        result=CounselResult.model_validate(value).model_dump()
        if [v['card_id'] for v in result['tarot']] != [v['card_id'] for v in cards]:
            raise ValueError()
        factual={v['id'] for v in sources if v['kind'] in ('web','user_definition')}
        if any(not set(v['source_ids'])<=factual for v in result['facts']):raise ValueError()
        # Reuse the established output policy for diagnosis/prediction/guarantees.
        validate_result({'kind':'tarot','empathy':result['empathy'],
            'summary':' '.join(v['text'] for v in result['facts']) or '지금의 마음을 함께 살펴봐요.',
            'card_insights':[{'card_id':c['card_id'],'position':c['position'],
                'meaning':c['card'].get('basic_meaning','생각을 돌아보는 상징이에요.'),
                'reflection':r['reflection']} for c,r in zip(cards,result['tarot'])],
            'advice':result['advice'],'actions':result['actions'],'expression':'listen',
            'follow_up_questions':[result['follow_up_question']] if result['follow_up_question'] else [],
            'safety_mode':result['safety_mode']},'tarot',cards)
        return result
    except (ValidationError,ValueError,KeyError,TypeError,AIError):
        raise CounselingError('OUTPUT_INVALID',502,'답변의 근거를 확인하지 못했어요. 같은 카드로 다시 요청할 수 있어요.') from None
