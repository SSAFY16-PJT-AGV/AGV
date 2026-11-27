# 가상 우회 제안·수락 MQTT 경계

이 기능은 [우회 예약 준비](sim_reroute_preparation.md)에 이어지는 격리 가상
실험이다. 기존 런타임에 자동으로 켜지지 않으며 실제 로봇이나 공용 브로커에
연결하지 않는다. 수락은 **이동하지 않고** 정지된 가상 로봇의 이력과 새
계획을 보관하는 것이다.

1. `publish_proposal(runtime, task_id)`는 복구 세대·로봇·대기 이벤트·기존과
   새로운 구간 점유가 일치하는 준비 건을 검증한다. 전송 시도 **전에** 상태를
   `uncertain`으로 영속 기록한 뒤 `agv/sim-r1/reroute/prepare`와 같은 토픽으로
   QoS 1, 비-retain으로 한 번 발행한다. 발행 예외·ACK 유실·브로커 ACK만
   있는 경우 모두 양쪽 구간의 점유를 유지하고 자동 재발행하지 않는다.
2. `SimRerouteMqttReceiver`는 명시적으로 결합한 가상 로봇·서버 인가 검사와
   별도 loopback MQTT 클라이언트를 사용한다. 로봇은 자신이 수신한 메시지에
   대해 서버의 같은 세대·세션·점유를 검증하고, 자기 inbox의 정지 이력·방향·
   원래 목적지·계획을 검사해 수락 장부에 기록한다. 서명된 수락 응답은
   `agv/sim-r1/reroute/receipt`로 보낸다.
3. `SimRerouteReceiptReceiver`는 프로세스 내부에서만 전달된 로봇별 별도 키로
   응답의 HMAC과 전환 ID·부모 명령·세션·계획 digest를 검증한다. 수락 기록과
   `uncertain→accepted`는 같은 SQLite 트랜잭션으로 처리한다. 동일 응답은
   중복 저장하지 않는다. 연결·검증·저장 실패는 점유를 해제하지 않는다.

서버 재시작 후의 기록 무결성 검사는 저장된 ID·명령 digest와 점유를 대조한다.
HMAC 키는 이 실험 프로세스의 메모리에 있으므로 재시작 후 그 기록만으로
수락 증명을 복원하거나 재개해서는 안 된다. 실제 활성화 전에 새로운 세션
계약과 수락 검증이 필요하다. 로봇 또는 서버가 수락해도 현재는 **경로 변경,
움직임, 이전 구간 해제 또는 웹 주문 재개를 하지 않는다**. 장치 신원·실물
위치·정지·후단 이탈은 이 시험으로 증명하지 않는다.

격리 브로커의 성공·중복·위조 응답·발행 실패·응답 유실 시험:

```powershell
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest `
  server.tests.test_sim_reroute_wire.RerouteWireTest -q
```
