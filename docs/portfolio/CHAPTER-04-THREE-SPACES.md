# 챕터 4 · 세 공간 스킨 추가본

홈에서 카페, 마음 진료실, 고양이 놀이방을 고르면 캐릭터와 배경·프레임·메뉴·버튼이 함께 바뀐다. 카페는 연한 녹색과 편안한 셔츠, 진료실은 청회색과 짧은 옆가르마, 놀이방은 크림색 고양이와 쿠션·매트로 구분했다.

| 공간 | 화면과 반응 | 실제 화면 |
|---|---|---|
| 카페 | 차 한 잔을 곁에 둔 사람, 작은 인사 동작 | [카페 캡처](https://github.com/tarobo22/taro/blob/f729519dc47b7397d8f85d08543c5010593f137c/docs/quality/three-skins-20261107/native/android-cafe.png) |
| 진료실 | 단정한 옆가르마, 흰 가운과 잔잔한 배경 | [진료실 캡처](https://github.com/tarobo22/taro/blob/f729519dc47b7397d8f85d08543c5010593f137c/docs/quality/three-skins-20261107/native/android-clinic.png) |
| 놀이방 | 파스텔 놀이방 전체 스킨, 고양이 인사와 짧은 야옹 | [놀이방 캡처](https://github.com/tarobo22/taro/blob/f729519dc47b7397d8f85d08543c5010593f137c/docs/quality/three-skins-20261107/native/android-playroom.png) |

| 항목 | 확인한 범위 |
|---|---|
| 웹·RN | 웹 158건, RN 161건 통과. 선택 저장과 야옹 재생 제어에 기능 검증을 추가했다. |
| Android x86_64 | APK 서명·설치, 공간 전환, 연결 재시도, 앱을 다시 열었을 때 놀이방 복원 확인. |
| Android ARM64 | APK 서명과 패키징 확인. |
| iOS | Swift 6건, iPhone 16 시뮬레이터 빌드·설치·실행, 딥링크 열기 명령 확인. 캡처 범위는 연결 복구 화면이다. |
| 연결 복구 | 계정 상태가 준비되기 전 재시도 버튼에서 발생한 앱 종료를 고쳤다. 최종 iOS 화면에서 제목과 버튼이 안전 영역 안에 표시된다. |

- [4쪽 제작보고서와 검증 자료](https://github.com/tarobo22/taro-raect/tree/fb78188efc490e3784168dbb10a0bdabceaa3ea3/docs/quality/three-skins-20261107)
- [Android APK와 체크섬](https://github.com/tarobo22/taro/releases/tag/untagged-815d965dc371e2631e4c)
- [iOS 실행 기록](https://github.com/tarobo22/taro/actions/runs/42342416808) · [연결 복구 화면](https://github.com/tarobo22/taro/blob/f729519dc47b7397d8f85d08543c5010593f137c/docs/quality/three-skins-20261107/native/ios-launch.png)
- [60초 가로·세로 소개와 30초 사용법](https://github.com/tarobo22/taro-raect/tree/fb78188efc490e3784168dbb10a0bdabceaa3ea3/media/three-spaces-20261107/videos)
- [웹 코드](https://github.com/tarobo22/taro-raect/tree/fb78188efc490e3784168dbb10a0bdabceaa3ea3) · [RN 코드](https://github.com/tarobo22/taro/tree/f729519dc47b7397d8f85d08543c5010593f137c)

영상은 공간 선택 → 대화 입력 → 이해 확인 → 카드 뒤집기 → 기록 순서로 보여준다. 화면 확대, 터치 표시, 내레이션과 소리를 해당 동작에 맞췄다.
