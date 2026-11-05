# taro-back · 여운

## 현재 구현 현황 - 2026-11-06

기획 대비 남은 통계·달력·즐겨찾기·선택 기록 AI 요약·Android 기기 알림을 연결했다. [현재 구현 현황](docs/planning/implementation-status-20261106.md), [68개 요구사항 추적표](docs/planning/requirements-traceability-20261106.csv), [남은 결정안](docs/planning/remaining-decisions-20261106.md)을 기준으로 확인한다. RN/웹 1.3.0이며, 운영 배포·실제 GPT 품질·계정/관리/결제/스토리 정책은 구분한다.

FastAPI·DDD 도메인·PostgreSQL·OpenAI 어댑터. 저장소 이름은 사용자가 지정한 철자를 유지했다.

## 실행

Node 24.22.2 / Python 3.12 / PostgreSQL 17 / RN 0.85.3 / React 19.2.3.

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r api/requirements-dev.txt -c constraints-dev.txt
Copy-Item .env.example .env.local
docker compose up -d --wait db
.venv/Scripts/python.exe -m api.migrate
.venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8808 --reload --no-access-log
# 격리 DB 포함 검사: ./tools/test.ps1
```

웹: http://127.0.0.1:8807 · API: http://127.0.0.1:8808/docs.
Android 에뮬레이터 API: 10.0.2.2:8808. 개발 OTA: 127.0.0.1:8806.
제품 API는 FastAPI이며 Node는 빌드·OTA 도구에만 사용한다.

## 현재 기능

마음기록 CRUD·임시 글 보관, 1·3·5장 타로·일일 카드·78장 외부 에셋,
좌 아니요/우 예·수동 전송·음성 입력·이어지는 대화, 다정/기가차드 모드,
표정·카드 뒤집기·다이얼·효과음·배경음·지원 기기의 햅틱 설정을 연결했다.
웹은 기존 화면을 React Native Web으로 렌더링한다.

GPT는 서버 전용 연결 코드와 계약 검사가 준비돼 있다. 실제 키·모델이 없으므로 기본 가이드를 사용한다.
운영 HTTPS·앱 서명·운영 OTA·iOS 프로젝트·실기기 성능은 별도 작업이다.
개발 APK는 배포용 운영 서명을 대신하지 않는다.

## Git와 검증

기존 이력은 로컬에 백업하고 원격이 비어 있을 때 main을 다시 구성했다.
각 기능 commit 뒤 push와 원격 SHA를 대조했다. 이후 기능은 codex/ 브랜치에서 작업한다.
검사 범위와 한계는 [TDD·테스트 평가](docs/quality/tdd-audit.md)에 명시했다.

- [API 계약](docs/contracts/openapi.json)
- [기획 PDF](docs/planning/타로기획1안.pdf)
- [검사 기록](docs/quality/verification.json)
- [기능별 커밋 기록](docs/quality/commits.json)

GitHub Actions 자동 등록은 사용자의 추가 인증 중단 요청에 따라 보류했다. [CI 설정 템플릿](docs/ci/checks.yml)은 준비돼 있으며 실제 원격 CI가 실행됐다고 표시하지 않는다.

## P0 구현 완료 기록

[기능·TDD·검증 결과와 남은 범위](docs/quality/p0-completion.md)를 확인할 수 있다.
