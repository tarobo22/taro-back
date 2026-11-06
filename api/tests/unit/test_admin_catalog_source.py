from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from api.application.data_privacy import DataPrivacyUseCases
from api.domain.data_privacy import DataPolicy, InvalidDataInput
from api.tests.support.admin_fake import catalog_fixture


def policy():
    return DataPolicy.from_mapping(json.loads((Path(__file__).parents[3]/'shared/data-policy.json').read_text(encoding='utf-8')))


def test_catalog_source_is_lazy_and_selects_the_explicit_immutable_version():
    catalog,ids=catalog_fixture();factory=Mock(side_effect=AssertionError('constructor acquired database'))
    source=Mock(return_value=catalog)
    service=DataPrivacyUseCases(factory,policy(),{},catalog_source=source)
    source.assert_not_called();factory.assert_not_called()
    assert service.get_card(0,deck_version_id=ids[1])==catalog['cards_by_deck'][ids[1]][0]
    assert source.call_count==1
    source.reset_mock()
    assert service.list_cards(deck_version_id=ids[1])['items']==catalog['cards_by_deck'][ids[1]]
    assert source.call_count==1
    result=service.get_deck(ids[0]);result['name']='caller mutation'
    assert catalog['decks'][0]['name']=='Fixture deck'


def test_catalog_paging_is_bound_to_the_dynamic_publication_snapshot():
    catalog,_=catalog_fixture();source=Mock(return_value=deepcopy(catalog))
    service=DataPrivacyUseCases(Mock(),policy(),{},catalog_source=source)
    first=service.list_cards(limit=1)
    assert first['next_cursor']
    source.return_value['source_manifest_sha256']='c'*64
    with pytest.raises(InvalidDataInput,match='invalid_catalog_cursor'):
        service.list_cards(limit=1,cursor=first['next_cursor'])


def test_catalog_query_failure_never_returns_an_outdated_successful_catalog():
    catalog,ids=catalog_fixture();source=Mock(side_effect=RuntimeError('synthetic unavailable'))
    service=DataPrivacyUseCases(Mock(),policy(),catalog,catalog_source=source)
    for read in (lambda:service.list_decks(),lambda:service.get_deck(ids[0]),
                 lambda:service.list_cards(),lambda:service.get_card(0)):
        with pytest.raises(RuntimeError,match='synthetic unavailable'):read()
    legacy=DataPrivacyUseCases(Mock(),policy(),catalog)
    assert legacy.list_decks()['items']==catalog['decks']
