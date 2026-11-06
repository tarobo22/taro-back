# 실제 실행 인증·오류 계약 보충

이 문서는 `actual-openapi.json`과 함께 읽는다. 실제 서버의 자동 생성 원본을 수정하지 않았으며, 아래 내용은 canonical 실행 코드에서 확인한 보충 계약이다. 인증 실패 요청이나 사용자 데이터 조회를 새로 실행하지 않았다.

## 원본과 실행 계약의 차이

2026-11-06 수집한 원본은 81개 path, 99개 method/path operation, 119개 schema를 가진다. 중복 operationId는 없다. `components.securitySchemes`는 없고 인증 헤더는 보통 `required:false`인 일반 Header parameter로 나타난다. 이를 해당 API가 익명 사용자에게 열려 있다는 뜻으로 해석하면 안 된다.

실행 코드는 `api/main.py:current_session`과 각 router의 `Depends(owner_dependency)` 및 운영 서비스가 인증·소유권을 검사한다. API별 목록은 `api-auth-inventory.csv`에 분리했다. 아래의 특수 경로를 일반 owner 경로와 구분한다.

| 경로 | 실제 인증 경계 | 코드 근거 |
|---|---|---|
| `/health`, `/v2/health` | 공개 상태 확인 | `api/main.py`, `api/adapters/http/bootstrap.py` |
| `POST /v1/session`, `POST /v2/sessions` | 공개 게스트 연결 생성 | `api/main.py`, `api/adapters/http/identity.py` |
| `GET /v1/experience-config`, `GET /v1/journal-policy` | 공개 제품 설정 | `api/adapters/http/experience.py`, `journal.py` |
| `GET /v2/auth/providers` | 공개 준비 상태. 미설정이면 unavailable이며 성공 로그인을 흉내 내지 않는다 | `api/adapters/http/social_auth.py` |
| social start/account/sessions/logout | 현재 owner 세션. 서버가 인식한 revoked/expired social 세션을 legacy 인증으로 우회하지 않는다 | `api/main.py:current_session`, `api/application/social_auth.py` |
| social GET/POST callback | 앱 bearer를 URL에 넣지 않는다. 저장된 state·provider·만료와 제공자 응답을 검증한다 | `api/adapters/http/social_auth.py` |
| social exchange | 시작 owner 세션과 일회용 ticket·S256 verifier가 모두 필요하다 | 같은 router 및 social service |
| `GET /v2/admin/eligibility` | 일반 세션이 선택적이다. 없거나 유효하지 않으면 `eligible:false, role:null`을 반환한다. 역할을 발급하지 않는다 | `api/application/admin.py:eligibility` |
| `POST /v2/admin/sessions` | 유효한 social member/source session와 이미 명시 발급된 viewer/publisher 역할이 필요하다. 불충족 시 403 | `api/application/admin.py:issue` |
| 나머지 `/v2/admin/*` | 별도의 짧은 운영 세션. viewer/publisher 읽기, publisher 변경. source 세션 폐기·역할·owner·만료를 매번 재검사 | `AdminService._principal` |
| `GET /v2/data-requests/{request_id}` | owner 세션 또는 해당 요청 receipt. 일반 데이터 조회의 우회 권한으로 쓰지 않는다 | `api/main.py:status_session`, `DataPrivacyUseCases.get_data_request` |
| export download | owner 세션 필요. 상태 조회 receipt만으로 다운로드하지 않는다 | `api/adapters/http/data_privacy.py` |
| billing catalog/entitlements | owner 세션 | `api/adapters/http/billing.py` |
| billing verify | owner 세션과 member 계정, 서버 영수증 검증. guest 구매를 허용하지 않는다 | `api/application/billing.py:verify` |

운영 세션은 최대 900초 또는 source session 만료 중 이른 시점까지 유효하다. 서버는 해시만 저장하고 클라이언트는 운영 세션을 메모리에만 둔다. 사용자 API의 세션과 운영 세션을 서로 대체하지 않는다. 실제 인증정보 값은 이 보고서에 싣지 않는다.

## callback HTTP 성공 상태

자동 생성 원본에서 social callback 두 operation의 기본 성공 응답은 200으로 표시된다. 실제 정상 callback 구현은 allowlist 복귀 주소로 **303** redirect를 반환한다. 취소·실패가 저장된 복귀 흐름에 연결되는 경우에도 정해진 오류 fragment로 돌아간다. 알 수 없는 state나 잘못된 요청은 redirect 없이 오류가 될 수 있다.

Apple form callback은 `application/x-www-form-urlencoded`만 받으며 길이·필드 개수를 제한한다. 앱 bearer는 redirect에 없고, 완료 ticket은 별도의 짧은 유효기간·일회성·owner/PKCE 결합을 가진다. 실제 OAuth 등록·인증은 이번 검증에서 실행하지 않았다.

## 오류 본문

`api/main.py:error_response`는 `/v2/` 오류를 중앙 처리한다. 문자열 detail을 받는 일반 경로의 형식은 다음과 같다. 아래 값은 구조 설명용이며 실제 세션이나 개인정보가 아니다.

```json
{
  "error": {
    "code": "INVALID_INPUT",
    "message": "입력한 날짜와 내용을 확인해 주세요.",
    "retryable": false
  },
  "request_id": "<요청 UUID>"
}
```

detail이 dict로 명시된 경로는 그 dict를 보존한다. 기본 status/code 매핑은 401 SESSION_INVALID, 403 FORBIDDEN, 404 NOT_FOUND, 409 CONFLICT, 410 EXPIRED, 422 INVALID_INPUT, 429 RATE_LIMITED, 503 SERVICE_UNAVAILABLE이다. 매핑되지 않은 status는 SERVER_ERROR 기본값을 사용한다. 문자열 detail의 retryable은 429/500/502/503/504에서 true다. 이 플래그가 개인 데이터 변경을 자동 재전송하라는 뜻은 아니다.

`RequestValidationError`도 위 중앙 경로로 422를 반환하며 Pydantic의 입력 원문/세부 오류 배열을 그대로 내보내지 않는다. `/v2/` 오류에는 `Cache-Control:no-store`를 설정한다. legacy `/v1/` 오류는 호환을 위해 `{error:문자열}` 형식이며 위 request_id/envelope와 동일하다고 가정하지 않는다.

자동 생성 OpenAPI의 여러 422 응답은 FastAPI 기본 `HTTPValidationError`/`detail` 배열로 선언되어 있다. 예를 들어 `POST /v2/story-runs`가 그렇다. **이 기본 DTO를 실행 시 오류 형식의 근거로 삼으면 안 된다.** `api-error-inventory.csv`는 원본 선언과 중앙 실행 코드를 별도 행으로 기록한다. 모든 API에서 모든 오류 status가 발생한다는 의미도 아니다.

## 변경 범위

이번 보고서 수집은 API 동작·권한·DTO를 변경하지 않았다. 원본 OpenAPI와 이 실행 계약 보충을 함께 제공하는 것이 이번 산출물의 계약 표시 방식이다. 원본을 임의 보정해 실제 생성 결과와 다른 파일을 만들지 않았다.
