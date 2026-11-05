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

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env.local')
        url = os.environ.get('DATABASE_URL')
        if not url:
            raise RuntimeError('DATABASE_URL is required. Copy .env.example to .env.local for local development.')
        return cls(url, tuple(x.strip() for x in os.environ.get('CORS_ORIGINS', '').split(',') if x.strip()),
                   os.environ.get('OPENAI_MODEL', ''))
