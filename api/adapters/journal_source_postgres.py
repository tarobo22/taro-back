"""Indexed source lookup without loading other journal or conversation histories."""
from psycopg.rows import dict_row


class PostgresJournalSourceLookup:
    def resolve(self, connection, owner, kind, source_id):
        with connection.cursor(row_factory=dict_row) as cursor:
            if kind == 'reading':
                cursor.execute('SELECT id FROM p0_readings WHERE owner_id=%s AND id=%s', (owner, source_id))
                row = cursor.fetchone()
                if row:
                    return {'kind': 'reading_v2', 'reading_id': str(row['id'])}
                cursor.execute('SELECT id,result,created_at FROM tarot_readings WHERE session_id=%s AND id=%s',
                               (owner, source_id))
                row = cursor.fetchone()
                if row:
                    return {'kind': 'legacy_reading', 'reading': {**row['result'], 'id': str(row['id']),
                                                                'created_at': row['created_at']}}
                cursor.execute('SELECT id,day,result,created_at FROM daily_readings WHERE session_id=%s AND id=%s',
                               (owner, source_id))
                row = cursor.fetchone()
                if row:
                    return {'kind': 'legacy_reading', 'reading': {**row['result'], 'id': str(row['id']),
                                                                'day': row['day'], 'created_at': row['created_at']}}
            elif kind == 'turn':
                cursor.execute('''SELECT t.id,t.conversation_id FROM ai_conversation_turns t
                    JOIN ai_conversations c ON c.id=t.conversation_id AND c.owner_id=t.owner_id
                    WHERE t.owner_id=%s AND t.id=%s''', (owner, source_id))
                row = cursor.fetchone()
                if row:
                    return {'kind': 'turn_v2', 'conversation_id': str(row['conversation_id']), 'turn_id': str(row['id'])}
                cursor.execute('SELECT id,topic,question,tone,advice,created_at FROM counsel_sessions WHERE session_id=%s AND id=%s',
                               (owner, source_id))
                row = cursor.fetchone()
                if row:
                    return {'kind': 'legacy_counsel', 'counsel': {**row['advice'], 'id': str(row['id']),
                            'topic': row['topic'], 'question': row['question'], 'tone': row['tone'],
                            'created_at': row['created_at']}}
        return None
