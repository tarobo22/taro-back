from fastapi import FastAPI

from api.adapters.http.journal_v2 import journal_v2_router
from api.application.journal_v2 import JournalV2UseCases
from api.domain.journal_v2 import JournalV2Policy


def test_journal_construction_and_openapi_never_acquire_a_database_connection():
    def forbidden_connection():
        raise AssertionError('OpenAPI export must not acquire a database connection')
    policy = JournalV2Policy.from_legacy(['편안해요', '기뻐요', '생각이 많아요', '차분해요', '이야기하고 싶어요'],
        {'maxBodyLength': 7}, {'idempotencyHours': 24, 'pageDefault': 20, 'pageMax': 50})
    service = JournalV2UseCases(forbidden_connection, policy)
    app = FastAPI()
    app.include_router(journal_v2_router(service, lambda: None))
    document = app.openapi()
    assert document['paths']['/v2/journal-entries']['post']['operationId'] == 'createJournal'
    assert document['components']['schemas']['JournalInput']['properties']['body']['maxLength'] == 7
    assert document['components']['schemas']['JournalPatch']['required'] == ['expected_version']
