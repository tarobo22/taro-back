import json
from pathlib import Path

from api.main import app


def export():
    path = Path(__file__).resolve().parents[1] / 'docs/contracts/openapi.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Exported {len(app.openapi()["paths"])} API paths')


if __name__ == '__main__':
    export()
