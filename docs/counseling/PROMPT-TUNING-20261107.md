# 챕터 3 · AI 프롬프트 튜닝

여운의 마음 대화에서 이해 확인 뒤 답변이 사연과 상담 모드를 지키도록 프롬프트를 조정했다. 이전 실제 GPT 평가와 이번 후보의 검증 상태를 따로 기록한다.

| 지표 | 이전 실측 전 | 이전 실측 후 | 새 v3 후보 | 합격선 |
|---|---:|---:|---|---:|
| 이해 확인 평균 | 82.08점 | 83.33점 | 실측 대기 | 참고 지표 |
| 최종 답변 평균 | 66.67점 | 74.58점 | 실측 대기 | ≥90점 |
| 최종 90점 이상 | 0/12 | 0/12 | 실측 대기 | ≥45/50 |
| 전체 응답 형식 통과 | 23/24 | 24/24 | 실측 대기 | 형식 통과율 별도 공개 |
| 기존 상담 단위 검사 | - | - | 39/39 | 제품 코드 회귀 확인 |
| 최종 응답 평균 시간 | 7.15초 | 8.29초 | 실측 대기 | 품질과 함께 보고 |
| 최종 응답 p95 | 9.84초 | 11.90초 | 실측 대기 | 품질과 함께 보고 |
| 두 단계 24호출 비용 추정 | US$0.03776 | US$0.03713 | 실측 대기 | 사용량과 함께 보고 |
| 실측 요청 수 | 24 | 24 | 0 | 최대 220회 비교 |

이전 비교는 합성 상담 12건, 단계당 한 번 생성, 같은 모델·RAG·카드 조건에서 총 48회 실행했다. 프롬프트 작성자인 Codex가 응답 순서를 가린 상태로 채점했다. 비용은 당시 토큰과 캐시를 반영한 추정치다. 현재 TDD 재구현본 v2와 v3 후보는 새 합격 세트에서 아직 측정하지 않았다. [이전 A/B 원본과 평가 기준](ab-20261107/README.md)

## 실제 점수가 빠진 곳

| 최종 답변 항목 | 전 → 후, 4점 만점 | 관찰과 v3 조정 |
|---|---:|---|
| 관련성·간결함 | 2.00 → 2.58 | 앞서 실패한 방법의 이유를 놓치고 시간 관리 과제를 늘렸다. 한 가지 행동을 `actions`에만 쓰고 나머지 필드에는 이유와 관점을 나눴다. |
| 경청·선택권 | 2.25 → 2.75 | 행동 배열은 한 개여도 본문과 카드에 추가 숙제가 남았다. 경청은 질문·과제 없이 두 문장으로, 조언은 사용자가 밝힌 여건에 맞는 한 가지 선택으로 제한했다. |
| 근거·불확실성 | 2.75 → 2.75 | 주말을 오늘로 바꾸고 습관을 추정했다. 최신 정정, 행위자·시점, 확인한 사실을 우선하도록 구체화했다. |
| 감정 반영 | 3.08 → 3.17 | 복합 감정은 짚었으나 합격한 일을 사과하도록 이끌었다. 감정과 실제 상황을 한 문장으로 반영하도록 했다. |
| 사연 이해 | 3.25 → 3.67 | 개인사전 내용을 이번 발화의 인용처럼 취급한 사례가 있었다. 현재 발화와 저장된 정의의 출처를 구분한다. |

이해 확인의 `acknowledgement`는 감정 한 문장이다. 실제로 모르는 정보만 `clarification_question`에서 묻고, 사용자가 확인하면 경청 또는 카드·조언 전략을 적용한다. 최종 답변에서는 공감, 카드의 관점, 제안 이유, 한 가지 행동의 역할을 분리했다. 이 제약은 프롬프트가 안내하며, 문장 전체의 사실 일치와 중복 과제는 별도 채점 항목으로 확인한다.

**설계 예시**

> 사용자 “부담돼요. 발표는 선배가 하고, 저는 다음 달 첫 모임에서 자료만 나눠줘요.”
>
> 이해 확인 “발표는 선배가 하고, 다음 달 첫 모임에서 자료를 나눠주는 일이 부담스러운 거죠?”

이 문장은 설계를 보여주는 예시다. 저장된 모델 응답은 [이전 A/B 보고서](ab-20261107/README.md)에서 확인할 수 있다.

## 논문과 적용 범위

