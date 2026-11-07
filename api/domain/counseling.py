"""Counseling rules. Personal meanings, public evidence and tarot stay separate."""
import random
import re
import unicodedata
import ipaddress
from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.domain.ai_jobs import AIError, AIResult, CardInsight, validate_result


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
    acknowledgement:str=Field(default='',max_length=100)
    understanding:str=Field(min_length=1,max_length=240)
    clarification_question:str|None=Field(max_length=160)
    explicit_definitions:list[DefinitionProposal]=Field(max_length=5)
    unknown_terms:list[UnknownTerm]=Field(max_length=5)
    perspective:str|None=Field(default=None,max_length=100)
    goal:str|None=Field(default=None,max_length=200)
    response_mode:Literal['advice','listen']='advice'
    memory_scope:Literal['personal','conversation']='personal'
    needs_immediate_support:bool=False
    intent:Literal['tarot_counseling','general_information']='tarot_counseling'
    search_purpose:Literal['none','clarify_counseling_term']='none'


class GroundedClaim(StrictModel):
    text:str=Field(min_length=1,max_length=180)
    source_ids:list[str]=Field(min_length=1,max_length=4)


class Reflection(StrictModel):
    card_id:int=Field(ge=0,le=77)
    reflection:str=Field(min_length=1,max_length=400)


class CounselResult(StrictModel):
    empathy:str=Field(min_length=1,max_length=160)
    facts:list[GroundedClaim]=Field(max_length=1)
    tarot:list[Reflection]=Field(max_length=5)
    advice:str=Field(min_length=1,max_length=300)
    actions:list[str]=Field(max_length=3)
    follow_up_question:str|None=Field(max_length=200)
    safety_mode:Literal['standard','support']


class ShortReflection(Reflection):
    reflection:str=Field(min_length=1,max_length=60)


class FollowUpCounselResult(CounselResult):
    tarot:list[ShortReflection]=Field(max_length=5)
    advice:str=Field(min_length=1,max_length=180)


class CounselingCardInsight(CardInsight):
    position:int=Field(strict=True,ge=1,le=5)


class CounselingResultCheck(AIResult):
    card_insights:list[CounselingCardInsight]=Field(max_length=5)


def normalized(text):
    return unicodedata.normalize('NFKC', text).casefold().strip()

def draw_cards(cards,count=3,sample=None):
    catalog = {card['id']: card for card in cards}
    if (type(count) is not int or count not in (1, 3, 5) or len(cards) != 78
            or set(catalog) != set(range(78))
            or any(type(card['id']) is not int for card in cards)):
        raise CounselingError('DECK_INVALID', 503)
    selected = list((sample or random.SystemRandom().sample)(list(catalog), count))
    if (len(selected) != count or len(set(selected)) != count
            or any(type(item) is not int or item not in catalog for item in selected)):
        raise CounselingError('DRAW_INVALID', 503)
    labels = (('지금의 마음', '마음에 걸리는 것', '도움이 되는 것', '살펴볼 관점', '다음 한 걸음')
              if count == 5 else ('지금의 마음', '살펴볼 관점', '다음 한 걸음'))
    return [dict(card_id=card_id, position=index + 1, position_label=labels[index],
                 orientation='upright', card=catalog[card_id])
            for index, card_id in enumerate(selected)]

def personal_definitions(analysis,text):
    proposals = []
    for item in analysis.explicit_definitions:
        quote = item.evidence_quote
        if quote in text and item.term in quote and item.definition in quote:
            proposals.append(item.model_dump())
    return proposals

def retrieve_definitions(text,entries,limit=8):
    query = normalized(text)
    candidates = [entry for entry in entries
                  if normalized(entry['term']) and normalized(entry['term']) in query]
    return sorted(candidates, key=lambda item: len(normalized(item['term'])), reverse=True)[:max(0, limit)]

def search_queries(analysis,definitions):
    known = {normalized(item['term']) for item in definitions}
    queries = []
    for item in analysis.unknown_terms:
        term = item.term.strip()
        if item.kind != 'public' or normalized(term) in known:
            continue
        if re.search(r'@|https?://|\d{2,3}[ -]?\d{3,4}[ -]?\d{4}|[\r\n]', term):
            continue
        if re.fullmatch(r'[\w가-힣 ·+()/.-]{1,80}', term):
            candidate = term + ' 공식 자료'
            if candidate not in queries:
                queries.append(candidate)
    return queries[:2]

def safe_public_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'\s', value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return '.' in host and not host.endswith(('.local', '.localhost', '.internal'))
    except ValueError:
        return False

def urgent_risk(text):
    explicit = r'죽고\s*싶|나를\s*해(?:치|칠)|(?:내\s*몸|스스로)(?:을|를)?\s*(?:다치|다칠|해치|해칠)|자해(?:할|하고\s*싶)|자살(?:할|하고\s*싶)'
    return re.search(explicit, text) is not None

def strict_schema(model):
    schema = model.model_json_schema()
    def require_fields(node):
        if isinstance(node, dict):
            node.pop('default', None)
            if node.get('type') == 'object':
                node.update(additionalProperties=False, required=list(node.get('properties', {})))
            for child in node.values():
                require_fields(child)
        elif isinstance(node, list):
            for child in node:
                require_fields(child)
    require_fields(schema)
    return schema

def grounded_result(value,cards,sources,mode='advice'):
    try:
        result = CounselResult.model_validate(value).model_dump()
        if mode not in ('advice', 'listen'):
            raise ValueError('Unknown response mode')
        if [item['card_id'] for item in result['tarot']] != [item['card_id'] for item in cards]:
            raise ValueError('Cards must be the server draw, in order')
        references = {item['id'] for item in sources if item['kind'] in ('web', 'user_definition')}
        if any(not set(claim['source_ids']).issubset(references) for claim in result['facts']):
            raise ValueError('Unknown factual reference')
        if any(item['kind'] == 'web' and not safe_public_url(item.get('url')) for item in sources):
            raise ValueError('Private reference URL')
        if any(not action.strip() or len(action) > 120 for action in result['actions']):
            raise ValueError('Invalid action')
        if not result['empathy'].strip() or not result['advice'].strip():
            raise ValueError('Empty response')
        if mode == 'advice' and not result['actions']:
            raise ValueError('An actionable reply is required')
        if mode == 'listen' and (cards or result['actions']):
            raise ValueError('Listening does not draw cards or prescribe actions')
        insights = [dict(card_id=card['card_id'], position=card['position'],
                         meaning=card['card'].get('basic_meaning', '생각을 돌아보는 상징이에요.'),
                         reflection=reflection['reflection'])
                    for card, reflection in zip(cards, result['tarot'])]
        validate_result(dict(kind='tarot', empathy=result['empathy'],
            summary=' '.join(claim['text'] for claim in result['facts']) or '지금의 마음을 함께 살펴봐요.',
            card_insights=insights, advice=result['advice'],
            actions=result['actions'] or ['편하게 이야기를 이어가세요.'], expression='listen',
            follow_up_questions=[result['follow_up_question']] if result['follow_up_question'] else [],
            safety_mode=result['safety_mode']), 'tarot', cards, result_model=CounselingResultCheck)
        return result
    except (ValueError, TypeError, KeyError, AIError):
        raise CounselingError('OUTPUT_INVALID', 502,
            '답변의 근거를 확인하지 못했어요. 같은 카드로 다시 요청할 수 있어요.') from None
