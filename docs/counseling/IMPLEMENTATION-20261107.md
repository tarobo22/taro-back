# 여운 · 타로 상담 구현·검증
기준일: 2026-11-07

## 상담 기준
**짧은 공감 → 부드러운 의미 확인 → 사용자 입장 확인 → 카드와 짧은 조언 → 작은 행동.**

“그것 때문에 너무 화나.”에는 “화가 많이 나셨겠어요. 괜찮으시면, 어떤 일이 있었는지 조금 들려주실래요?”처럼 감정을 먼저 받아준다.

일반 정보 조회는 상담으로 부드럽게 돌린다. 장학금·제도·일정 조회를 정상 상담 사례로 쓰지 않는다. 일상적인 고민에는 웹 검색하지 않으며, 낯선 상담 표현에 한해 확인 후 최대 한 번 참고한다. 참고 자료는 필요할 때 펼쳐보고 공감·카드·조언을 먼저 읽는다.

## 연결한 기능
| 영역 | 구현 |
|---|---|
| 상담 | 공감, 의미 질문, 맞아요/조금 달라요, 정정, 듣기 모드, 이어가기 |
| 타로 | Python SystemRandom으로 1/3장 추첨, 중복 방지, 추첨 ID 저장, 실패 후 같은 카드 유지 |
| 개인사전 RAG | 본인 용어 정확 검색, 명시한 의미만 기억, 수정·삭제·대화 한정 기억 |
| 근거 | 실제 검색 URL과 제공한 출처 ID만 허용, 카드 해석과 외부 사실 구분 |
| 화면 | 상단 카드 고정, 카드 선택, 짧은 말풍선, 텍스트·기기 음성·검토한 클라우드 전사 |
| 초안 | 계정별 메모리, 화면 이동 후 유지, 계정 전환·해제 시 제거 |
| 보안 | 서버 세션으로 사용자 결정, owner_id 입력 거부, DB RLS, 제한된 DB 역할 |
| 처리 | 비동기 작업, 같은 요청 중복 방지, 동의·사전 변경 재확인 |
| 삭제 | 전체 삭제에 대화·작업·사전 포함, 상담 작업을 동의 이벤트보다 먼저 삭제 |

개인 용어와 현재 대화·카드 의미를 가져와 답변을 구성하는 RAG다. 별도의 의미 임베딩 검색과 공용 상담 벡터 저장소는 이번 구현에 포함하지 않았다.

## 검증
- 백엔드 최종 원격 CI **579 passed**. 상담 기능 관련 **38건도 격리 DB에서 재검증 통과**.
- React **133 passed**, TypeScript·배포 빌드 통과.
- RN **136 passed**, TypeScript·Android JS 번들 생성 통과.
- 신규 검증의 의도된 RED 이후 구현했다. 기존 베이스라인 테스트의 삭제·완화·skip은 없다.
- 전체 삭제의 FK 의존성 오류를 신규 검증으로 재현하고 삭제 순서를 고쳤다.
- 공유 테스트 DB의 누적 200회 한도 때문에 막혔던 회귀 검사는 새 격리 DB에서 통과했다.
- [50개 유즈케이스](USE-CASES-50.md), [JSON](../../evals/counseling-cases.json).

### Git 반영과 원격 검증

세 저장소의 `codex/counseling-rag-20261107` 브랜치에 기능별로 커밋·푸시했다. main 병합과 이력 강제 변경은 하지 않았다.