| 근거 | 여운에 적용한 부분 |
|---|---|
| [ESConv · ACL 2021](https://aclanthology.org/2021.acl-long.269/) | 탐색·경청·행동에 필요한 전략을 나눠 프롬프트를 구성했다. |
| [전략 편향 연구 · ACL 2024](https://aclanthology.org/2024.acl-long.813/) | 조언으로 치우치는지 평가하고, 앱에서 정해진 단계에 맞는 전략을 모델에 전달한다. |
| [ESC-Eval · EMNLP 2024](https://aclanthology.org/2024.emnlp-main.883/) | 이전 모델 답과 다음 사용자 정정을 이어서 넣고, 사람 두 명이 답변의 적절성과 자연스러움을 평가하도록 설계했다. |
| [Self-RAG · ICLR 2024](https://arxiv.org/abs/2310.11511) | 필요한 근거만 찾는 원칙을 참고했다. 이전에 저장한 개인사전과 현재 사용자 발화는 다른 출처다. |
| [TEA-Bench · ACL 2026](https://aclanthology.org/2026.acl-long.2152/) | 익숙한 상황에서의 향상과 새 상황에서의 일반화를 나눠 보도록 검증 세트를 분리했다. |
| [정서 지원의 안전 실패 · FAccT 2025](https://arxiv.org/abs/2504.18412) | 위기 신호 누락과 근거 없는 확정을 평균 품질과 별도로 확인한다. |
| [OpenAI Docs · Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices) | 새 평가 데이터와 고정 기준을 두고 자동 검사와 사람 평가를 결합한다. |

이번 변경은 텍스트 지침을 조정한 프롬프트 튜닝이다. 코드 검증은 기존 상담 단위 테스트 39건과 평가 실행기의 모의 요청 7건을 사용했다. 후보 프롬프트 커밋은 `e44479c21e77299e0b94e3a91443d83f579a33c8`다.

## 90점 합격 판정

- 합성 한국어 50건을 경청, 복합 감정, 정정, 개인사전, 이전 조언 실패로 10건씩 구성했다. 프롬프트 작성 이후 고정했고 기존 12건과 구분한다. 정정 사례 10건은 후보가 실제로 만든 이해 확인 문장에 이어 두 번째 사용자 발화를 넣는다.
- TDD v2 기준선과 v3를 같은 모델 스냅샷, 카드 ID·순서, 데이터로 순차 실행한다. 두 버전 합계 최대 220회다. 실행기는 공급자와 연결된 격리 로컬 Responses 릴레이를 사용한다.
- 평가는 두 사람이 X/Y를 독립 채점한다. 감정·이해·선택권·근거·관련성 각 0~4점의 합에 5를 곱한다. 답변이 나오지 않은 사례도 분모에 남긴다.
- 최종 평균 90점 이상, 90점 이상 응답 45/50 이상, 중대 오류 0건을 모두 요구한다. 위기·타 계정 데이터는 별도 안전 및 소유권 회귀도 확인한다.
- 실제 GPT 연결과 독립 채점이 완료되면 이 표에 실측값과 재현 링크를 추가한다.

| 자료 | 링크 |
|---|---|
| 변경 프롬프트 | [전략](../../api/adapters/counseling_strategy.py) · [OpenAI 어댑터](../../api/adapters/counseling_openai.py) |
| 고정 입력 | [50개 평가 사례](../../evals/prompt-tuning-holdout-20261107.json) |
| 재현 도구 | [평가 실행기](../../tools/evaluate_counseling_prompt_v3.py) |
| 수치 원본 | [CSV](prompt-tuning-20261107/performance.csv) |
| 최신 포트폴리오 | [챕터 3·4 통합 PDF](../portfolio/yeoun-development-portfolio-chapters-3-4.pdf) |

### 재현 순서

`plan`은 사례 해시·그룹·요청 상한을 출력한다. `capture`는 검증된 로컬 릴레이와 정확한 소스 worktree를 지정해 두 팔을 각각 기록한다. `blind`가 만든 리뷰용 파일만 평가자에게 제공하고 매핑은 결과 제출 뒤에 사용한다. `score`는 제출한 CSV를 검증하고 점수를 합산한다.

~~~powershell
python tools/evaluate_counseling_prompt_v3.py plan
python tools/evaluate_counseling_prompt_v3.py capture --arm baseline --source $baselineSource --relay $evalRelay --allow-paid --out .local/v2.json
python tools/evaluate_counseling_prompt_v3.py capture --arm candidate --source $candidateSource --relay $evalRelay --allow-paid --out .local/v3.json
python tools/evaluate_counseling_prompt_v3.py blind --baseline .local/v2.json --candidate .local/v3.json --out .local/review.json --mapping .local/mapping.json
python tools/evaluate_counseling_prompt_v3.py score --review .local/review.json --mapping .local/mapping.json --ratings .local/ratings.csv --out .local/quality.json
~~~

채점 CSV 열은 `case_id,candidate,reviewer,empathy,context,autonomy,grounding,usefulness,critical_error`다. `candidate`에는 X 또는 Y를, `critical_error`에는 0 또는 1을 기록한다.
