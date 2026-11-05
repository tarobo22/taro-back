from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class PostgresExperienceRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock(self, owner, kind, request_id):
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                                (f'{owner}:{kind}:{request_id}',))

    def operation(self, owner, kind, request_id):
        return self.connection.execute('SELECT fingerprint,result FROM experience_operations WHERE session_id=%s AND kind=%s AND request_id=%s',
                                       (owner, kind, request_id)).fetchone()

    def remember(self, owner, kind, request_id, fingerprint, result):
        self.connection.execute('INSERT INTO experience_operations(session_id,kind,request_id,fingerprint,result) VALUES(%s,%s,%s,%s,%s)',
                                (owner, kind, request_id, fingerprint, Jsonb(result)))

    def save_reading(self, owner, entry_id, result):
        row = self.connection.execute('INSERT INTO tarot_readings(id,session_id,topic,question,card_ids,result) VALUES(%s,%s,%s,%s,%s,%s) RETURNING created_at',
            (entry_id, owner, result['topic'], result['question'], [c['id'] for c in result['cards']], Jsonb(result))).fetchone()
        return {**result, 'id': str(entry_id), 'created_at': row['created_at'].isoformat()}

    def save_counsel(self, owner, entry_id, result, answers, parent):
        row = self.connection.execute('INSERT INTO counsel_sessions(id,session_id,topic,question,answers,tone,advice,parent_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING created_at',
            (entry_id, owner, result['topic'], result['question'], Jsonb(answers), result['tone'], Jsonb(result), parent)).fetchone()
        return {**result, 'id': str(entry_id), 'created_at': row['created_at'].isoformat()}

    def list(self, owner, kind):
        if kind == 'reading':
            rows = self.connection.execute('SELECT id,result,created_at FROM tarot_readings WHERE session_id=%s ORDER BY created_at DESC LIMIT 100', (owner,)).fetchall()
            daily = self.connection.execute('SELECT id,result,day,created_at FROM daily_readings WHERE session_id=%s ORDER BY day DESC LIMIT 100', (owner,)).fetchall()
            combined = [{**r['result'], 'id': str(r['id']), 'created_at': r['created_at'].isoformat()} for r in rows]
            combined += [{**r['result'], 'id': str(r['id']), 'day': str(r['day']), 'created_at': r['created_at'].isoformat()} for r in daily]
            return sorted(combined, key=lambda r: r['created_at'], reverse=True)[:100]
        rows = self.connection.execute('SELECT id,topic,question,tone,advice,created_at FROM counsel_sessions WHERE session_id=%s ORDER BY created_at DESC LIMIT 100', (owner,)).fetchall()
        return [{**r['advice'], 'topic': r['topic'], 'question': r['question'], 'tone': r['tone'],
                 'id': str(r['id']), 'created_at': r['created_at'].isoformat()} for r in rows]

    def daily(self, owner, day, topic, result):
        self.connection.execute('INSERT INTO daily_readings(session_id,day,topic,result) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                                (owner, day, topic, Jsonb(result)))
        row = self.connection.execute('SELECT id,result,created_at FROM daily_readings WHERE session_id=%s AND day=%s AND topic=%s', (owner, day, topic)).fetchone()
        return {**row['result'], 'id': str(row['id']), 'day': str(day), 'created_at': row['created_at'].isoformat()}

    def context(self, owner, parent):
        rows = self.connection.execute('''WITH RECURSIVE chain AS (
          SELECT id,parent_id,question,advice,1 AS depth FROM counsel_sessions WHERE id=%s AND session_id=%s
          UNION ALL SELECT c.id,c.parent_id,c.question,c.advice,p.depth+1 FROM counsel_sessions c
          JOIN chain p ON c.id=p.parent_id WHERE c.session_id=%s AND p.depth<5
        ) SELECT question,advice FROM chain ORDER BY depth DESC''', (parent, owner, owner)).fetchall()
        return [{'question': row['question'], 'advice': row['advice'].get('advice', '')} for row in rows]

    def ai_job(self, owner, request_id):
        return self.connection.execute(
            'SELECT fingerprint,lease_id,state,lease_until>clock_timestamp() AS active FROM experience_ai_jobs WHERE session_id=%s AND request_id=%s',
            (owner, request_id)).fetchone()

    def reserve_ai(self, owner, request_id, fingerprint, lease_id, owner_limit, global_limit, lease_seconds):
        from api.application.experience import UsageLimitReached
        # Only reservation accounting is serialized; provider calls never hold this lock.
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('ai:global-daily-reservations',))
        usage = self.connection.execute("""
            SELECT count(*) AS total,count(*) FILTER(WHERE session_id=%s) AS owned
            FROM ai_usage_attempts
            WHERE created_at >= date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """, (owner,)).fetchone()
        if usage['owned'] >= owner_limit:
            raise UsageLimitReached('오늘 AI 대화를 충분히 나누었어요. 기본 가이드로 계속할 수 있어요.')
        if usage['total'] >= global_limit:
            raise UsageLimitReached('지금은 AI 대화가 잠시 쉬고 있어요. 기본 가이드로 계속할 수 있어요.')
        self.connection.execute("""
            INSERT INTO experience_ai_jobs(session_id,request_id,fingerprint,lease_id,lease_until,state)
            VALUES(%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),'pending')
            ON CONFLICT(session_id,request_id) DO UPDATE SET
                lease_id=excluded.lease_id,lease_until=excluded.lease_until,state='pending'
        """, (owner, request_id, fingerprint, lease_id, lease_seconds))
        self.connection.execute('INSERT INTO ai_usage_attempts(id,session_id,request_id) VALUES(%s,%s,%s)',
                                (lease_id, owner, request_id))

    def finish_ai(self, owner, request_id, lease_id, state):
        self.connection.execute('UPDATE experience_ai_jobs SET state=%s WHERE session_id=%s AND request_id=%s AND lease_id=%s',
                                (state, owner, request_id, lease_id))


class PostgresExperienceUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.experiences = PostgresExperienceRepository(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