| 저장소 | 원격 검증 | 실제 변경 파일 |
|---|---|---|
| taro-back | [FastAPI 전체 검사 통과](https://github.com/tarobo22/taro-back/actions/runs/42264732339) | [목록](CHANGES.md) |
| taro-raect | [타입·테스트·웹 빌드 통과](https://github.com/tarobo22/taro-raect/actions/runs/42264741782) | [목록](https://github.com/tarobo22/taro-raect/blob/codex/counseling-rag-20261107/docs/counseling/CHANGES.md) |
| taro | [타입·테스트·Android JS 번들 통과](https://github.com/tarobo22/taro/actions/runs/42264742288) | [목록](https://github.com/tarobo22/taro/blob/codex/counseling-rag-20261107/docs/counseling/CHANGES.md) |

별도 [iOS 시뮬레이터 CI](https://github.com/tarobo22/taro/actions/runs/42264742248)는 확인 시점 실행 대기 상태다. iOS 네이티브 검증 완료로 표기하지 않는다. 원본 작업 폴더에 있던 별도 App Store 변경은 보존했다.

## 로컬 부하 측정
**GPT 성능 수치가 아니다.** 실제 PostgreSQL·애플리케이션에 10ms 고정 합성 제공자를 연결했다. OpenAI 호출·실제 웹 검색은 0건이다. DB 풀 최대 12, 측정용 이용 한도를 사용했다.

| 동시 사용자 | 표본 | p50 | p95 | p99 |
|---|---|---|---|---|
| 1 | 50 | 190.59ms | 242.61ms | 252.44ms |
| 5 | 50 | 218.49ms | 256.69ms | 266.01ms |
| 10 | 50 | 414.79ms | 494.99ms | 537.31ms |

[원시 결과](local-benchmark.json). 네트워크·실제 GPT·사람의 확인 시간은 포함하지 않는다.

## 실제 AI 테스트
현재 서버 AI 연결이 미설정이다. 평가 도구가 이를 확인하고 **유료 호출 0건**으로 중단했다. [상태 기록](live-evaluation-status.json)

기본 상담 모델은 gpt-5-mini이며 환경설정으로 바꿀 수 있다. reasoning effort low, 이해 확인 2000 / 답변 2500 / 드문 용어 참고 900 출력 토큰 상한을 설정했다. 실제 속도 개선 수치로 주장하지 않는다.

설정은 shared/counseling-runtime.json에서 관리한다. 서버는 비밀 환경에서 OPENAI_API_KEY를 받아야 실제 호출할 수 있다. 이번 작업에서 키를 읽거나 등록하지 않았다.

[공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-5-mini)에서 Responses·web search·structured outputs 지원을 확인했다.

준비 상태 확인:
~~~powershell
python tools/evaluate_counseling.py --base-url http://127.0.0.1:8818 --limit 50
~~~
설정 후 유료 평가:
~~~powershell
python tools/evaluate_counseling.py --base-url http://127.0.0.1:8818 --allow-paid --finish-confirmed --limit 50 --concurrency 1
~~~
보안·클라이언트·부하 사례는 해당 경로에서 따로 검증한다. 문장 품질은 구조 검사 통과만으로 합격 처리하지 않는다.

## API와 실행
[OpenAPI](../contracts/openapi.json): 세션 생성·조회·삭제, 메시지, 의미 확인, 사전 조회·추가·수정·삭제.

- 로컬 API: http://127.0.0.1:8818
- 배포 빌드 미리보기: http://127.0.0.1:8847
- 키가 없는 상태를 정확히 표시하며 실제 GPT가 답했다고 꾸미지 않는다.

## 남은 확인과 제한
- 실제 API 키 등록, 실제 GPT 50건 평가·문장 품질·응답 속도 측정은 미완료.
- 화면 검수 이미지는 합성 응답이며 실제 GPT 상담 기록이 아니다.
- RN은 Android JS 번들까지 확인했다. 이번 변경의 실기기·iOS 네이티브 실행은 측정하지 않았다.
- Vite 개발 모드의 기존 RN Flow 의존성 최적화 오류가 있어 검수에는 배포 빌드 미리보기를 사용했다. 관련 의존성 설정은 수정하지 않았다.
- 큰 JS 번들 경고와 테스트 의존성 경고가 남아 있다.
- 운영 배포·스토어 출시는 이번 완료 범위가 아니다.

재시도에서 이미 확인한 용어 자료를 다시 검색하지 않도록, 개인 범위 캐시를 답변 생성 전에 저장한다. 같은 사용자의 10분 이내 자료만 재사용한다.
