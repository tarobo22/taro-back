from datetime import date
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.adapters.http.journal_summary import journal_summary_router
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.journal_summary import JournalSummaryInvalid


OWNER = UUID(int=1)
EMPTY = {'range_start': '2026-10-08', 'range_end': '2026-11-06', 'entry_count': 0,
         'days_with_entries': [], 'mood_counts': {'calm': 0, 'happy': 0, 'thinking': 0, 'settled': 0, 'talk': 0}}


def client_for(service, owner=lambda: OWNER):
    app = FastAPI()
    app.include_router(journal_summary_router(service, owner))
    return TestClient(app)


def test_summary_http_exact_public_contract_and_required_date_queries():
    service = Mock()
    service.summary.return_value = EMPTY
    with client_for(service) as client:
        result = client.get('/v2/journal-summary', params={'from': EMPTY['range_start'], 'to': EMPTY['range_end']})
        assert result.status_code == 200 and result.json() == EMPTY
        service.summary.assert_called_once_with(OWNER, start=date(2026, 10, 8), end=date(2026, 11, 6))
        operation = client.get('/openapi.json').json()['paths']['/v2/journal-summary']['get']
        assert operation['operationId'] == 'getJournalSummary'
        assert {p['name']: p['required'] for p in operation['parameters']} == {'from': True, 'to': True}


@pytest.mark.parametrize('params', [{}, {'from': '2026-11-06'}, {'to': '2026-11-06'},
    {'from': 'invalid', 'to': '2026-11-06'}, {'from': '2026-02-30', 'to': '2026-11-06'}])
def test_bad_or_missing_dates_do_not_reach_the_service(params):
    service = Mock()
    with client_for(service) as client:
        assert client.get('/v2/journal-summary', params=params).status_code == 422
    service.summary.assert_not_called()


@pytest.mark.parametrize('error,status', [(JournalSummaryInvalid('range'), 422),
    (DataNotFound('owner'), 404), (OwnerDeleting('owner'), 409)])
def test_domain_and_owner_errors_are_translated(error, status):
    service = Mock()
    service.summary.side_effect = error
    with client_for(service) as client:
        assert client.get('/v2/journal-summary?from=2026-11-06&to=2026-11-06').status_code == status


def test_summary_uses_authentication_dependency_before_calling_service():
    def unauthenticated():
        raise HTTPException(401, 'session required')
    service = Mock()
    with client_for(service, unauthenticated) as client:
        assert client.get('/v2/journal-summary?from=2026-11-06&to=2026-11-06').status_code == 401
    service.summary.assert_not_called()
