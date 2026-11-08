from fastapi.testclient import TestClient

from api.adapters.http.data_privacy import Card
from api.application.data_privacy import DataPrivacyUseCases
from api.config import Settings
from api.domain.admin import snapshot_for
from api.domain.data_privacy import DataPolicy
from api.main import create_app, read_shared


def test_experience_config_pairs_existing_labels_with_stable_ids():
    app = create_app(Settings('postgresql://schema_only_test', ()), result_provider=None,
                     transcription_provider=None, workers_enabled=False)
    response = TestClient(app).get('/v1/experience-config')
    assert response.status_code == 200
    config = response.json()
    assert [(option['id'], option['label']) for option in config['topicOptions']] == [
        ('relationship', '연애·관계'), ('work', '일·진로'), ('self', '나 자신'),
        ('daily', '오늘의 선택'), ('care', '마음 돌봄'), ('start', '새로운 시작'),
    ]
    assert config['topics'] == [option['label'] for option in config['topicOptions']]
    assert config['cards'][0]['short_meaning'] == '모든 걸 알기 전에도 작은 첫걸음을 내디딜 수 있어요.'


def test_short_card_copy_is_deck_specific_and_does_not_change_snapshot_identity():
    catalog = read_shared('data-catalog.json')
    deck_id = catalog['decks'][0]['id']
    base_catalog = {key: value for key, value in catalog.items() if key != 'card_short_meanings'}
    assert snapshot_for(catalog, deck_id) == snapshot_for(base_catalog, deck_id)
    service = DataPrivacyUseCases(lambda: None, DataPolicy.from_mapping(read_shared('data-policy.json')), catalog)
    cards = service.list_cards(deck_version_id=deck_id)['items']
    assert len(cards) == 78 and all(card['short_meaning'] for card in cards)
    assert service.get_card(0, deck_version_id=deck_id)['short_meaning'] == cards[0]['short_meaning']
    assert Card.model_validate(base_catalog['cards'][0]).short_meaning is None
