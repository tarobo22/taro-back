from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from api.application.reading_v2 import ReadingV2Service
from api.application.reading_v2_ports import DailyAnchor, ReadingCommand, ReadingRecord, ReadingRepository
from api.domain.ai_jobs import AIError, fingerprint


OWNER, RID, DECK, KEY = UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4)
NOW = datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)
CARDS = [{'id': n, 'basic_meaning': f'card {n}'} for n in range(78)]


def record(*, rid=RID, version=1, kind='three_card', state='draft', slots=None, selected=None):
    selected = [] if selected is None else selected
    slots = [] if slots is None else slots
    return ReadingRecord(id=rid, kind=kind, state=state, version=version, deck_version_id=DECK,
                         private_slots=slots, selected_cards=selected, created_at=NOW,
                         view={'id': str(rid), 'state': state, 'version': version,
                               'slots': [{'id': s['id'], 'index': s['index']} for s in slots],
                               'selected_cards': selected})


class FakeUnitOfWork:
    """The fake deliberately has no connection or execute capability."""
    def __init__(self, repository):
        self.readings = repository
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def commit(self):
        self.commits += 1

    def repository_for(self, transaction):
        assert transaction is self.readings.transaction
        return self.readings


def context(**options):
    repository = Mock(spec=ReadingRepository)
    repository.transaction = object()
    repository.owner_available.return_value = True
    repository.command.return_value = None
    repository.timezone.return_value = 'Asia/Seoul'
    repository.find_daily.return_value = None
    repository.daily_anchor.return_value = None
    repository.get.return_value = record()
    unit = FakeUnitOfWork(repository)
    catalog = SimpleNamespace(get_deck=Mock(return_value={'status': 'candidate'}),
                              list_cards=Mock(return_value={'items': CARDS}))
    service = ReadingV2Service(lambda: unit, catalog, clock=lambda: NOW,
                              random_sample=lambda values, count: values[:count], **options)
    return service, repository, unit, catalog


def test_daily_create_uses_locked_owner_repository_and_preserves_an_existing_anchor():
    service, repo, unit, _ = context()
    repo.daily_anchor.return_value = DailyAnchor(card_id=7, deck_version_id=DECK, captured_timezone='Asia/Seoul')
    repo.add.side_effect = lambda owner, rid, **values: record(rid=rid, kind='daily', state=values['state'], selected=values['selected_cards'])
    result = service.create(OWNER, KEY, {'kind': 'daily', 'topic': 'care', 'deck_version_id': str(DECK)})
    repo.lock_owner.assert_called_once_with(OWNER)
    repo.owner_available.assert_called_once_with(OWNER, None)
    repo.daily_anchor.assert_called_once_with(OWNER, date(2026, 11, 7), 'care')
    repo.add_daily_anchor.assert_not_called()
    assert repo.add.call_args.kwargs['selected_cards'][0]['card_id'] == 7
    assert result['selected_cards'][0]['card_id'] == 7 and unit.commits == 1


def test_request_replay_returns_a_reference_without_new_draw_or_commit():
    service, repo, unit, catalog = context()
    body = {'kind': 'three_card', 'topic': 'care', 'deck_version_id': str(DECK), 'question': ''}
    repo.command.return_value = ReadingCommand(fingerprint=fingerprint(body), reading_id=RID)
    expected = record()
    repo.get.return_value = expected
    assert service.create(OWNER, KEY, body) == expected.view
    repo.add.assert_not_called()
    catalog.get_deck.assert_not_called()
    assert unit.commits == 0
    repo.get.return_value = None
    with pytest.raises(AIError, match='READING_DELETED'):
        service.create(OWNER, KEY, body)


def test_selection_uses_domain_order_then_repository_cas_and_refuses_a_cas_miss():
    service, repo, unit, _ = context()
    slots = [{'id': str(uuid4()), 'index': n + 1, 'card_id': n} for n in range(12)]
    repo.get.return_value = record(slots=slots)
    def save(owner, rid, version, selected):
        return record(version=2, state='selected', selected=selected)
    repo.select.side_effect = save
    chosen = [slots[n]['id'] for n in (9, 2, 5)]
    result = service.select(OWNER, RID, KEY, 1, chosen)
    assert [c['card_id'] for c in result['selected_cards']] == [9, 2, 5]
    assert repo.select.call_args.args[:3] == (OWNER, RID, 1)
    assert unit.commits == 1
    repo.select.side_effect = None
    repo.select.return_value = None
    with pytest.raises(AIError, match='READING_VERSION_CONFLICT'):
        service.select(OWNER, RID, uuid4(), 1, chosen)
    assert unit.commits == 1


def test_owner_lifecycle_failure_stops_reading_access_before_repository_query():
    service, repo, unit, _ = context()
    repo.owner_available.return_value = False
    with pytest.raises(AIError, match='OWNER_UNAVAILABLE'):
        service.get(OWNER, RID)
    repo.lock_owner.assert_called_once_with(OWNER)
    repo.get.assert_not_called()
    assert unit.commits == 0


def test_delete_cancels_pending_work_on_the_same_opaque_transaction_before_removal():
    jobs = SimpleNamespace(cancel_for_resource=Mock())
    service, repo, unit, _ = context(ai_jobs=jobs)
    events = []
    jobs.cancel_for_resource.side_effect = lambda *args: events.append(('cancel', args))
    repo.delete.side_effect = lambda *args: events.append(('delete', args))
    service.delete(OWNER, RID)
    assert events == [('cancel', (repo.transaction, OWNER, RID)), ('delete', (OWNER, RID))]
    assert unit.commits == 1


def test_cross_context_callbacks_use_a_bound_repository_without_driver_operations():
    service, repo, unit, _ = context()
    repo.snapshot.return_value = {'id': str(RID), 'version': 1, 'cards': []}
    assert service.snapshot(repo.transaction, OWNER, RID) == repo.snapshot.return_value
    repo.exists.return_value = True
    assert service.source_exists(repo.transaction, OWNER, RID) is True
    repo.all_owned.return_value = [record()]
    assert service.export_owner(repo.transaction, OWNER) == {'v2_readings': [record().view]}
    service.erase_owner(repo.transaction, OWNER)
    repo.erase_owner.assert_called_once_with(OWNER)
    assert unit.commits == 0


def test_completion_validates_selected_cards_then_uses_repository_compare_and_swap():
    service, repo, unit, _ = context()
    selected = [{'card_id': 0, 'position': 1}]
    repo.get.return_value = record(state='generating', selected=selected)
    result = {'kind': 'tarot', 'empathy': '들었어요.', 'summary': '한 걸음씩 살펴봐요.',
              'card_insights': [{'card_id': 0, 'position': 1, 'meaning': '시작', 'reflection': '작게 시도해요.'}],
              'advice': '직접 골라도 괜찮아요.', 'actions': ['잠깐 쉬어요.'], 'expression': 'listen',
              'follow_up_questions': [], 'safety_mode': 'standard'}
    repo.complete.return_value = True
    assert service.complete(repo.transaction, OWNER, RID, 1, result) is True
    repo.complete.assert_called_once_with(OWNER, RID, 1, result)
    with pytest.raises(AIError, match='AI_OUTPUT_INVALID'):
        service.complete(repo.transaction, OWNER, RID, 1, {**result, 'card_insights': []})
    assert repo.complete.call_count == 1 and unit.commits == 0
