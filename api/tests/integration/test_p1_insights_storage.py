from pathlib import Path
from uuid import uuid4

import pytest

from api.adapters.journal_insights_postgres import PostgresJournalInsightsUnitOfWork
from api.domain.journal_insights import insight_view


pytestmark = pytest.mark.integration


def test_storage_preserves_only_source_references_until_explicit_owner_erasure(pg_pool, owners):
    owner, other = owners
    with pg_pool.connection() as tx:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            tx.execute(migration.read_text(encoding='utf-8'))
        entry, insight = uuid4(), uuid4()
        tx.execute("INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,'2026-11-05','calm','do not copy body')", (entry, owner))
    values = {'id': insight, 'owner_id': owner, 'range_start': '2026-11-05', 'range_end': '2026-11-05',
              'based_on_count': 1, 'consent_event_id': uuid4(), 'context_consent_id': uuid4()}
    with PostgresJournalInsightsUnitOfWork(pg_pool) as uow:
        repository = uow.insights
        repository.lock_owner(owner)
        created = repository.add(values, [{'id': entry, 'version': 1}])
        assert created['state'] == 'draft' and 'do not copy body' not in str(created)
        assert repository.get(other, insight) is None
        assert repository.mark_generating(owner, insight, 1)
        uow.commit()
    with pg_pool.connection() as tx:
        tx.execute('DELETE FROM journal_entries WHERE id=%s', (entry,))
    with PostgresJournalInsightsUnitOfWork(pg_pool) as uow:
        assert uow.insights.current_sources(owner, [entry]) == []
        uow.insights.invalidate_for_journal(owner, entry)
        view = insight_view(uow.insights.get(owner, insight))
        assert view['state'] == 'invalidated' and view['result'] is None
        assert view['entry_ids'] == [str(entry)] and view['entry_versions'] == {str(entry): 1}
        uow.insights.erase_owner(owner)
        assert uow.insights.get(owner, insight) is None
        uow.commit()
    with pg_pool.connection() as tx:
        assert tx.execute('SELECT count(*) AS n FROM p1_journal_insight_sources WHERE insight_id=%s', (insight,)).fetchone()['n'] == 0
