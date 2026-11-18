# 로봇 주문 오프라인 사전 검사

`server.field_order_preview.inspect_field_order`는 팀 `field-run` 주문의 기존
여섯 필드와 로봇 ID를 **네트워크 연결 없이** 검사한다. 기존
`validate_field_robot_order`를 사용하고, 실제 MQTT 전송 형식으로 직렬화한
주문의 SHA-256과 주문 ID·순번·맵 ID를 불변 요약으로 돌려준다. 맵과 세션,
위치·방향, 정지 또는 후단 이탈, 로봇 인증, 주문 수신이나 실행은 검증하지
않는다. 이 요약은 운행 허가나 안전 증거가 아니다.

`route_start_node`는 주문 `nodes`에 들어있는 **전체 경로의 첫 노드**이다.
후속 주문에서는 로봇의 현재 노드 또는 후속 구간 시작 노드를 뜻하지 않는다.

```python
from server.field_order_preview import inspect_field_order

preview = inspect_field_order('robot-1', order)
print(preview.order_id, preview.sequence, preview.payload_sha256)
```

로봇 ID는 토픽에 안전하게 들어갈 수 있는 UTF-8 문자열인지 검사한다. 여기의
128바이트 제한은 서버 임시 제한이며 확정된 팀 로봇 식별 규격이 아니다.
요약 형식은 서버 내부용으로, MQTT에 필드를 추가하거나 팀과 합의된 새 메시지를
선언하지 않는다. 내용이 바뀌면 다시 검사해야 한다.

이 `payload_sha256`은 현재 MQTT 전송 바이트의 digest이며 JSON 키 순서에
영향받는다. [재동기화 제안](recovery.md)의 `order_digest`는 키를 정렬하고
ASCII 이스케이프한 정규 JSON의 digest이므로 별개의 값이다. 서로 대체하거나
비교 기준으로 혼용하면 안 된다.

`review_proposed_observation`은 시험 대역이 제공한 로봇 ID·세션·맵·노드·
진입 간선·상태와 서버가 기대하는 값을 비교해 불일치 목록을 반환한다. `online`,
`retained`와 수신 시각은 대역의 입력이다. `received_at`, `now`는 **같은
프로세스의 같은 단조 시계** 기준이어야 하며 재시작 후 저장된 값을 재사용하지
않는다. 누락·불일치·오래됨·미래 시각·retained·offline·IDLE 외 상태를
보고한다. 이 자료 구조는 실제 로봇 MQTT 메시지가 아니라 미합의 시험용
제안이다. 실제 MQTT 수신 시각을 취득하거나 메시지 출처·연결 상태를 인증하지
않고, 시간값의 출처가 진짜인지를 확인할 수도 없다. 빈 불일치 목록은 검사한
입력이 모순되지 않는다는 뜻일 뿐 인증·신선도·실제 위치나 정지 증거도,
배정·발행·점유 해제 허가도 아니다. 대역용 값으로 실물을 제어하면 안 된다.
관측 검사는 `seq=1`인 최초 주문만 지원한다. 후속 주문은 전체 경로의 첫 노드와
현재 정지 노드가 다를 수 있고 상태도 `WAITING` 등일 수 있으므로 거부한다.
반복 경로의 최초 주문에서 첫 노드 비교는 입력 형식 검토일 뿐 방문 순서나
실제 위치를 확인하지 않는다.

이 모듈에는 전송·MQTT 연결·예약 변경 기능이 없다. 기존 발행기의 `sim-`
제한과 시뮬레이션 전용 운행 보호도 변경하지 않는다. 이 검사에 통과해도
실물 로봇이나 공유 브로커에 명령을 보낼 수 있다는 뜻이 아니다.

```powershell
.\.venv\Scripts\python -m unittest server.tests.test_field_order_preview -v
```
