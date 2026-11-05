"""Durable insight metadata and original source references, with no journal copies."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.adapters.ai_jobs_postgres import default_owner_guard, owner_lock


class PostgresJournalInsightsRepository:
    def __init__(self, transaction):
        self.transaction = transaction

    def lock_owner(self, owner):
        owner_lock(self.transaction, owner)

    def owner_available(self, owner, guard=None):
        return (guard or default_owner_guard)(self.transaction, owner) is not False

    def _with_sources(self, row):
        if row:
            row = dict(row)
            row['sources'] = self.transaction.execute('''SELECT entry_id,entry_version FROM p1_journal_insight_sources
                WHERE insight_id=%s ORDER BY position''', (row['id'],)).fetchall()
        return row

    def get(self, owner, insight_id):
        return self._with_sources(self.transaction.execute('SELECT * FROM journal_insights WHERE owner_id=%s AND id=%s', (owner, insight_id)).fetchone())

    def add(self, values, sources):
        row = self.transaction.execute('''INSERT INTO journal_insights
            (id,owner_id,range_start,range_end,based_on_count,consent_event_id,context_consent_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''', tuple(values[key] for key in
            ('id', 'owner_id', 'range_start', 'range_end', 'based_on_count', 'consent_event_id', 'context_consent_id'))).fetchone()
        for position, source in enumerate(sources, 1):
            self.transaction.execute('''INSERT INTO p1_journal_insight_sources(insight_id,entry_id,entry_version,position)
                VALUES(%s,%s,%s,%s)''', (values['id'], source['id'], source['version'], position))
        return self._with_sources(row)

    def latest_consent(self, owner, kind):
        return self.transaction.execute('''SELECT id,granted,policy_version FROM ai_consent_events
            WHERE owner_id=%s AND kind=%s ORDER BY occurred_at DESC,id DESC LIMIT 1''', (owner, kind)).fetchone()

    def current_sources(self, owner, ids):
        return self.transaction.execute('SELECT id,version,day FROM journal_entries WHERE session_id=%s AND id=ANY(%s)', (owner, ids)).fetchall()

    def mark_generating(self, owner, insight_id, version):
        return self.transaction.execute("""UPDATE journal_insights SET state='generating',result=NULL
            WHERE owner_id=%s AND id=%s AND version=%s AND state IN ('draft','failed','cancelled') RETURNING id""",
            (owner, insight_id, version)).fetchone() is not None

    def mark_terminal(self, owner, insight_id, version, state):
        self.transaction.execute('''UPDATE journal_insights SET state=%s,result=NULL WHERE owner_id=%s AND id=%s
            AND version=%s AND state='generating' ''', (state, owner, insight_id, version))

    def complete(self, owner, insight_id, version, result):
        return self.transaction.execute("""UPDATE journal_insights SET state='ready',result=%s,version=version+1
            WHERE owner_id=%s AND id=%s AND version=%s AND state='generating' RETURNING id""",
            (Jsonb(result), owner, insight_id, version)).fetchone() is not None

    def invalidate(self, owner, insight_id):
        self.transaction.execute("""UPDATE journal_insights SET state='invalidated',result=NULL,version=version+1
            WHERE owner_id=%s AND id=%s AND state!='invalidated'""", (owner, insight_id))

    def invalidate_for_journal(self, owner, entry_id):
        self.transaction.execute("""UPDATE journal_insights SET state='invalidated',result=NULL,version=version+1
            WHERE owner_id=%s AND state!='invalidated' AND id IN
            (SELECT insight_id FROM p1_journal_insight_sources WHERE entry_id=%s)""", (owner, entry_id))

    def invalidate_for_consent(self, owner, kind):
        if kind in ('ai_processing', 'journal_context'):
            self.transaction.execute("""UPDATE journal_insights SET state='invalidated',result=NULL,version=version+1
                WHERE owner_id=%s AND state!='invalidated'""", (owner,))

    def all_owned(self, owner):
        return [self._with_sources(row) for row in self.transaction.execute(
            'SELECT * FROM journal_insights WHERE owner_id=%s ORDER BY created_at,id', (owner,)).fetchall()]

    def erase_owner(self, owner):
        self.transaction.execute('DELETE FROM p0_ai_idempotency WHERE owner_id=%s AND resource_id IN (SELECT id FROM journal_insights WHERE owner_id=%s)', (owner, owner))
        self.transaction.execute('DELETE FROM journal_insights WHERE owner_id=%s', (owner,))


class PostgresJournalInsightsUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def repository_for(transaction):
        return PostgresJournalInsightsRepository(transaction)

    def __enter__(self):
        self.transaction = self.pool.getconn()
        self.transaction.row_factory = dict_row
        self.insights = self.repository_for(self.transaction)
        return self

    def commit(self):
        self.transaction.commit()

    def __exit__(self, *exc):
        try:
            self.transaction.rollback()
        finally:
            self.pool.putconn(self.transaction)
