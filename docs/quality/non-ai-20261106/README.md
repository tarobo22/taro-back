# 비AI 서버 계약·데이터 추적

확인일: 2026-11-06. 현재 Android·웹 비AI 구현과 로컬/원격 검증 결과다. 실제 OAuth·실과금·iOS native·운영 배포는 별도다. AI 처리 로직·모델·프롬프트 추가 개발과 실제 모델 호출은 이번 범위에서 제외했다.

[Notion 비공개 인계 초안](https://app.notion.com/p/410b37c6fe7281249a4dfa62b85a3c46?pvs=204) · [RN 보고서](https://github.com/tarobo22/taro/tree/codex/complete-non-ai-20261106/docs/reports/non-ai-20261106) · [Backend 계약](https://github.com/tarobo22/taro-back/tree/codex/complete-non-ai-20261106/docs/quality/non-ai-20261106) · [Web QA](https://github.com/tarobo22/taro-raect/tree/codex/complete-non-ai-20261106/docs/quality/non-ai-20261106)

단위: Back532 / RN133 / Web130, 브라우저39, Android JVM6 통과. 세 저장소 GitHub CI도 성공했다. 기존 baseline 테스트 수정·삭제·skip0. 같은 공유 규칙 검증을 품질 점수나 사용자 성능으로 합산하지 않는다.

## 계약과 데이터

- [실제 OpenAPI](actual-openapi.json): 81paths / 99operations(83v2·15v1·health1) / 119schemas.
- [전체 operation](api-operation-inventory.csv), [인증](api-auth-inventory.csv), [오류](api-error-inventory.csv).
- [런타임 인증·오류 보충](runtime-auth-error-contract.md): 자동 생성 schema의 optional Header를 public API로 해석하지 않는다. 일부422 schema와 실제 envelope 차이를 함께 읽는다.
- [물리 ERD](physical-erd.md), [원본 Mermaid](schema-erd.mmd), [컬럼·제약 메타데이터](schema-metadata.json).
- [비AI 46개](non-ai-requirement-evidence.csv), [전체 68개](requirements-68-classification.csv), [기존 테스트 보전](test-preservation.json), [문서 포함 최종 경로](all-changed-files.csv).

새 도메인은 social auth / billing / story / admin이며 migrations0021~0024를 사용한다. 소유권·멱등·CAS·export/erase 수명주기를 공유한다. 관리자 권한은 기본0명이며 명시적 CLI 지정이 필요하다. 보관기간 None은 미정이며 30일 승인으로 해석하지 않는다.

source-changed-files.csv는 보고서 파일을 더하기 전 source/CI 계약 snapshot이다. all-changed-files.csv가 새 문서까지 포함한 목록이다. 기존 문서387개 중386개는 같은 바이트이고 docs/contracts/openapi.json 한 건은50→81paths의 의도된 생성 갱신이다.

운영 활성화에는 공급자·스토어 설정, HTTPS·비밀 런타임 주입, DB 백업/복구 리허설·관측·권리 승인·보관 정책이 남아 있다. 실제 credential이나 사용자 원문은 이 폴더에 저장하지 않았다.
