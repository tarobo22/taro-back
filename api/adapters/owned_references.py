"""Owner-scoped cross-context references inside the caller's existing transaction."""


class PostgresOwnedReferences:
    def __init__(self, readings, jobs):
        self.readings, self.jobs = readings, jobs

    def exists(self, transaction, owner, kind, resource_id):
        if kind == 'reading':
            return self.readings.source_exists(transaction, owner, resource_id) or bool(transaction.execute(
                'SELECT 1 FROM tarot_readings WHERE session_id=%s AND id=%s UNION ALL '
                'SELECT 1 FROM daily_readings WHERE session_id=%s AND id=%s LIMIT 1',
                (owner, resource_id, owner, resource_id)).fetchone())
        if kind == 'turn':
            return self.jobs.turn_exists(transaction, owner, resource_id) or bool(transaction.execute(
                'SELECT 1 FROM counsel_sessions WHERE session_id=%s AND id=%s', (owner, resource_id)).fetchone())
        return False
