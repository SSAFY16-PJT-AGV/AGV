# 가상 로봇 주문 수신 저장소

`sim.order_inbox.SimOrderInbox`는 격리된 **가상 로봇**이 주문의 중복과
순서를 검사하는 SQLite 저장소다. MQTT 네트워크에 접속하거나 카메라·모터를
조작하지 않는다. 후단 이탈 이벤트를 만들거나 실제 위치를 판정하지 않는다.
실제 통신 계약은 `server/docs/segment_recovery_proposal.md`에 적힌 제안이며,
팀과 합의되지 않았으므로 실물 로봇에 사용하지 않는다.

## 사용 흐름

```python
from sim.order_inbox import SimOrderInbox

# 시험용 SQLite 경로와 가상 로봇 ID, 승인된 맵 버전을 호출자가 준비한다.
inbox = SimOrderInbox(db_path, "sim-r1", "layout@v1")
inbox.initialize()

# payload는 수신한 MQTT 메시지의 UTF-8 JSON bytes다.
receipt = inbox.accept(payload)
if receipt.new:
    # 호출자가 가상 로봇의 단 한 번의 실행을 별도로 진행한다.
    pass
```

`receipt`에는 `new`, `order_id`, `seq`, `digest`, `state`가 있다. 동일한
`(order_id, seq)`와 같은 JSON 내용을 다시 받으면 `new=False`이며 다시 실행하지
않는다. 같은 키에 다른 내용이 오면 `OrderRejected`를 발생시킨다. 서로 다른
JSON 필드 순서와 공백은 같은 내용으로 취급한다. digest는 정렬된 키와 ASCII
JSON 표현의 SHA-256이며 무결성 검사 용도이지 메시지 인증이 아니다.

다음 명령은 이전 명령이 완료된 뒤에만 연속된 순번으로 수락한다. 같은 작업의
경로와 끝 동작은 유지하고 `released_until`은 앞으로 진행해야 한다. 새 작업은
이전 작업의 마지막 경로 노드까지 완료한 뒤부터 수락한다. 메시지는 설계 문서의
여섯 주문 필드만 허용하며 `sim-` 로봇 ID와 승인된 `map_id@v버전`으로 제한한다.

## 완료와 재시작

```python
inbox.mark_completed(
    receipt.order_id, receipt.seq, receipt.digest,
    stopped_at=receipt_stopped_node, rear_clear_confirmed=True,
)
```

이 호출은 해당 인스턴스가 새로 수락한 주문에 한하며, 중간 실패나 정지 상태가
불명확하면 호출하지 않는다. 대기 노드는 해당 주문의 `released_until`과 같아야
한다. `initialize()` 재호출이나 프로세스 재시작은 해당 인스턴스의 새 수락
권한을 지운다. 재시작 뒤 기존 미완료 주문을 다시 받더라도 `new=False`이므로
자동 재실행하지 않는다.

가상 로봇의 독립된 상태를 직접 확인한 뒤에만 `reconcile_completed`에 같은
인자를 명시적으로 전달해 복구할 수 있다. 두 완료 함수의 `rear_clear_confirmed`
불리언은 호출자가 주장한 가상 확인일 뿐 센서 판정이나 인증된 이벤트가 아니다.
실제 후단 이탈 자원·부팅 세션·중복 이벤트와 서버 점유 해제는 별도 프로토콜로
연결하고 검증해야 한다.

DB는 로봇 ID와 맵 버전에 고정된다. 로봇·맵 불일치, 기존 주문이 있는데 식별
정보가 없어졌거나 저장된 주문과 digest가 맞지 않는 경우 실패한다. 새로운
시뮬레이션용 DB를 사용하고 기존 파일을 복원·수정하기 전에 그 출처를 확인한다.

## 테스트

저장소 루트에서 서버 의존성이 설치된 Python으로 실행한다.

```powershell
.\.venv\Scripts\python -m unittest discover -s sim/tests -v
```

## 가상 후단 이탈 이벤트 수신

