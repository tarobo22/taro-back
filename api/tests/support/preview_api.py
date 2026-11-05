"""Opt-in loopback integration fixture. Real DB/workers, fake providers, no external model calls."""
import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

# Test fixture only: prevent an inherited development key from activating any legacy provider.
os.environ['OPENAI_API_KEY'] = ''

from api.adapters.ai_audio import PrivateAudioStore
from api.config import ROOT, Settings
from api.main import create_app
from api.tests.integration.test_p0_app_flow import ResultProvider


class TranscriptionProvider:
    model = 'local-test-transcription'

    def available(self):
        return True

    def transcribe(self, path, mime_type):
        return '차근차근 마음을 정리하고 싶어요.'


def application():
    url = os.environ.get('DATABASE_URL', '')
    info = conninfo_to_dict(url)
    if not info.get('dbname', '').endswith('_test') or info.get('host') not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('The preview fixture requires an explicit isolated loopback database ending in _test')
    settings = Settings(url, ('http://127.0.0.1:8827',), worker_poll_seconds=0.05, maintenance_seconds=0.1)
    return create_app(settings, result_provider=ResultProvider(), transcription_provider=TranscriptionProvider(),
                      audio_store=PrivateAudioStore(ROOT / '.local' / 'p1-preview-audio'))
