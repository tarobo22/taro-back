# 여운 GitHub CI/CD

## push 이후 흐름

| 저장소 | 검증 | main 성공 후 |
|---|---|---|
| taro-raect | 타입·기존 동작 테스트·Vite 빌드 | 정적 파일 릴리스 → 웹 교체 → 공개 URL 확인 |
| taro-back | PostgreSQL 마이그레이션·전체 pytest·OpenAPI 차이·배포 검사·Docker 빌드 | 이미지 릴리스 → 서버 사전 검증 → API 교체 → 공개 URL 확인 |
| taro | 타입·동작·JS 번들·Android 네이티브 테스트·APK 빌드 | 전용 서명·검증 → APK GitHub Release |
| taro / iOS | Swift·시뮬레이터 빌드·설치·실행·딥링크 | 앱·화면·로그를 Actions artifact로 보관 |

작업 브랜치/PR은 검사만 한다. `main`의 검증에 성공한 동일 커밋만 배포한다. CI 실패 시 배포 작업은 실행되지 않는다. 기존 테스트를 삭제·완화·skip하지 않았다.

릴리스 직전에 현재 `main`과 빌드 커밋을 비교한다. 오래된 빌드는 artifact로만 남기고 배포하지 않는다. 업로드 중 `main`이 바뀌면 draft를 공개하지 않는다. 최신 커밋의 실제 업로드 오류는 실패로 처리한다.

문서 전용 커밋은 빌드를 생략한다. 빌드 도중 문서만 추가됐다면 원래 검증한 코드의 배포를 계속한다. 코드가 변경되거나 GitHub 비교 결과가 불완전하면 게시하지 않는다.

## 서버 배포

GitHub가 이미지를 빌드하므로 1 GB 서버에서 컴파일하지 않는다. 서버의 `yeoun-deploy.timer`가 1분 간격으로 지정된 두 공개 저장소의 최신 릴리스 manifest를 조회한다. GitHub runner에 SSH·AWS 키를 주거나 서버 SSH를 추가 공개하지 않는다. manifest와 아카이브는 draft 릴리스에 먼저 올린 뒤 함께 공개한다.

- 저장소·커밋·릴리스 태그·파일명·크기·SHA-256을 검사한다.
- 웹은 안전하게 압축을 푼 뒤 `www/current` 링크를 교체한다. 이전 페이지의 해시 에셋도 보존한다.
- API는 기존 DB를 서버 내부에 백업하고 트랜잭션 마이그레이션을 실행한다. 작업 처리를 끈 임시 컨테이너에서 DB health를 확인한 뒤 API만 교체한다.
- 사전 검사 실패 시 새 API로 전환하지 않는다. DB와 이미지의 자동 롤백은 하지 않는다. 새 마이그레이션은 기존 앱과 호환되는 추가 방식이어야 한다.
- 단일 API 컨테이너 교체 중 짧은 중단이 생길 수 있다. 무중단/고가용성 배포는 아니다.
- GitHub Actions는 공개 `/deployment-status.json`의 릴리스 태그와 `/api/health`를 확인해야 성공 처리한다. 릴리스 업로드 성공만으로 배포 완료라고 표시하지 않는다.

GPT·DB 키는 기존 서버의 root 전용 환경파일에만 둔다. Android 전용 서명키만 사용자 승인에 따라 GitHub Secret에 보관한다. 딜러허브 자원은 사용하지 않는다.

## 설치·운영

이미 배포된 여운 서버에 `deploy/lightsail/`의 CI 설치 파일을 별도 staging 폴더로 전송한 뒤 `sudo sh <staging>/install-ci.sh`를 한 번 실행한다. 이 스크립트는 최초 웹을 보존하고 Caddy 설정과 배포 타이머만 설치한다. 최초 인프라 배포는 `README.md`를 따른다.

자동 배포 대상은 웹 산출물과 API 이미지다. 배포 에이전트·Compose·Caddy 설정을 변경할 때는 해당 파일을 검토해 서버에 다시 설치해야 한다. AWS 인프라 생성·변경을 자동 실행하는 파이프라인은 아니다.

```sh
sudo systemctl status yeoun-deploy.timer
sudo journalctl -u yeoun-deploy.service --since '30 minutes ago'
sudo systemctl start yeoun-deploy.service
```

공개 배포 상태에는 커밋·태그·시각·오류 유형만 포함한다. 서버의 `/var/lib/yeoun-deploy`에는 다운로드 및 DB 백업이 남는다. 디스크 여유가 부족하면 새 배포를 중단하므로 운영자가 보관 기간에 맞춰 여운 파일만 정리해야 한다. Docker 볼륨 삭제는 배포 절차에 없다.

## 검증 범위

새 배포 검사 8개는 구현 전 실패를 확인한 후 통과시켰다: 다른 저장소·다른 커밋·손상 파일·경로 이탈·심볼릭 링크·진입 파일 누락 거절, 정상 manifest·정적 파일 수용. 릴리스 파일 크기·해시, 오래된 코드 게시 방지, 문서 변경 시 정상 게시, 최신 빌드의 실제 오류 전파 검사를 추가해 현재 배포 검사는 12개다. 기존 제품 테스트는 유지했다. 배포 확인은 공개 URL과 DB health 기준이며, 매 배포마다 유료 GPT를 호출하거나 부하 테스트를 수행하지 않는다.

- [공개 앱](https://yeoun-43-201-209-249.sslip.io/)
- [배포된 커밋](https://yeoun-43-201-209-249.sslip.io/deployment-status.json)
- [서버 Actions](https://github.com/tarobo22/taro-back/actions/workflows/checks.yml)
- [웹 Actions](https://github.com/tarobo22/taro-raect/actions/workflows/checks.yml)
- [앱 Actions](https://github.com/tarobo22/taro/actions)

Android는 APK 다운로드 배포, iOS는 시뮬레이터 artifact까지다. 스토어 등록과 OTA 서버 활성화는 이번 구성에 포함되지 않는다. 새 AWS 유료 자원은 만들지 않는다. GitHub Actions 사용량과 GPT 사용료는 각 서비스 정책에 따른다.
