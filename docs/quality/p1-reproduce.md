# P1 검증 재현

기본 서버 대신 명시적으로 실행하는 모의 제공자 fixture다. 실제 GPT·전사 품질을 검증하지 않는다. 별도 로컬 DB 이름은 `_test`로 끝나야 하며 일반 개발 DB·사용자 데이터에는 실행하지 않는다.

## 백엔드

`tools/test.ps1`의 테스트 DB를 준비하고, `DATABASE_URL`과 `YEOUN_TEST_DATABASE_URL`을 같은 격리 테스트 DSN으로 설정한다. 실제 키는 사용하지 않는다. 아래 명령은 taro-back에서 실행한다.

```powershell
$env:OPENAI_API_KEY = ''
python -m api.migrate
python -m pytest api/tests -q --basetemp=.local/p1-test-temp
python -m uvicorn api.tests.support.preview_api:application --factory --host 127.0.0.1 --port 8828 --no-access-log
```

fixture는 실 PostgreSQL, 실제 composition·worker·내보내기·삭제를 사용하며 결과/전사만 모의로 제공한다. 소유권·삭제·동의 등 제품 판단을 모의로 통과시키지 않는다. HTTP 응답에 키·기록 본문 진단 로그를 출력하지 않는다.

## 웹 통합

다른 터미널의 taro-raect에서 실행한다. `npm ci`는 의존성 설치가 필요한 최초 한 번만 실행하고, preview가 켜진 상태에서 재설치하지 않는다.

```powershell
$env:VITE_API_PROXY = 'http://127.0.0.1:8828'
npm run build
npm exec vite preview -- --host 127.0.0.1 --port 8827 --strictPort
```

또 다른 taro-raect 터미널:

```powershell
$env:YEOUN_WEB_URL = 'http://127.0.0.1:8827'
$env:YEOUN_API_URL = 'http://127.0.0.1:8828'
$env:YEOUN_TEST_ARTIFACTS = '.local/p1-real-integration'
node --test tests/e2e/p0-integration.test.mjs tests/e2e/p1-integration.test.mjs
```

브라우저는 Playwright의 테스트용 Chromium을 사용한다. 신규 소유자만 만들고 해당 테스트가 만든 소유자에만 제품의 전체 삭제 API를 호출해 정리한다. 기존 에뮬레이터 앱이나 과거 테스트 소유자의 데이터를 직접 지우지 않는다. 다른 fixture 브라우저 검사는 `tests/e2e/*flow.test.mjs`에서 독립적으로 실행한다.

## RN / Android

각 프론트 저장소의 `npm test`, `npm run typecheck`를 실행한다. Android는 taro의 개발 환경을 로드한 뒤:

```powershell
cd android
./gradlew.bat :app:testReleaseUnitTest :app:assembleRelease '-PreactNativeArchitectures=x86_64' '-PyeounApplicationId=com.yeoun.app.p1verify'
```

별도 API 35 에뮬레이터에 이 테스트 패키지를 설치하고 로그인 전 `기기 알림 설정`에서 확인한다. 기본 꺼짐 → OS 거부 → 예약 0 → 허용 → 예약 1 → 시간 변경 → 예약 1 → 미리 보기 → 끄기 → 예약 0 순서다. `tools/verify-reminder-resume.py`는 별도 테스트 앱에서 직접 켠 설정이 있을 때만 강제 종료·재진입을 확인하며 데이터를 지우지 않는다.

실제 다음 날의 inexact alarm 배송, 실기기 FPS·마이크·햅틱·한국어 제공자 품질, 운영 OTA·스토어 서명·iOS 검증은 별도로 수행한다.
