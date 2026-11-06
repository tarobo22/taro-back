# 이번 기능의 변경 파일

기존 작업 폴더와 미커밋 결제 변경은 수정하지 않았다. 새 기능 테스트와 검증 자료만 추가했으며 베이스라인 테스트는 변경하지 않았다.

- .env.example
- api/adapters/counseling_openai.py
- api/adapters/counseling_postgres.py
- api/adapters/http/counseling.py
- api/application/counseling.py
- api/application/counseling_ports.py
- api/domain/counseling.py
- api/main.py
- api/migrations/0027_counseling_rag.sql
- api/tests/integration/test_counseling_flow.py
- api/tests/integration/test_counseling_http.py
- api/tests/unit/test_counseling_openai.py
- api/tests/unit/test_counseling_rag.py
- docs/contracts/openapi.json
- docs/counseling/CHANGES.md
- docs/counseling/IMPLEMENTATION-20261107.md
- docs/counseling/STATUS-20261107.pdf
- docs/counseling/USE-CASES-50.md
- docs/counseling/live-evaluation-status.json
- docs/counseling/local-benchmark.json
- evals/counseling-cases.json
- shared/counseling-runtime.json
- tools/benchmark_counseling.py
- tools/evaluate_counseling.py

## 2026-11-07 상태 보고서 갱신

- IMPLEMENTATION-20261107.md: 완료·미완료, RAG 구현 범위, 순차 내부 성능 측정과 실제 GPT 미측정을 구분했다.
- STATUS-20261107.pdf: 같은 보고서의 열람용 PDF를 추가했다.
- CHANGES.md: 이번 문서 변경 범위를 기록했다.

이번 보고서 요청에서는 제품 코드·설정·테스트를 변경하지 않았고 부하 테스트를 실행하지 않았다. 이전 부하 기록은 삭제하지 않고 이번 성능 평가에서 제외했다.
