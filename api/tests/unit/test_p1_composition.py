"""The shipped composition exposes P1 contracts without opening a database for schema export."""
from psycopg_pool import ConnectionPool

from api.config import Settings
from api.main import create_app


def test_p1_composition_is_db_free_and_registers_every_planned_p1_operation(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Construction and schema export must not open a database')
    monkeypatch.setattr(ConnectionPool, 'connection', forbidden)
    monkeypatch.setattr(ConnectionPool, 'open', forbidden)
    app = create_app(Settings('postgresql://schema_export_only_test', (), private_audio_dir=tmp_path),
                     result_provider=None, transcription_provider=None, workers_enabled=False)
    paths = app.openapi()['paths']
    operations = [('/v2/journal-summary', 'get', '200', 'getJournalSummary'),
                  ('/v2/favorites', 'get', '200', 'listFavorites'),
                  ('/v2/favorites', 'post', '201', 'addFavorite'),
                  ('/v2/favorites/{reading_id}', 'delete', '204', 'removeFavorite'),
                  ('/v2/journal-insights', 'post', '202', 'createInsight'),
                  ('/v2/journal-insights/{insight_id}', 'get', '200', 'getInsight')]
    for path, method, status, operation_id in operations:
        assert paths[path][method]['operationId'] == operation_id
        assert status in paths[path][method]['responses']
