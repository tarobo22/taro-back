"""The shipped schema must describe enforced authentication and actual errors."""
from uuid import UUID

from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from api.config import Settings
from api.main import create_app


def app_without_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('schema export and rejected input must not acquire a database')
    monkeypatch.setattr(ConnectionPool, 'connection', forbidden)
    monkeypatch.setattr(ConnectionPool, 'open', forbidden)
    return create_app(Settings('postgresql://schema_only_test', ()), result_provider=None,
        transcription_provider=None, workers_enabled=False)


def test_openapi_distinguishes_session_admin_and_receipt_alternatives(monkeypatch):
    document = app_without_database(monkeypatch).openapi()
    schemes = document['components'].get('securitySchemes', {})
    assert schemes.get('SessionBearer', {}).get('scheme') == 'bearer'
    assert schemes.get('AdminBearer', {}).get('scheme') == 'bearer'
    assert schemes.get('DataReceipt', {}).get('name') == 'X-Data-Receipt'
    paths = document['paths']
    assert paths['/v2/journal-entries']['get']['security'] == [{'SessionBearer': []}]
    assert paths['/v2/admin/eligibility']['get']['security'] == [{'SessionBearer': []}]
    assert paths['/v2/admin/sessions']['post']['security'] == [{'SessionBearer': []}]
    assert paths['/v2/admin/catalog']['get']['security'] == [{'AdminBearer': []}]
    assert paths['/v2/data-requests/{request_id}']['get']['security'] == [
        {'SessionBearer': []}, {'DataReceipt': []}]
    for path, method in [('/v2/sessions', 'post'), ('/v2/auth/providers', 'get'),
                         ('/v2/auth/social/{provider}/callback', 'get')]:
        assert not paths[path][method].get('security')
    assert document is app_without_database(monkeypatch).openapi() or document == app_without_database(monkeypatch).openapi()


def test_v2_error_schemas_and_header_inputs_match_the_runtime_contract(monkeypatch):
    app = app_without_database(monkeypatch)
    document = app.openapi()
    assert app.openapi() is document
    reference = {'$ref': '#/components/schemas/V2ErrorEnvelope'}
    envelope = document['components']['schemas'].get('V2ErrorEnvelope', {})
    assert set(envelope.get('required', [])) == {'error', 'request_id'}
    assert envelope['properties']['request_id']['format'] == 'uuid'
    for path, item in document['paths'].items():
        if not path.startswith('/v2/'):
            continue
        for method, operation in item.items():
            if method not in {'get', 'post', 'put', 'patch', 'delete'}:
                continue
            assert operation['responses']['default']['content']['application/json']['schema'] == reference
            if '422' in operation['responses']:
                assert operation['responses']['422']['content']['application/json']['schema'] == reference
            if operation.get('security'):
                assert all(parameter['name'].lower() != 'authorization' for parameter in operation.get('parameters', []))
    assert document['paths']['/v2/auth/social/{provider}/callback']['get']['responses'].get('303')


def test_missing_and_malformed_auth_and_validation_have_the_documented_error_shape(monkeypatch):
    app = app_without_database(monkeypatch)
    client = TestClient(app)
    responses = [client.get('/v2/me'), client.get('/v2/me', headers={'Authorization': 'Basic invalid-fixture'}),
        client.post('/v2/sessions', json={'platform': 'invalid-device'})]
    assert [response.status_code for response in responses] == [401, 401, 422]
    for response in responses:
        data = response.json()
        assert set(data) == {'error', 'request_id'}
        assert set(data['error']) == {'code', 'message', 'retryable'}
        UUID(data['request_id'])
        assert response.headers['cache-control'] == 'no-store'
