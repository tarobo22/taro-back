import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_url: str
    cors_origins: tuple[str, ...]
    openai_model: str = ''
    transcription_model: str = ''
    private_audio_dir: Path = ROOT / '.local' / 'private-audio'
    worker_poll_seconds: float = 0.25
    maintenance_seconds: float = 30
    ota_channel: str = 'preview'
    min_native_version: str = '1.2.0'
    ota_updates_enabled: bool = True
    allow_candidate_decks: bool = True

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env.local')
        url = os.environ.get('DATABASE_URL')
        if not url:
            raise RuntimeError('DATABASE_URL is required. Copy .env.example to .env.local for local development.')
        return cls(url, tuple(x.strip() for x in os.environ.get('CORS_ORIGINS', '').split(',') if x.strip()),
                   os.environ.get('OPENAI_MODEL', ''), os.environ.get('OPENAI_TRANSCRIPTION_MODEL', ''),
                   Path(os.environ.get('PRIVATE_AUDIO_DIR', str(ROOT / '.local' / 'private-audio'))),
                   ota_channel=os.environ.get('OTA_CHANNEL', 'preview'),
                   min_native_version=os.environ.get('MIN_NATIVE_VERSION', '1.2.0'),
                   ota_updates_enabled=os.environ.get('OTA_UPDATES_ENABLED', 'true').lower() == 'true',
                   allow_candidate_decks=os.environ.get('ALLOW_CANDIDATE_DECKS', 'true').lower() == 'true')
