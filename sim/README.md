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
