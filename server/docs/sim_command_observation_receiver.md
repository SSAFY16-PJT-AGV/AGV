# 격리 가상 명령 응답 수신기

`SimCommandObservationReceiver`는 별도 opt-in `SimCommandObservations`에
시험용 수락·거절 **주장**을 기록한다. `SimRuntime`에 자동 연결되지 않고
기존 `rear_clear` 수신기·진행·복구·예약을 호출하거나 변경하지 않는다.
응답 토픽·본문은 팀이 확정한 로봇 프로토콜이 아니다. 실제 로봇이나 공유
브로커에 연결하지 않는다.

호출자는 별도 격리된 브로커와 초기화된 가상 관측 저장소를 준비한다.
새 관측 테이블은 첫 가상 배정을 만들기 전 `RecoveryGate`와 `SegmentProgress`를
초기화한 다음 생성해야 한다. 기존 배정이 생긴 뒤 새로 추가할 수 없으며,
복구 게이트 세대가 바뀐 기존 테이블을 임의로 새 세대에 연결하지 않는다.
수신기에는 `simulation=True`, 저장소와 일치하는 전체 `sim-` 로봇 ID 목록,
명시적인 브로커 포트가 필요하다. `start()`는 `127.0.0.1`에만 접속하고
`sim-observation/{robot_id}/order-response`를 QoS 1로 구독한다. 별도
클라이언트, 새 clean session, 자동 재연결 비활성, 수동 ACK를 사용한다.
IPv4 루프백 주소와 ID만으로 브로커의 실제 격리나 발행자의 신원을 증명할
수 없으므로 시험 소유 브로커인지 호출자가 확인해야 한다.

수신된 유효한 응답은 저장소가 해당 로봇·세션·작업·순번·맵과 저장된
주문 바이트의 digest를 검증하고 SQLite 트랜잭션을 완료한 뒤에만 MQTT
ACK한다. 동일 이벤트의 동일 응답은 저장소가 중복으로 확인하면 ACK한다.
오류·잘못된 토픽·QoS·retain·JSON·불일치 또는 충돌은 애플리케이션의
수동 ACK를 호출하지 않고 이 수신기의 연결을 닫는다. 특히 QoS 2 메시지의
경우 Paho 2.x가 `on_message` 전에 MQTT 프로토콜의 PUBREC를 내부적으로 보낼
수 있으므로 이 거부가 어떠한 프로토콜 응답도 없었다는 뜻은 아니다.
DB 반영 후 ACK 실패 시 기록은 보존되지만 브로커가
ACK를 받았는지는 알 수 없다. 수신 retain 플래그가 false여도 MQTT 3.1.1에서
발행 당시 retain=false를 증명할 수 없다.

구독 SUBACK 전에 메시지가 도착하면 저장이나 애플리케이션 수동 ACK를 하지
않고 연결을 닫는다. clean session이므로 재연결 시 그 메시지의 재전달을
보장하지 않는다. 수신 콜백은 SQLite 기록이 완료될 때까지 Paho 네트워크
루프를 동기적으로 점유하며, 이 동안 disconnect 감지와 `stop()` 완료도
지연될 수 있다. 실시간 제어·물리 정지나 응답 시간 상한으로 사용할 수 없다.

응답 기록은 로봇 신원, 실물 수신·수행, 위치·정지·후단 이탈의 근거가
아니다. 연결 중단이나 process 재시작 뒤에는 자동 재접속·복구·재발행하지
않는다. clean session이라 단절 중 응답 보관·재전달을 기대할 수 없다.
새 수신기를 만드는 시험에서 같은 응답을 별도로 다시 입력하면 저장소의
중복 검사를 통과할 수 있으나, 이를 로봇의 현재 상태나 자동 재개 근거로
사용하면 안 된다. 저장소와 복구 게이트의 세대 조건은 별도 문서를 따른다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:PYTHON_DOTENV_DISABLED = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_sim_command_observation_receiver `
    server.tests.test_sim_command_observation_receiver_mqtt -v
```

두 번째 시험만 별도 aMQTT 의존성과 로컬 소켓 권한이 필요하다. 시험은
실제 모터·카메라·공유 브로커를 사용하지 않고, 격리 가상 주문 발행기·
SQLite·가짜 응답 발행자를 사용한다. 실제 장비나 모든 장애 상황의 검증은
아니다.
