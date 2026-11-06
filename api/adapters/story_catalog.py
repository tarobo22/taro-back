import json
from pathlib import Path
from api.domain.story import StoryCatalog


def load_story_catalog(path=None):
    source = Path(path) if path else Path(__file__).parents[2] / 'shared/story-episodes.json'
    return StoryCatalog.from_mapping(json.loads(source.read_text(encoding='utf-8')))
