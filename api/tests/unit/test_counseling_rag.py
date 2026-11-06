from uuid import uuid4

import pytest

from api.domain.counseling import (
    Analysis, CounselingError, draw_cards, grounded_result, personal_definitions,
    retrieve_definitions, search_queries,
)


def analysis(**changes):
    return Analysis.model_validate({'understanding':'새 출발이 걱정되는 마음이에요.',
        'clarification_question':None,'explicit_definitions':[], 'unknown_terms':[], **changes})


def test_draw_is_python_random_without_replacement_and_keeps_catalog_artwork():
    cards=[{'id':i,'name_ko':str(i),'basic_meaning':'의미','image_asset':{'id':str(i)}} for i in range(78)]
    calls=[]
    def sample(population,count):
        calls.append((population,count));return [population[77],population[2],population[31]]
    result=draw_cards(cards,sample=sample)
    assert [c['card_id'] for c in result]==[77,2,31]
    assert [c['position'] for c in result]==[1,2,3]
    assert result[0]['card']['image_asset']=={'id':'77'}
    assert len(calls)==1 and calls[0][1]==3


def test_random_source_cannot_return_duplicates_or_cards_outside_deck():
    cards=[{'id':i} for i in range(78)]
    for invalid in ([1,1,2],[0,1,78]):
        with pytest.raises(CounselingError,match='DRAW_INVALID'):
            draw_cards(cards,sample=lambda p,n:invalid)


def test_user_definition_requires_verbatim_evidence_in_current_user_message():
    a=analysis(explicit_definitions=[{'term':'반짝','definition':'우리 동아리 이름',
        'evidence_quote':'반짝은 우리 동아리 이름이야'}])
    assert personal_definitions(a,'반짝은 우리 동아리 이름이야')[0]['term']=='반짝'
    assert personal_definitions(a,'반짝 때문에 고민이에요')==[]


def test_dictionary_retrieval_is_relevant_and_does_not_invent_missing_terms():
    entries=[{'id':str(uuid4()),'term':'반짝','definition':'내 동아리'},
             {'id':str(uuid4()),'term':'초록','definition':'친구 별명'}]
    assert [v['term'] for v in retrieve_definitions('반짝 모임이 고민이에요',entries)]==['반짝']
    assert retrieve_definitions('처음 보는 말',entries)==[]


def test_private_or_ambiguous_unknown_terms_are_never_sent_to_web_search():
    a=analysis(unknown_terms=[{'term':'반짝','kind':'personal'},
                            {'term':'그 모임','kind':'unclear'},
                            {'term':'국민취업지원제도','kind':'public'}])
    assert search_queries(a,[])==['국민취업지원제도 공식 자료']


def test_dictionary_hit_and_private_contact_information_are_not_search_queries():
    a=analysis(unknown_terms=[{'term':'반짝','kind':'public'},
                            {'term':'me@example.com','kind':'public'},
                            {'term':'010-1234-5678','kind':'public'}])
    assert search_queries(a,[{'term':'반짝','definition':'우리 모임'}])==[]


def test_advice_rejects_invented_citations_and_model_selected_cards():
    cards=draw_cards([{'id':i} for i in range(78)],sample=lambda p,n:[0,1,2])
    value={'empathy':'걱정되는 마음을 들려주셔서 고마워요.',
           'facts':[], 'tarot':[{'card_id':i,'reflection':'작은 시작을 떠올려봐요.'} for i in range(3)],
           'advice':'선택을 천천히 살펴봐요.','actions':['작은 일 하나를 적어보세요.'],
           'follow_up_question':'어떤 일부터 해볼까요?','safety_mode':'standard'}
    assert grounded_result(value,cards,[])['tarot'][0]['card_id']==0
    for changed in ({'facts':[{'text':'어떤 사실','source_ids':['made-up']}]},
                    {'tarot':[{'card_id':77,'reflection':'해석'}]}):
        with pytest.raises(CounselingError,match='OUTPUT_INVALID'):
            grounded_result({**value,**changed},cards,[])


def test_card_is_not_accepted_as_a_source_for_factual_claims():
    cards=draw_cards([{'id':i} for i in range(78)],sample=lambda p,n:[0,1,2])
    value={'empathy':'함께 살펴봐요.','facts':[{'text':'상대방은 거짓말을 해요.','source_ids':['tarot:0']}],
        'tarot':[{'card_id':i,'reflection':'생각을 정리해요.'} for i in range(3)],
        'advice':'차분히 생각해요.','actions':['확인한 사실을 적어보세요.'],
        'follow_up_question':None,'safety_mode':'standard'}
    with pytest.raises(CounselingError,match='OUTPUT_INVALID'):
        grounded_result(value,cards,[{'id':'tarot:0','kind':'tarot_reference','title':'카드 의미','url':None,'text':'상징'}])