`sim.event_receiver.SimEventReceiver`는 호출자가 제공한 **별도 격리 브로커의
전용 Paho 클라이언트**에 이벤트 콜백을 연결한다. 기본값은 비활성화이며
`simulation_enabled=True`와 로봇 ID별 활성 작업 ID의 명시적 매핑이 필요하다.
`SegmentProgress`와 동일한 `RecoveryGate` 및 같은 DB와 로봇 작업 매핑의
`SimEventInbox`를 전달한다. 어댑터는 클라이언트를 연결하거나 재연결하거나
주문을 발행하지 않는다. Paho 자동 재연결은 반드시 꺼야 하며 MQTT 3.1.1
전용 클라이언트는 고정 client ID와 `clean_session=False`를 사용해야 한다.
호출자가 클라이언트 연결 및 네트워크 루프를 관리한다.

```python
receiver = SimEventReceiver(
    dedicated_client, progress, inbox=event_inbox, recovery_gate=gate,
    robot_tasks={"sim-r1": ("task-1",)}, simulation_enabled=True,
    on_result=handle_result, on_error=handle_error,
)
```

`agv/+/event`와 `agv/+/conn`을 각각 QoS 1로 구독한다. 실제 이벤트의
수신 QoS가 1이고 retained 전달이 아니며, 토픽의 가상 로봇 ID와 메시지의
작업 ID가 명시된 매핑에 속할 때만 `SegmentProgress.advance`에 전달한다.
UTF-8, JSON 객체, 중복 키·비정상 숫자와 크기를 검사한다. `offline` 연결
보고는 QoS 0 또는 1과 retained 여부에 관계없이 해당 로봇의 복구 proof를
무효화하고, `online`은 proof를 생성하지 않는다. 알 수 없는 연결 값도 해당
로봇의 proof를 무효화한다. 브로커 연결이 끊기거나 구독이 실패하면 모든
매핑 작업의 proof를 무효화하고 수신기를 닫는다. 영속 세션 재접속 시 구독
확인 전 도착하는 이벤트는 검증 후 저장하고 ACK는 구독 확인까지 보류한다.
이 이벤트는 자동 실행하지 않으며 명시적 retry가 필요하다. 구독 확인 전
`offline`은 즉시 proof를 무효화한다. 보류 ACK는 최대 64개이며, 초과·검증
오류·저장 오류에서는 ACK 없이 닫고 전체 proof를 무효화한다. 새로운 작업 매핑이나
재연결에는 새 전용 클라이언트와 명시적 재동기화가 필요하다.

이벤트는 검증 후 먼저 inbox에 저장하고 저장이 완료된 다음 MQTT 수동 ACK를
보낸다. 이어서 한 번 처리를 시도하며, 복구 미완료나 충돌 등으로 실패하면
이벤트는 pending으로 남고 `on_error`와 `last_error`에 오류가 전달된다.
호출자는 원인을 확인하고 복구한 후
`event_inbox.retry("sim-r1", event_id, progress)`를 명시적으로 실행한다.
이벤트 ACK는 영속 저장 확인이지 구간 진행 완료의 확인이 아니다. 저장이나
ACK가 실패하면 전체 proof를 무효화하고 수신기를 닫는다. QoS 1 offline 보고는
proof 무효화가 성공한 뒤 ACK한다. 이 수신기는 FastAPI에 자동 연결되지 않고
다음 주문을 자동 발행하지 않는다.

브로커가 유지하는 세션이 살아 있어야 미확인 메시지를 재전달받을 수 있다.
브로커 재시작 시 세션 영속성과 만료는 브로커 설정에 달려 있으며 보장하지
않는다. 송신 측 응용 프로그램의 재전송·수신 확인 규약은 팀 협의가 필요하다.
콜백은 짧아야 하며 명시적 재시도가 자동으로 실행되지는 않는다.

MQTT 3.1.1에서는 발행자가 retain을 설정했더라도 기존 구독자에게 전달되는
메시지에서 retain 표시가 제거될 수 있다. 따라서 `message.retain=False`는
발행자가 non-retained로 보냈다는 증거가 아니며, 발행자 규약과 격리 브로커가
별도로 필요하다. 이 이벤트·온라인 보고·불리언은 로봇의 실제 위치, 후단 이탈,
정지 또는 메시지 출처를 인증하지 않는다. **실물 운행에 사용하지 않는다.**

명시적으로 활성화한 `SimProgressWorkflow`를 이 수신기에 연결하면, 새로
처리된 이탈 이벤트의 다음 구간 주문을 별도 작업자에서 발행할 수 있다.
재시작 후 기존 pending 주문은 자동 발행하지 않으며 새 복구 확인 후 별도
요청이 필요하다. 시작 조건과 제한은 [workflow 안내](docs/workflow.md)를
참고한다.
