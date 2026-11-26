# 격리 환경의 실물 로봇 이벤트 재동기화 실험

이 기능은 단절 뒤 **같은 로봇 프로세스·세션에서 정지 지점의 이벤트 원본을
서버 장부와 대조하고 서명된 확인을 기록**한다. 실물 명령이나 점유 해제,
주행 재개를 허가하지 않는다. 로봇 재시작·세션 변경, 경로 중간 정지,
집기·적재 결과 불명, 이벤트 원본 유실이나 불일치는 보류한다.

팀 로봇 구현은 `feature/field-run` 커밋
`7e6e6bb741d35a490d8c14d56cb92d2544c4976b`에 병합됐다. 서버 구현은
`feature/physical-review-safeguards` 커밋
`ce5335736f75927cad4de02fe166de1c37ad7d1f`에 있으며
`feature/server-communication`의 기존 운용 경로에는 병합되지 않았다.
로봇 규약은 팀 브랜치의 `agv/RECONCILE_V1.md`를 참고한다.

서버·통신 브랜치는 위 검토 코드에서 **관찰 전용 부분만** 현재
`RecoveryGate`, 기존 SQLite 할당·점유 및 이벤트 inbox와 연결한다.
`server.physical_evidence_observer`의 명시적 격리 watcher와 읽기 전용
inbox 뷰, `server.physical_reconcile`의 서명 장부,
`server.physical_reconcile_mqtt`의 loopback MQTT 어댑터가 그 경계다.
이 구성은 수신된 같은 세션의 결과를 대조해 장부에 기록할 뿐 inbox 이벤트를
처리·재발행하거나 gate 차단과 점유를 해제하지 않는다. watcher를 종료하면
관찰을 무효화하고 작업 차단을 유지한다. watcher와 어댑터는 로컬
`127.0.0.1` 및 명시적 `isolated=True`를 요구하며 자동 재연결하지 않는다.
기존 서버 시작 경로나 웹 API는 이 실험을 자동으로 켜지 않는다.
별도 시험 호출자는 같은 DB의 `PhysicalEvidenceInbox`와 기존 `RecoveryGate`에
`PhysicalEvidenceWatcher`를 묶고, watcher가 연결된 상태에서
`isolated_gate_reconciliation(...)` 컨텍스트를 명시적으로 연다.
`bridge.request(...)`가 반환하는 nonce별 결과는 `bridge.result_for(nonce)`로
구분한다. `None`은 결과 불명이며 요청을 자동 재전송하거나 점유를 해제할
이유가 아니다. 어댑터는 한 수명 동안 최대 256개 요청만 허용한다.
서명 확인 행은 gate와 같은 DB의 별도 `physical_reconcile` 테이블에 기록되고,
기존 event inbox는 읽기만 한다. 테스트 키는 임시로 만들며 실제 키를 저장소나
로그에 남기지 않는다.

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

서버·통신 브랜치의 아래 도구는 두 확인된 Git 커밋을 임시 폴더에 각각
추출하고 팀 로봇 MQTT·컨트롤러와 서버의 격리된 재동기화 시험을 실행한다.
두 원격 추적 브랜치가 위 커밋과 다르면 자동으로 시작하지 않는다. 이 검사는
로컬에 있는 Git 참조만 확인하므로 먼저 `git fetch`로 최신 상태를 확인한다.
Mosquitto 2의 기본 localhost 전용 동작이 필요하고 새 임시 DB를 쓴다.

```powershell
$env:TEMP = (Resolve-Path .venv\pr59-test-temp).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m server.tools.physical_branch_compat_check `
  --mosquitto 'C:\Program Files\mosquitto\mosquitto.exe' `
  --temp-parent $env:TEMP
```

테스트는 브로커 중단과 같은 세션의 재접속, 위변조·기한 초과·세션 변경,
이벤트 중복, 서명된 이벤트 확인 뒤에도 기존 점유와 운행 차단이 유지되는지
확인한다. 서버의 일반 운용 경로가 통합됐거나 실물 재개가 허용된다는 뜻이
아니다. 새 Git 커밋은 본 검사 결과를 그대로 이어받지 않으며 코드를 다시
검토한 뒤 고정값과 시험을 갱신해야 한다.

현재 서버 코드에 연결한 관찰 경계는 다음 시험으로 별도 검증한다.
`AGV_FIELD_REPO`는 위 `7e6e6bb` 팀 소스의 독립 사본이어야 한다. 이 시험은
새 임시 DB와 로컬 Mosquitto를 소유하며 실제 장비에 연결하지 않는다.

```powershell
$env:AGV_FIELD_REPO = 'D:\경로\팀-field-run-독립-사본'
$env:MOSQUITTO_EXE = 'C:\Program Files\mosquitto\mosquitto.exe'
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest `
  server.tests.test_physical_evidence_observer `
  server.tests.test_physical_observation_exchange `
  server.tests.test_physical_observation_mqtt `
  server.tests.test_physical_observation_team_mqtt -v
```

팀 맵의 실물 배정·인계와 결과 불명 뒤 운행 재개는 연결하지 않았다.
특히 집기·적재의 불명 결과, 다른 세션·중간 위치, 실제 후단 이탈은 이
서명 장부가 해결하지 않는다. 인식·브로커 ACL·실제 물리 정지 및 현장 검증이
별도로 필요하다.

서버 저장소의 Python 환경과 별도 로봇 브랜치 체크아웃, 로컬 Mosquitto가
필요하다. **검토 브랜치 안에서만 제공되는** 아래 명령은 테스트가 소유하는
새 임시 DB와 loopback 브로커를 만들고,
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
