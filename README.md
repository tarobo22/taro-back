# 여운 · FastAPI 서버

여운의 카드·상담·기록 API입니다. FastAPI가 요청을 접수하고, 애플리케이션 계층이 상담 흐름을 관리합니다. PostgreSQL에 상태를 저장하고 OpenAI 어댑터에서 응답을 생성합니다. [공개 데모](https://yeoun-43-201-209-249.sslip.io/) · [공개 API 상태](https://yeoun-43-201-209-249.sslip.io/api/health)

## 핵심 흐름

1. 상담 요청을 접수하고 버전·요청 키를 확인합니다.
2. 사용자 원문을 바탕으로 필요한 정보를 확인합니다. 사연이 충분하면 서버가 카드를 정합니다.
3. 확정한 카드의 ID와 순서를 응답에 대조합니다. 생성이 실패하면 같은 카드로 이어갈 수 있습니다.
4. 카드마다 짧게 풀이하고, 카드를 함께 본 결론과 해 볼 일 하나를 반환합니다.

[API 계약](docs/contracts/openapi.json) · [상담 설계와 검증 자료](docs/portfolio/AI-COUNSELING.md)

## 로컬 실행

Python 3.12, Docker Compose, PostgreSQL 17을 사용합니다.

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r api/requirements-dev.txt -c constraints-dev.txt
Copy-Item .env.example .env.local
docker compose up -d --wait db
.venv/Scripts/python.exe -m api.migrate
.venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8808 --reload --no-access-log
# 격리 DB 포함 검사
./tools/test.ps1
```

API 문서는 `http://127.0.0.1:8808/docs`에서 열립니다. 로컬에서 실제 GPT 응답을 확인하려면 서버 환경에 모델과 API 연결을 설정합니다.

## 배포·평가

2026-11-08 기준 공개 API `3646de3`에서 PostgreSQL 연결과 실제 GPT 상담을 확인했습니다. 기존 API 테스트 629개와 별도 배포 검사 12개가 통과했습니다. [CI 실행](https://github.com/tarobo22/taro-back/actions/runs/42470540941) · [운영 버전](https://yeoun-43-201-209-249.sslip.io/deployment-status.json) · [배포 흐름](docs/deployment/ci-cd.md)

이전 A/B 실험은 합성 사연 12건과 실제 API 호출 48회의 비교입니다. 최종 답변 평균 점수는 66.67에서 74.58로 변했습니다. 최종 응답의 모델 호출 평균은 7.15초에서 8.29초로 늘었습니다. 공개 데모의 실제 상담 1건은 해당 흐름의 동작 검증입니다. [평가 원문과 조건](docs/counseling/ab-20261107/README.md)

## 설계 기록

[기획 PDF](docs/planning/타로기획1안.pdf) · [2026-11-06 구현 현황](docs/planning/implementation-status-20261106.md) · [요구사항 추적표](docs/planning/requirements-traceability-20261106.csv) · [남은 결정안](docs/planning/remaining-decisions-20261106.md) · [P0 완료 기록](docs/quality/p0-completion.md) · [TDD 평가](docs/quality/tdd-audit.md)
