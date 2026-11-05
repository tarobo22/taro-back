# P1 선택 마음기록 AI 요약

FR-JOU-07 / S26을 기존 AI 작업의 lease, 동의, 한도, 취소, 명시적 재시도 경로로 처리한다. 실제 OpenAI 호출 없이 제공자 fake와 PostgreSQL로 검증했다.

## HTTP 계약

`POST /v2/journal-insights`는 인증된 owner와 `X-Idempotency-Key` 또는 호환 `Idempotency-Key` UUID를 받는다. 응답은 202 `JobAccepted`이고 `resource_id`는 insight ID다. 작업 `kind`는 `insight`이며 상태 조회, SSE, 취소, 재시도는 기존 `/v2/ai/jobs/{job_id}` 경로를 사용한다.

| 요청 필드 | 계약 |
|---|---|
| `range_start`, `range_end` | ISO 날짜. 양 끝을 포함하며 시작일은 종료일 이하여야 한다. |
| `entry_ids` | 명시적으로 고른 본인 기록 UUID 1..10개. 중복과 범위 밖 원본은 거절한다. |
| `consent_event_id` | 현재 정책 `2026-11-05`의 최신 `ai_processing` true 이벤트 ID. 별도의 최신 `journal_context` true 이벤트도 필수다. |
| `entry_versions` | 선택적 추가 전제. 제공하면 entry_ids 전체와 정확히 같은 UUID 키, 양의 정수 version 맵이어야 한다. null은 허용하지 않는다. 접수 전 원본 변경은 409 `RESOURCE_CHANGED`다. |

요청에는 원문을 넣을 수 없다. 선택한 ID 외의 다른 일기를 찾거나 자동으로 보충하지 않는다. 범위는 조회 필터로만 쓰이지 않고 선택한 각 원본에도 적용한다. 동의·소유권·버전 확인을 통과해야 한도를 예약하고 작업을 저장한다. 공급자가 준비되지 않으면 503, 동의가 없으면 403, 다른 owner 또는 없는 원본은 404다.

`GET /v2/journal-insights/{insight_id}`는 200으로 다음 필드를 반환한다. 이 조회는 AI 제공자를 호출하지 않는다.

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "range_start": "2026-11-01",
  "range_end": "2026-11-06",
  "state": "generating",
  "based_on_count": 1,
  "result": null,
  "entry_ids": ["00000000-0000-4000-8000-000000000002"],
  "entry_versions": {"00000000-0000-4000-8000-000000000002": 1}
}
```

`entry_ids`와 `entry_versions`는 S26 원본 바로가기를 서버의 선택 메타데이터로 복원하기 위해 기획안 기본 6필드에 추가한 승인 계약이다. 원문 본문은 응답하지 않는다. 준비된 `result`는 기존 `AIResult`이며 `kind=insight`, `card_insights=[]`다. 기존 진단·보장 등 출력 규칙을 그대로 검증한다.

| 상태 | 의미 |
|---|---|
| `draft` | 저장 트랜잭션 내부의 초기 상태. 정상적인 202 접수는 `generating`으로 커밋된다. |
| `generating` | 작업 대기 또는 실행 중. 원본과 동의가 유효하다. |
| `ready` | 검증된 결과 저장 완료. GET 때도 원본과 동의를 확인한다. |
| `failed` | 제공자 실패, 출력 오류, 시작 후 lease 만료의 불확실한 결과. 자동 재호출하지 않는다. |
| `cancelled` | 사용자가 명시적으로 취소했다. |
| `invalidated` | 원본 편집·삭제·선택 불일치 또는 captured 동의 교체/철회. 결과는 null이다. 재동의만으로 되살아나지 않는다. |

## 원본·동의와 결과 수명주기

Migration `0015_p1_insights.sql`은 `journal_insights`와 `p1_journal_insight_sources`를 추가하고 기존 jobs의 kind 제약을 확장한다. source 표는 ID, version, 선택 순서만 저장한다. 일기 원본과 동의 표에 FK를 두지 않아 삭제 후에도 무효화 사유와 원본 바로가기에 필요한 최소 참조가 남는다. insight 또는 owner 자체가 삭제되면 source 참조도 cascade로 지워진다.

접수, worker의 공급자 호출 전, payload 조립 시, 결과 저장 직전에 원본·버전·동의를 검증한다. durable insight 선택표와 job 선택표도 대조하여 작업 payload를 통한 선택 확장을 막는다. 외부 I/O 중 DB 트랜잭션이나 owner lock을 유지하지 않는다. 일기 본문은 메모리에서 공급자 payload에만 읽으며 jobs payload, idempotency, source 표, 진단 로그에 복제하지 않는다. 최대 10개라는 개수와 기존 12,000자 총 예산을 함께 적용한다. 기존 타로/대화 참조 최대 3개는 유지한다.

완료 후에도 GET과 내보내기는 현재 원본, 소유권, version, 날짜 범위, 최신 목적별 동의를 다시 확인한다. 무효하면 결과를 지우고 `invalidated`로 저장한다. `AIJobsService.cancel_for_journal(tx, owner, entry_id)`는 진행 작업 취소와 완료된 insight 무효화를 같은 트랜잭션에서 처리한다. 새 AI 또는 기록 참조 동의 이벤트도 이전 insight를 무효화한다.

명시적 취소 또는 실패의 재시도는 새 job ID와 같은 insight ID를 사용한다. 기존 source version과 동의가 계속 유효해야 한다. 삭제된 insight를 새로 만들지 않는다. 공급자 시작 전 lease 만료만 기존 작업을 다시 대기시킬 수 있으며 시작 후 만료는 `UNKNOWN_PROVIDER_OUTCOME`이다. 사용자 20회/일, 전체 200회/일, owner당 동시 작업 1개와 기존 usage accounting을 공유한다. 취소·실패는 예약된 시도를 환불하지 않는다.

## Composition root 연결

```python
insights = JournalInsightsService(
    lambda: PostgresJournalInsightsUnitOfWork(pool), ai_jobs=ai_jobs,
    owner_guard=owner_guard, policy=ai_policy)
ai_jobs.insight_source = insights
app.include_router(create_journal_insights_router(insights, owner_dependency))
```

생성자는 DB에 접속하지 않는다. application은 SQL 또는 psycopg에 의존하지 않는다. privacy callback에 `insights.export_owner(tx, owner)`와 `insights.erase_owner(tx, owner)`를 등록한다. export는 별도 `journal_insights` dataset을 반환한다. erase는 기존 작업을 취소하고 insight/source/idempotency 참조를 삭제한다. AI jobs erase는 기존 usage owner를 익명화하여 일일 과금 집계를 보존한다. 진행 중 전체 삭제가 끝나면 늦은 공급자 결과가 insight를 다시 만들 수 없다.
