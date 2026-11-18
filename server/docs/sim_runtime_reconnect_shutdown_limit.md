# 가상 런타임 재연결과 종료

격리 시뮬레이션의 `SimRuntime.reconnect()`는 한 번에 한 시도만 허용한다.
기존 수신기·workflow를 정리한 후 복구 게이트의 로컬 차단을 확인하고 새
수신기를 연결한다. 재연결 중인 수신기는 SUBACK을 받았더라도 외부 제어
요청에 준비된 것으로 표시하거나 사용하지 않는다. 재연결 자체는 차단 해제,
복구 승인, 위치 확인이나 명령 발행이 아니다.

네트워크 연결과 정리는 런타임 전체 잠금 밖에서 수행한다. 이 동안 재연결
시도는 DB owner를 보유하므로 종료는 신규 workflow 발행을 먼저 막고 시도와
그 콜백이 끝난 뒤에만 owner를 놓는다. 초기 수신기와 재연결 수신기 모두에서
종료 fence 이후에 새로 진입하는 이벤트 콜백은 이벤트 저장·ACK·점유 이전을
수행하지 않는다. 이미 dispatch에 들어간 콜백은 완료될 수 있으며, 종료는
이를 기다린 뒤 receiver를 명시적으로 닫고 복구 게이트를 무효화한다.
별도의 종료 경로에서 동기 `connect()` 중인 클라이언트에 동시에 disconnect를
호출하지 않는다. **동기 connect는 강제로 취소할 수 없으므로 stop 반환은
연결 시도가 끝날 때까지 지연될 수 있다.** 이미 발행 검사에 통과한 작업은
종료가 완료될 때까지 끝날 수 있고 이미 발행된 메시지는 회수할 수 없다.
연결 성공 직후에도 종료와 소유권 인계를 같은 잠금에서 판정한다. 종료가 먼저
시작됐다면 재연결 쪽이 클라이언트를 정리하고 오류를 반환하며, 인계가 먼저
끝났다면 종료 쪽이 해당 클라이언트를 정리한다.
이 변경은 재연결에 관한 것이다. 최초 `start()`와 기존 일부 배정·발행·복구
작업은 여전히 I/O 중 런타임 잠금을 보유한다. 이런 작업이 이미 잠금을 잡고
있다면 stop 호출이 fence를 설정하기 전까지 기다릴 수 있다. 위 수신 차단은
fence가 설정된 이후에 적용되며, 호출 순간의 물리적 정지를 의미하지 않는다.

통신 정리가 실패하면 런타임은 후속 제어와 재연결을 차단하고 DB owner를
유지한다. 자체 network/workflow 스레드에서 stop 또는 reconnect를 재진입하면
스스로의 종료를 기다리거나 owner를 조기 해제하지 않도록 거부한다. 이런
실패의 해결은 별도 운영 조치가 필요하며 자동 재개하지 않는다.

격리 aMQTT·실제 SQLite 시험은 connect와 loop_start를 각각 지연시키고,
중복 재연결·동시 stop·잠정 SUBACK·늦은 이벤트·자기 종료·cleanup 실패,
owner 보존 및 선택적 관측·조사 기록 기능을 확인한다. 이 시험은 실물 로봇의
신원, 물리 정지, 장비 통신이나 안전 인터록을 검증하지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_sim_reconnect_shutdown_lifetime_mqtt server.tests.test_sim_reconnect_reentrant -v
```
