# 격리 환경의 실물 로봇 이벤트 재동기화 실험

이 기능은 단절 뒤 **같은 로봇 프로세스·세션에서 정지 지점의 이벤트 원본을
서버 장부와 대조하고 서명된 확인을 기록**한다. 실물 명령이나 점유 해제,
주행 재개를 허가하지 않는다. 로봇 재시작·세션 변경, 경로 중간 정지,
집기·적재 결과 불명, 이벤트 원본 유실이나 불일치는 보류한다.

팀 로봇 구현은 `feature/robot-recovery` 브랜치의 `agv/RECONCILE_V1.md`를
참고한다. `feature/field-run` 원본에는 아직 합쳐지지 않았다. 서버 구현은
`feature/physical-review-safeguards`에 있으며 `feature/server-communication`의
기존 운용 경로에 병합되지 않았다.

로봇별 별도 비밀 키와 HMAC, 서버 nonce·epoch·로봇 nonce·세션·순번·명령 및
이벤트 digest를 검증한다. 서버는 기존 gate·점유·inbox를 검사하고 이벤트
원본을 별도 비실행 확인 장부에만 기록한다. 기존 이벤트를 재발행하거나 inbox의
작업 진행을 호출하지 않는다. inbox에서 이벤트가 보이지 않는 것은 미수신의
증거가 아니며 `not_observed`로 표시한다. 확인 ACK는 수신 시점의 서명 교환
기록일 뿐 로봇의 현재 연결·물리적 정지·위치 또는 서버 운행 승인이 아니다.

현재 일반 `main`/`SimRuntime.start()`의 physical 실행 차단은 유지한다. 이를
우회해 기존 DB·실물 브로커를 쓰는 실행법이 아니다. 로봇 측의 opt-in 설정은
`AGV_RECONCILE_ISOLATED=1`과 로봇별 비밀 `AGV_RECONCILE_KEY_HEX`이며,
로컬 브로커 전용이다. 실제 키를 소스·문서·로그에 남기지 않는다.

## 독립 재현

서버 저장소의 Python 환경과 별도 로봇 브랜치 체크아웃, 로컬 Mosquitto가
필요하다. 아래 명령은 테스트가 소유하는 새 임시 DB와 loopback 브로커를 만들고,
팀 `MqttLink`·`LinkController`와 서버 gate·inbox·watcher의 서명 교환을 실행한다.
테스트의 runner·위치 상태는 합성 대역이다. 브로커를 중단·재기동하고 같은 로봇
세션에서 재접속한 뒤 증거 기록·기존 점유·명령 잠금을 확인한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -W error::ResourceWarning -m server.tools.isolated_reconcile_demo `
  --team-repo 'D:\팀-로봇-체크아웃' `
  --mosquitto 'C:\Program Files\mosquitto\mosquitto.exe'
```

이것은 격리 재현용 명령이다. 운영자가 제공한 DB를 읽지 않고 기존 서버에
연결하지 않는다. 실물 장비별 인증·TLS·ACL, 지연된 과거 주문을 구별할 dispatch
세대 계약, 결과 불명 후 안전한 운행 재개 및 실제 제동·후단 이탈 검증이 남았다.
