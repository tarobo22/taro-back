"""Behavior contracts for the rewrite; existing counseling tests stay unchanged."""
import pytest

from api.domain.counseling import (
    Analysis, CounselingError, draw_cards, personal_definitions,
    retrieve_definitions, safe_public_url, strict_schema,
)


def test_matching_private_memory_is_normalized_bounded_and_never_changes_the_input():
    entries = [{'term': 'ＲＡＧ', 'definition': '내 모임'},
               {'term': 'rag 모임', 'definition': '더 구체적인 의미'},
               {'term': '별개', 'definition': '가져오지 않을 기억'}]
    found = retrieve_definitions('RAG 모임 때문에 고민이에요', entries, limit=1)
    assert found == [entries[1]]
    assert len(entries) == 3 and entries[0]['term'] == 'ＲＡＧ'


def test_model_cannot_store_a_definition_quoted_only_from_retrieved_context():
    value = Analysis(understanding='모임 일이 마음에 걸리시는군요.', clarification_question=None,
        explicit_definitions=[{'term': '반짝', 'definition': '내 독서 모임',
                               'evidence_quote': '반짝은 내 독서 모임이야'}], unknown_terms=[])
    assert personal_definitions(value, '반짝 때문에 속상해요') == []


@pytest.mark.parametrize('count', [True, 0, 2, 5])
def test_server_draw_rejects_unsupported_counts_before_calling_random_source(count):
    calls = []
    with pytest.raises(CounselingError, match='DECK_INVALID'):
        draw_cards([{'id': i} for i in range(78)], count,
                   sample=lambda population, n: calls.append(n))
    assert calls == []


def test_structured_contract_requires_nested_fields_and_forbids_extra_properties():
    schema = strict_schema(Analysis)
    assert set(schema['required']) == set(schema['properties'])
    for model in schema['$defs'].values():
        assert model['additionalProperties'] is False
        assert set(model['required']) == set(model['properties'])


@pytest.mark.parametrize('url', ['https://example.com:8443/', 'https://a.local/',
                                'https://[::1]/', 'https://example.com/\nprivate'])
def test_private_or_ambiguous_reference_links_cannot_be_published(url):
    assert safe_public_url(url) is False
