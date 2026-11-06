# 최근 AI 상담 기능 · 실제 TDD 재구현 결과

기준일: 2026-11-07 · 브랜치: `codex/counseling-tdd-20261107`

**최근 AI 상담의 동작 로직을 구현 직전 커밋에서 다시 작성하고, 실제 RED → GREEN → 회귀 검사 → 기능별 commit/push를 수행했다. 과거 구현 이력을 TDD로 소급 표시하지 않는다.**

## 범위와 보존

| 대상 | 이번 처리 |
|---|---|
| FastAPI | 카드·근거 도메인, 개인사전 저장, 작업 큐, 상담 유스케이스, GPT 어댑터·전략, HTTP·계정 삭제 연결 재구현 |
| React·RN | API 검증·요청 경계, 계정별 초안, 대화 컨트롤러, 기존 화면 연결 재구현 |
| 기존 계약 | DTO, 포트 선언, 0027 DB 스키마, API 경로·요청·응답 형식 유지 |
| 기존 디자인 | 파스텔 화면·카드 고정 영역·타이포·에셋·네비게이션 구성 유지. 디자인 snapshot 테스트 추가 없음 |
| 기존 자료 | 기획·아키텍처·명세·사용방법·포트폴리오 PDF·이전 실제 AI 평가·평가 도구 보존 |
| 기존 Git | 세 저장소 모두 `codex/backup-counseling-before-tdd-20261107` 원격 백업. main 변경·force push 없음 |

순서를 바꾸거나 보존한 제품 구현을 복원하여 새 TDD라고 부르는 재구성이 아니다. 기존 계약 테스트는 요구사항으로 재사용하고 원본 바이트를 유지했으며, 제품 동작은 미구현 상태에서 실제 실패를 확인한 뒤 작성했다. DTO·SQL 계약과 기존 화면 마크업까지 새로 발명했다고 주장하지 않는다.

## 실제 RED와 GREEN

| 단위 | 구현 전 실행 | 구현 후 실행 | 기능 커밋 |
|---|---|---|---|
| B01 카드·근거 도메인 | 19 실패 | 19 통과 | backend `bbf5229` |
| B04 GPT 전송·전략 | 12 실패·8 통과 | 20 통과 | backend `a98833c` |
| B02 소유권·작업 큐 | 4 실패 | 4 통과 | backend `692d09e` |
| B03 확인·RAG 상담 흐름 | 15 실패 | 15 통과 | backend `c26cfbb` |
| B05 HTTP·계정 생명주기 | 4 실패 | 4 통과 | backend `f210292` |
| F01 API·개인 초안 | 웹·RN 각각 4 실패·7 통과 | 각각 11 통과 | web `a458d11` / RN `7adfa1c` |
| F02 대화 상태 | 웹·RN 각각 8 실패 | 각각 8 통과 | web `5641eba` / RN `b40761b` |
| F03 새 이야기·화면 연결 | 웹·RN 각각 1 실패 | 각각 1 통과 | web `985a4d1` / RN `621cf5a` |

RED는 미구현 함수의 NotImplemented 또는 미연결 endpoint의 404 등 기능 결손을 검출한 실행이다. GREEN 커밋에 테스트와 구현을 함께 담고, RED 실행 로그는 evidence에 보존한다. RED 자체를 실패하는 원격 커밋으로 push하지 않았다.

**중간 실패를 숨기지 않는다.** B01 회귀 검사에서 환경변수 누락·기존 Windows 임시 폴더 권한 오류가 있었다. 사용자 승인 후 전용 합성 DB와 새 임시 경로로 해결했다. B02 신규 테스트의 준비 코드가 읽기 전용 상담 역할로 동의 행을 INSERT하려 한 오류는 사용자 승인 후 DB fixture 연결로 옮겼다. 모든 assert와 제품 권한을 유지했다. 최초 실패한 GREEN도 보존했다. [상세 실행 기록](../quality/counseling-tdd-20261107/README.md)

## 의미 있는 검증과 최종 결과

| 대상 | 결과 | 해석 |
|---|---|---|
| FastAPI 전체 | **603 passed** | 기존 단위·격리 PostgreSQL 통합 검사 포함 |
| React 전체 | **150 passed** | 타입 검사·Vite 배포 빌드 통과 |
| RN 전체 | **153 passed** | 타입 검사·Android production JS 번들 통과 |
| 브라우저 실제 연결 | **PASS** | React 배포 빌드 → FastAPI worker → 격리 PostgreSQL. 합성 제공자 사용 |
| 기존 테스트 보존 | backend 80 / web 51 / RN 31 파일 변경·삭제 0 | 이전 최신 커밋의 Git blob과 대조. 새 테스트만 추가 |
| 실제 GPT·부하 테스트 | **0회** | 이전 유료 평가와 이번 회귀 검증을 혼동하지 않음 |

