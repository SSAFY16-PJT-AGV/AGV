# 배정·점유·주문 발행 기록 저장소

`server.allocation_store.AllocationStore(path)`는 SQLite 파일을 사용하며
`initialize()`로 스키마를 만든다. SQLite의 `BEGIN IMMEDIATE` 트랜잭션과
고유 제약으로 작업·로봇·점유·첫 주문 기록을 원자적으로 저장한다. 실제
브로커에 연결하거나 메시지를 발행하지 않는다.

- `create_task(task_id)`: 기존 `SqliteTaskStore`와 같은 `tasks` 표에 `PENDING` 작업 생성.
- `allocate(task_id, robot_id, map_id, nodes, released_until, resources, action_at_end)`:
  대기 중인 작업, 하나의 로봇, 하나 이상의 자원을 함께 배정한다. 충돌 시
  전체 롤백한다. 자원은 `('node'|'edge'|'zone', id)` 튜플 모음이다. 경로는
  출발점과 다음 노드를 최소 하나 포함해야 하며, `released_until`까지의 모든
  노드와 그 사이의 모든 엣지 자원이 반드시 포함되어야 한다. 엣지 ID는 양 끝
  노드를 정렬한 배열에 `json.dumps(..., ensure_ascii=True, separators=(',', ':'))`를
  적용한 문자열이다. 양방향으로 같은 ID를 사용한다. 추가 공유 구역을 넣을 수
  있다. 입력의 경로·자원·끝 동작은 복사본으로 검증하고 저장한다. 호출자는
  복사가 진행되는 동안 입력을 변경하지 않아야 한다. 성공 시
  `task_id`, `robot_id`, `map_id`, `nodes`, `released_until`을 반환한다.
- `get_allocation(task_id)`: 배정 정보 또는 `None`을 반환한다.
- `list_occupancy()`: `(종류, ID)`에서 소유 로봇 ID로의 사본을 반환한다.
- `get_order(task_id)`: `{'payload': JSON 문자열, 'state': 상태}` 또는 `None`.
  payload에는 설계의 `order_id`, `seq`, `map_id`, `nodes`, `released_until`,
  `action_at_end`만 들어간다. 첫 주문의 `seq`는 1이다.
- `mark_attempted(task_id)`: 발행 시도 **전에** `pending`을 `uncertain`으로 변경.
- `mark_broker_acked(task_id)`: 발행자가 브로커 확인을 검증한 이후에만
  `uncertain`을 `acked`로 변경해야 한다. 브로커 확인은 로봇 실행 증거가 아니다.

같은 로봇이나 자원을 배정받은 다른 작업이 있으면 `AllocationConflict`가
발생한다. 생성한 파일을 다시 열면 배정·점유와 outbox 상태가 유지된다.
`uncertain`은 발행 여부를 확정할 수 없는 상태이며 자동 재발행하지 않는다.
점유 해제, 작업 완료 처리, 새 주문 시퀀스 생성, 재동기화는 구현하지 않았다.

첫 배정의 `map_id`와 버전은 같은 DB에 영속 기록된다. 이후 다른 맵 식별자로는
배정할 수 없으며 재시작 뒤에도 제한이 유지된다. 기존 저장소에 단일 맵의 배정이
있으면 초기화 때 그 맵으로 잠그고, 서로 다른 맵 배정이 섞여 있으면 초기화부터
거절한다. 맵 전환 중 기존 점유와 주문을 안전하게 처리하는 팀 절차가 아직 없어
이 보수적 임시 제한은 자동 해제하거나 변경하지 않는다.
입력 맵 식별자는 설계 예시에 맞춰 임시로 `map-name@v<0 이상의 ASCII 정수>`
형식만 허용한다. 형식과 맵 전환 절차는 팀 협의가 필요하다.

이 저장소는 기존 `SqliteTaskStore`의 `tasks` 표를 재사용한다. 양쪽이 같은
SQLite 파일을 사용해야 하나의 작업 상태가 공유되며, 외부 작업 요청을 받는 기능은
아직 없다. 저장소는 해제 구간의 노드와 엣지 포함 여부만 검사한다. 실제 맵
검증, 다음 대기점까지의 해제 구간 선정과 해당 구간의 모든 공유 구역 구성은
호출자가 담당해야 한다. 반복 노드 경로는 해제 노드가 모호하여 거절한다.
입력 JSON 깊이는 임시로 32단계로 제한한다. 팀과 재연결·중복·후단 이탈 및
로봇 상태 검증을 합의하고 물리 안전 인터록을 검증하기 전에는 실물에 연결하지
않는다. MQTT PUBACK, 상태 메시지나 로봇 연결만으로 실제 위치·정지를 증명할 수 없다.
