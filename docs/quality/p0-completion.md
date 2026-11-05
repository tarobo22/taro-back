# 여운 P0 구현·검증 완료 기록

2026-11-06 · React Native CLI / React Native Web / FastAPI / PostgreSQL

합의한 P0 기능을 구현하고 기능별 GREEN 커밋을 원격에 push했다. 기획서의 P0 API **44개**는 모두 경로·메서드·성공 상태가 일치한다. 실제 운영 배포 완료를 뜻하지 않는다.

| 영역 | 완료한 동작 |
|---|---|
| 진입·설정 | 명시적 게스트 시작, 유효 세션 연장, 만료 시 자동 교체 방지, 서버 말투·모션·음성 설정 복원 |
| 타로 | 덱·78장 출처 확인, 서버 비공개 슬롯·섞기·순서 선택, 일일 카드 고정, 기본/AI 결과·삭제 |
| 대화·AI | 텍스트·좌우 선택·음성, 목적별 동의, 선택 일기 최대 3개와 버전 재확인, 비동기 작업·취소·명시적 재시도 |
| 마음기록 | 저장 응답 유실 후 중복 방지, 초안 복원, CAS 충돌 해결, 결과 원본 연결, 종류·날짜 필터·더보기 |
| 개인정보 | 인증된 JSON/CSV ZIP, 전체 삭제·기기 초안 정리·토큰 폐기, 최소 접수증 상태 확인, 소유권 피드백 |
| 감각·업데이트 | 기존 파스텔 에셋·표정·사운드·햅틱 유지, 48dp 조작 영역, 프로필 ±6dp, 모션 감소, 서버 정책을 반영하는 OTA |

## 아키텍처와 TDD

화면은 repository/use-case를 통해 데이터를 사용하며 fetch·세션·저장은 infrastructure에 분리했다. 백엔드는 domain 규칙, application use-case/repository·UoW port, PostgreSQL/OpenAI/HTTP adapter와 composition root로 나눴다. Application에는 PostgreSQL driver import나 SQL 실행이 없다. 외부 AI 요청 중에는 DB 트랜잭션과 owner lock을 유지하지 않는다. 소유권 잠금, lease, CAS, 동의 이벤트·참조 버전 재검사로 중복 처리와 삭제 후 결과 재생성을 막는다.

새 기능과 경계 추출은 RED → 구현 → GREEN 로그를 남겼다. 과거 재구성 이력의 모든 RED 실행을 소급 증명한다는 의미는 아니다. 테스트 삭제·검증 완화·skip은 없다. 사용자 승인에 따라 기존 E2E의 명시적 온보딩 진입과 새 요청 계약을 확장했고, 출력 검증의 양성 fixture summary 한 줄을 유효 문장으로 맞췄다. 실제 요청에 추가된 context_entry_versions도 ID·버전까지 엄격히 비교한다.

## 검증 결과

| 검사 | 결과 | 실제 검증 범위 |
|---|---:|---|
| 백엔드 | 226 passed | 실제 격리 PostgreSQL, 동시 요청·소유권·lease·취소·버전·내보내기·삭제 |
| RN 단위 검사 | 54 passed | 초안·재시도·참조·설정·페이지·OTA 정책 |
| Web 단위 검사 | 51 passed | 공유 기능·DTO·상태 경합 |
| 브라우저 | 19 passed | 13 계약 + 4 기존 회귀 + 2 실제 API/DB/worker 흐름 |
| TypeScript / Web build | 통과 | 두 클라이언트 타입 검사, production bundle |
| Android | 통과 | 1.2.0 / code 3 / x86_64 release APK, 실행·모션 감소·파일 선택·저장 |
| 실제 OTA | 통과 | preview / 기본 앱 1.2.0에서 다운로드·사용자 적용·SDK launch report와 실행 bundle 확인 |

음성은 생성한 1초 무음 파일과 로컬 테스트 제공자로 검증했다. 선택만으로 전송하지 않았고, 명시적 전사 요청 후 수정한 문장을 초안에 넣어도 대화 turn은 생성되지 않았다. 임시 음성·전사문 제거를 DB에서 확인했다. Android 시스템 저장창으로 받은 ZIP은 CRC와 JSON/CSV 구성까지 검사했다. 실제 OpenAI 호출·마이크 녹음은 하지 않았다.

최종 Android 검사에서 모션 감소 시 본문이 숨는 현상을 발견해 최소 범위로 교정한 뒤 실제 에뮬레이터에서 다시 확인했다. 수정 전 결과를 성공으로 표시하지 않았다. 앞선 동일 SDK 1.1.7의 의도된 시작 실패 복구 검증은 1.1 기준이며, 최신 1.2는 정상 적용을 검증했다. 실패 시험용과 본문 오류가 있던 preview release는 비활성화했다.

## Git와 변경 범위

- 검증한 코드 SHA: `59a05c69a1e2a6d53c23a71542838bb724d5a9c9`
- 작업 브랜치: `codex/complete-core-flows-20261106`
- 기능 커밋과 실제 변경 파일은 [변경 목록](p0-changes.json), 실행 조건·APK/OTA 식별자는 [검증 데이터](p0-verification.json)에 기록했다.
- 승인한 P0 구현·계약 검사·구조 정리·완료 문서만 변경했다. 기존 사용자 변경, 키, .env.local, SDK 경로, 바이너리 빌드 산출물을 Git에 넣지 않았다.
- 최신 API 계약은 [taro-back OpenAPI](https://github.com/tarobo22/taro-back/blob/codex/complete-core-flows-20261106/docs/contracts/openapi.json)가 기준이다.

## 별도로 남은 범위

| 항목 | 상태 |
|---|---|
| GPT 실제 모델·비용·응답 품질 | 서버 연결·단일 요청 경계·모의 제공자 검증 완료, 실제 호출 미실행 |
| 운영 배포 | HTTPS, 운영 DB/OTA 호스팅, 스토어 서명·배포 별도 |
| iOS·실기기 감각/성능 | 이번은 Android 에뮬레이터와 Web 검증, iOS 및 실제 촉감·마이크·기기 성능 미검증 |
| 테스트 정리 | DB·에뮬레이터 앱 데이터 삭제를 포함한 정리 명령이 자동 검토에서 정책상 차단됐다. 합성 테스트 데이터는 보존했으며, 격리 HTTP 서버를 중단하고 실제 개발 API로 복원했다. 세부 차단 사유는 제공되지 않았다. |
| 원격 CI | 로컬 검사와 CI 템플릿 준비, 사용자의 추가 인증 중단 요청에 따라 Actions 등록 보류 |
| 카드 에셋 발행 | 원본 78장·출처·해시 확인, 현재 candidate 상태이며 최종 발행 승인 별도 |
| P1/P2 | 계정 동기화·결제·알림·통계·스토리 게임 등은 이번 P0 구현 범위 밖 |

AI 한도는 기존 운영 정책인 사용자 20회/일·전체 200회를 유지했다. 예전 기획 값 30회를 실제 적용했다고 표시하지 않는다. Web 최종 JS는 약 651kB / gzip 183kB이며 chunk 크기 경고가 남아 있다. 새 기능의 FPS나 실기기 성능을 측정한 것으로 해석하지 않는다.