웹과 RN은 공통 테스트를 각각 실행한다. 두 실행 건수를 합쳐 서로 다른 기능 수로 표시하지 않는다. 테스트 통과율을 실제 AI 상담 정확도나 임상 효과로 해석하지 않는다.

핵심 검증은 중복 없는 Python 추첨, 확인 전 추첨 금지, 실제 SQL RLS, 중복 요청 한 번 처리, 재시도 시 카드·검색 근거 유지, 동의 철회·삭제 중 결과 게시 차단, 경청에서 행동 제안 금지, 허위 출처·개인 URL 거절, 계정 전환 후 응답 차단, 불확실한 요청의 동일 키 재시도다.

## 구조와 코드

```text
React / RN 화면
  → CounselingController (입력·전송·재시도·계정 경계)
  → CounselingApi (요청 필드 선택·응답 검증)
  → FetchClient
  → FastAPI HTTP adapter
  → CounselingService
  → domain rules + repository/model ports
  → PostgreSQL / OpenAI Responses adapter
```

화면에서 직접 요청·재시도를 조합하던 동작을 독립 컨트롤러로 옮겼다. 입력이 있는 상태에서 전송 결과가 불확실해도 원래 요청 키·본문을 유지하며, 그 사이 새로 쓴 글은 성공 응답이 와도 지우지 않는다.

```ts
// execute(): 같은 시도의 응답만 게시하고 이후에 편집한 글은 보존한다.
if (!this.fence.valid(epoch)) return false;
this.attempt = undefined;
this.publish(value);
const clear = attempt.kind === 'message'
  && this.state.text.trim() === attempt.payload.text;
this.patch({pending: false, ...(clear ? {text: ''} : {})});
```

백엔드는 메시지 분석·카드 추첨·근거 검색·조언 생성 단계를 구분하고, 외부 호출 동안 DB 트랜잭션을 유지하지 않는다. 생성 전후에 현재 동의·소유자·사전 버전을 확인한다. RAG는 기존과 같은 본인 사전 키워드 검색·최근 대화·서버 추첨 카드 의미를 사용하며 임베딩 검색으로 표시하지 않는다.

GPT 모델 설정은 유지했다. 프롬프트 버전은 `esc-strategy-tdd-20261107-v2`이며, 서버 소유 정책과 사용자/RAG 데이터를 분리한다. Structured Outputs의 스키마 일치와 애플리케이션의 카드·출처·대화 모드 검증을 함께 적용했다. [OpenAI Docs](https://developers.openai.com/api/docs/guides/structured-outputs/)

## 문서·성능 수치의 적용 범위

- 기존 [실제 GPT A/B 보고서](ab-20261107/README.md)·4쪽 PDF와 13쪽 포트폴리오 PDF는 **이전 구현**의 자료로 보존했다.
- 이전 48회 평가의 품질·속도·비용 수치를 이번 재구현 성능으로 재사용하지 않는다. 이번에는 유료 호출·새 A/B·학습을 수행하지 않았다.
- 이번 변경에서 확인한 성능 관련 사항은 요청 중복 방지·명시적 재시도·빌드 성공이다. 새 FPS·실기기 응답시간·실제 모델 상담 품질의 개선 수치는 없다.
- 웹 번들에는 기존 500kB 초과 경고가 남아 있다. RN 검증은 Android JS 번들이며 새 기기 설치·iOS 실행 검증으로 표시하지 않는다.
- 무료 공개 배포·운영 AI 설정·main 병합·스토어 출시는 이번 작업에 포함하지 않았다.

## 증거와 Git

- [이 저장소 실행 증거](../quality/counseling-tdd-20261107/)
- [기존 테스트 보존 대조](../quality/counseling-tdd-20261107/existing-tests-audit.json)
- [실제 웹 화면·흐름 확인](https://github.com/tarobo22/taro-raect/tree/codex/counseling-tdd-20261107/docs/quality/counseling-tdd-20261107)
- [FastAPI CI](https://github.com/tarobo22/taro-back/actions/runs/42283031404)
- [React CI](https://github.com/tarobo22/taro-raect/actions/runs/42285342386)
- [RN CI](https://github.com/tarobo22/taro/actions/runs/42285359839)
- [백엔드 브랜치](https://github.com/tarobo22/taro-back/tree/codex/counseling-tdd-20261107)
- [React 브랜치](https://github.com/tarobo22/taro-raect/tree/codex/counseling-tdd-20261107)
- [RN 브랜치](https://github.com/tarobo22/taro/tree/codex/counseling-tdd-20261107)

실제 변경 파일은 각 저장소의 `docs/quality/counseling-tdd-20261107/changed-files.txt`에 기록한다. 기존 기능·에셋·테스트의 요청 외 변경은 하지 않았다.
