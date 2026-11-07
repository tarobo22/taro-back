# 여운 이력서 데모 배포

서울 Lightsail `micro_3_0` (1 GB RAM, 40 GB SSD, 공인 IPv4 포함, 월 US$7)에 웹·FastAPI·PostgreSQL 17을 함께 실행한다. GPT 사용료·세금·전송량 초과는 별도다. 딜러허브의 서버·DB·도메인·네트워크 설정은 사용하거나 변경하지 않는다.

## 구성

브라우저 → Caddy HTTPS → React 정적 파일 / `/api/*` → FastAPI → PostgreSQL 및 OpenAI.

- 정적 파일은 개발 PC에서 빌드해 서버로 전송한다. 서버에서 Node 빌드는 하지 않는다.
- 외부 공개 포트는 80·443, SSH 22는 관리 PC의 `/32` 주소만 허용한다. DB와 API는 Docker 내부 네트워크에서만 접근한다.
- DB·음성 임시 저장소·HTTPS 인증서는 별도 Docker 볼륨으로 보존한다.
- 서버 재부팅 시 Docker와 서비스가 자동 시작된다. 마이그레이션은 배포 때 한 번 명시적으로 실행한다.
- 메모리 1 GB의 소규모 면접 시연용이다. 동시 AI 작업은 제한하고 2 GB swap을 설정한다. 고가용성 서비스나 부하 검증 완료 환경은 아니다.

## 비밀값

사용자의 2026-11-08 명시적 예외 승인에 따라 키는 SSH로 전송하고 `/etc/yeoun/`의 root 전용 파일(0600)에만 보관한다. Git·CloudFormation·배포 아카이브·로그에 넣지 않는다.

`api.env`: `DATABASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_COUNSELING_MODEL`, `OPENAI_TRANSCRIPTION_MODEL`, `CORS_ORIGINS`, `COUNSELING_WORKERS=1`, `OTA_UPDATES_ENABLED=false`.

`db.env`: `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD`.

`site.env`: `YEOUN_HOST` (공개 호스트명, 비밀값 아님).

## 실행 순서

1. `stack.json`을 cfn-lint와 CloudFormation으로 검사한다. 전용 공개 SSH 키를 부팅 스크립트로 설정하고 스택을 생성한다.
2. AWS가 반환한 전용 고정 IP에서 sslip.io 호스트명을 정한다. 최초 SSH 서버 공개키를 로컬에 고정하고 이후 변경되면 연결을 거절한다. 이번 신규 인스턴스의 AWS `hostKeys` 응답은 비어 있어 최초 접속 신뢰(TOFU)를 사용했다.
3. `npm run build` 결과와 `api/`, `shared/`, `deploy/lightsail/`만 새 서버 `/opt/yeoun/`에 전송한다. 기존 사용자 DB·기록은 옮기지 않는다.
4. 서버의 환경파일을 설정한다. 아래 명령은 `/opt/yeoun`에서 실행한다.

```sh
sudo docker compose -f deploy/lightsail/compose.yaml --project-directory . build api
sudo docker compose -f deploy/lightsail/compose.yaml --project-directory . up -d db
sudo docker compose -f deploy/lightsail/compose.yaml --project-directory . run --rm api python -m api.migrate
sudo docker compose -f deploy/lightsail/compose.yaml --project-directory . up -d
```

5. HTTPS·게스트·실제 GPT 답변·카드·기록 저장을 외부 브라우저에서 확인하고 공개 주소를 QR로 만든다.

업데이트는 같은 경로에 새 코드와 웹 빌드를 전송하고 위 순서를 반복한다. `down -v`는 데이터 삭제 명령이므로 사용하지 않는다. CloudFormation 삭제만으로 서버·고정 IP가 제거되지 않도록 Retain을 설정했다. 서비스 종료 시 별도 확인 후 해당 여운 자원만 정리해야 요금이 멈춘다.
