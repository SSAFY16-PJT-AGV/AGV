# MQTT 메시지 기록 및 재생

설계 문서 v0.2 3장의 MQTT 메시지 JSONL 기록과 같은 간격의 재생을 위한
서버 내부 모듈이다. 이 모듈은 직접 브로커에 접속하지 않으며, 현재 서버에
자동 연결되어 있지 않다. 별도의 구독과 통합 작업이 필요하다.

## 기록 형식

각 줄은 수신 시각 `received_at`(UTC), 시작 이후 단조 경과 시간 `elapsed`(초),
`topic`, `payload_b64`, `qos`, `retain`을 담는다. 이 JSONL 필드들은 설계에
상세 규격이 없는 **내부 기록 형식 제안**이며 MQTT 메시지 필드는 바꾸지 않는다.
payload를 Base64로 보존하여 JSON뿐 아니라 연결 상태 `offline` 같은 문자열과
임의 바이트도 그대로 기록할 수 있다. `qos`와 `retain`은 **수신자가 관측한 값**이다.
MQTT 3.1.1에서는 발행자가 보낸 retain 설정이 기존 구독자에게 전달될 때
수신 메시지에서 그대로 나타나지 않을 수 있으므로, 이 기록만으로 발행 시
원본 retain 설정을 항상 복원할 수는 없다.

```python
from server.mqtt_log import JsonlRecorder

# 예시는 Paho on_message 콜백의 message를 전달한다.
with JsonlRecorder("records/session.jsonl") as recorder:
    recorder.record(message.topic, message.payload, message.qos, message.retain)
```

대상 디렉터리는 미리 준비해야 하며 기존 파일을 덮어쓰지 않는다. 실제 통합에서는
하나의 recorder를 구독 전체에 공유하고 종료 시 `close()`해야 한다. 기록 오류의
처리, 저장 공간, 개인정보·보관 정책과 수신 과정에서의 지연은 별도로 다뤄야 한다.
실측 JSONL과 대용량 기록은 저장소에 커밋하지 않는다.

## 재생

기본 호출은 파일 전체를 검증하고 재생 계획만 반환하며 발행하거나 기다리지 않는다.

```python
from server.mqtt_log import replay

plan = replay("records/session.jsonl")
```

실제 발행은 `allow_publish=True`와 호출자가 제공한 `publish` 함수가 둘 다 있어야
가능하다. 원본 토픽에 직접 발행할 수 없도록 토픽 앞에 `replay/`를 강제한다.
따라서 예를 들어 `agv/r1/order`는 `replay/agv/r1/order`로만 발행된다.

```python
# publish는 격리된 재생 환경을 담당하는 호출자가 제공한다.
replay("records/session.jsonl", publish=publish, allow_publish=True)
```

원래의 payload, 관측 QoS와 retain을 사용하고, 단조 경과 시간으로 수신 간격을
재현한다. 발행 처리 시간이 길어지는 경우 일정에 최대한 맞추지만 운영체제 스케줄링,
발행 지연 또는 연결 문제로 정확한 시각이나 전달을 보장하지 않는다. `publish`는
성공 시 `None` 또는 `rc == 0`인 Paho 발행 결과를 반환하고, 실패 시 예외를 발생시키거나
0이 아닌 `rc`를 반환해야 한다. 실패나 알 수 없는 반환값을 만나면 이후 메시지 재생을
중단한다. `rc == 0`은 로컬 큐 등록 결과이며 브로커 확인(PUBACK)이나 최종 수신을
보장하지 않는다. 전체 로그와 접두사 적용 후 토픽을 먼저 검증하며, 유효하지 않으면
첫 메시지도 발행하지 않는다.

격리 접두사를 사용하는 방식은 설계의 **같은 토픽 재생**과 다르다. 원래 토픽으로의
재생은 지원하지 않는다. 접두사만으로 완전한 격리가 보장되지는 않는다. 브로커에서
`#` 또는 `replay/#`를 구독하는 클라이언트도 재생 메시지를 받을 수 있고, retained
메시지는 재생 토픽에 남는다. 실물과 분리된 브로커·네트워크에서 재생하고, 실물 장비와
운영 서비스가 접속하지 못하도록 접근 제어(ACL)와 연결 상태를 확인해야 한다. 설계대로
같은 토픽으로 재생하려면 이런 분리 방법을 팀과 정하고 별도 구현·검증해야 한다.
