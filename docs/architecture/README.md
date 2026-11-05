# 현재 구조

React 화면 → hook/유스케이스 → repository port → API adapter → FetchClient.
FastAPI router → application use cases → domain rules / repository·UoW ports → PostgreSQL·OpenAI adapters.
기분·카드·스프레드·입력 제한은 서버 설정에서 받는다. 얼굴은 코드, 몸·팔·손은 PNG다.
원격 커밋은 main에 기능별로 게시했고, 이후 변경은 codex/ 작업 브랜치로 진행한다.
[품질 평가](../quality/tdd-audit.md)와 [실행 안내](../../README.md)를 함께 확인한다.
