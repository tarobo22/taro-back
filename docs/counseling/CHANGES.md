# 이번 기능의 변경 파일

기존 작업 폴더와 미커밋 결제 변경은 수정하지 않았다. 새 기능 테스트와 검증 자료만 추가했으며 베이스라인 테스트는 변경하지 않았다.

- .env.example
- api/adapters/counseling_openai.py
- api/adapters/counseling_strategy.py
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
- api/tests/unit/test_counseling_strategy.py
- docs/contracts/openapi.json
- docs/counseling/CHANGES.md
- docs/counseling/IMPLEMENTATION-20261107.md
- docs/counseling/STATUS-20261107.pdf
- docs/counseling/RESEARCH-COUNSELING-20261107.md
- docs/portfolio/AI-COUNSELING.md
- docs/portfolio/research-addendum.json
- docs/portfolio/verification.json
- docs/portfolio/yeoun-development-portfolio.pdf
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

## 후속 요청: RAG·상담 연구와 포트폴리오

- 제품 코드: counseling_strategy.py 신규, counseling_openai.py의 전략 선택·출력 계약 적용.
- 새 테스트: test_counseling_strategy.py 9건. 기존 테스트는 수정하지 않았다.
- 문서: RESEARCH-COUNSELING-20261107.md, 본 변경 목록, docs/portfolio/ 아래 연구 포트폴리오 자료.
- 로컬 포트폴리오: 연구 추가 장·생성 원본·MD·PDF·검수 기록을 갱신했다.
- 실제 모델 호출·가중치 학습·부하 테스트는 실행하지 않았다.

## 후속 요청: 실제 GPT 패턴 전후 A/B 비교

- evals/counseling-ab-20261107.json: 새 합성 12사례와 사전 기대 동작.
- tools/evaluate_counseling_ab.py: 동일 조건·단일 요청·최대 48회·US$1 상한 평가 도구. 키는 입력받아 메모리에서만 사용한다.
- tools/report_counseling_ab.py: 추가 API 호출 없이 기록된 출력·채점으로 보고서와 원문 비교 화면 생성.
- docs/counseling/ab-20261107/: 원본·동일 입력·조건·사전 기준·X/Y 점수·매핑·집계·CSV·HTML·PDF·표시 검수.
- 연구 문서·포트폴리오·본 변경 목록은 실제 평가 결과와 연구 당시 기록을 구분하도록 갱신했다.
- 이번 평가에서 제품 코드·기존 테스트·운영 서버 키 설정은 변경하지 않았다. 결과를 보고 프롬프트를 고치거나 실패 사례를 다시 호출하지 않았다.
