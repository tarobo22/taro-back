"""Real PostgreSQL ownership and deletion boundaries for linked-source navigation."""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal_v2 import journal_v2_router
from api.adapters.journal_source_postgres import PostgresJournalSourceLookup
from api.adapters.journal_v2_postgres import PostgresJournalV2UnitOfWork
from api.application.journal_v2 import JournalV2UseCases
from api.domain.journal_v2 import JournalV2Policy
from api.tests.integration.test_journal_v2 import ROOT
from api.tests.integration.test_data_privacy_profile_catalog import data_context
import json

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('kind', ['reading_v2', 'turn_v2', 'legacy_reading', 'legacy_daily', 'legacy_counsel'])
def test_source_is_indexed_owner_scoped_read_and_cannot_open_deleted_sources(data_context, kind):
    _, current, _, owners, pool = data_context
    source, entry, conversation = uuid4(), uuid4(), uuid4()
    content = json.loads((ROOT / 'shared/content.json').read_text(encoding='utf-8'))
    rules = json.loads((ROOT / 'shared/journal-policy.json').read_text(encoding='utf-8'))
    policy = JournalV2Policy.from_legacy(content['moods'], rules,
        json.loads((ROOT / 'shared/journal-v2-policy.json').read_text(encoding='utf-8')))
    service = JournalV2UseCases(lambda: PostgresJournalV2UnitOfWork(pool), policy,
                               source_lookup=PostgresJournalSourceLookup().resolve)
    app = FastAPI();app.include_router(journal_v2_router(service, lambda: current[0]))
    with pool.connection() as connection:
        connection.execute("INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,'2026-11-06','편안해요','QA source')",
                           (entry, owners[0]))
        column = 'source_turn_id' if kind in ('turn_v2', 'legacy_counsel') else 'source_reading_id'
        connection.execute(f'INSERT INTO journal_v2_sources(entry_id,owner_id,{column}) VALUES(%s,%s,%s)', (entry, owners[0], source))
        if kind == 'reading_v2':
            table = 'p0_readings';owner_column = 'owner_id'
            connection.execute("""INSERT INTO p0_readings(id,owner_id,kind,topic,deck_version_id,captured_timezone,state)
                VALUES(%s,%s,'three_card','self',%s,'Asia/Seoul','draft')""", (source, owners[0], uuid4()))
        elif kind == 'turn_v2':
            table = 'ai_conversation_turns';owner_column = 'owner_id'
            connection.execute("INSERT INTO ai_conversations(id,owner_id,topic,tone) VALUES(%s,%s,'self','warm')", (conversation, owners[0]))
            connection.execute("""INSERT INTO ai_conversation_turns(id,owner_id,conversation_id,client_message_id,
                fingerprint,turn_index,text,answers,input_source) VALUES(%s,%s,%s,%s,'test',1,'QA','[]','text')""",
                (source, owners[0], conversation, uuid4()))
        elif kind in ('legacy_reading', 'legacy_daily'):
            table = 'tarot_readings' if kind == 'legacy_reading' else 'daily_readings';owner_column = 'session_id'
            result = json.dumps({'topic': '나의 마음', 'question': 'QA', 'cards': [], 'summary': '요약', 'advice': '조언', 'action': '한 걸음'})
            if kind == 'legacy_reading':
                connection.execute('INSERT INTO tarot_readings(id,session_id,topic,card_ids,result) VALUES(%s,%s,%s,%s,%s)',
                                   (source, owners[0], '나의 마음', [0], result))
            else:
                connection.execute("INSERT INTO daily_readings(id,session_id,day,topic,result) VALUES(%s,%s,'2026-11-06',%s,%s)",
                                   (source, owners[0], '나의 마음', result))
        else:
            table = 'counsel_sessions';owner_column = 'session_id'
            advice = json.dumps({'empathy': '들었어요', 'advice': '조언', 'action': '한 걸음', 'expression': 'listen'})
            connection.execute("INSERT INTO counsel_sessions(id,session_id,topic,question,answers,tone,advice) VALUES(%s,%s,'나의 마음','QA','[]','warm',%s)",
                               (source, owners[0], advice))
        before = connection.execute('SELECT count(*) AS n FROM ai_usage_attempts').fetchone()['n']
    with TestClient(app) as client:
        path = f'/v2/journal-entries/{entry}/source'
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()['kind'] == ('legacy_reading' if kind == 'legacy_daily' else kind)
        if kind == 'turn_v2':assert response.json()['conversation_id'] == str(conversation)
        if kind.startswith('legacy_'):
            nested = response.json()['counsel' if kind == 'legacy_counsel' else 'reading']
            assert nested['id'] == str(source)
        current[0] = owners[1]
        assert client.get(path).status_code == 404
        current[0] = owners[0]
        with pool.connection() as connection:
            connection.execute(f'DELETE FROM {table} WHERE {owner_column}=%s AND id=%s', (owners[0], source))
        assert client.get(path).status_code == 404
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM ai_usage_attempts').fetchone()['n'] == before
        assert connection.execute('SELECT 1 FROM journal_entries WHERE id=%s', (entry,)).fetchone()
