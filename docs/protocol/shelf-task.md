# 선반 작업 메시지 (로봇 쪽 제안, 2026-11-26)

서버가 로봇에게 "선반 앞으로 가서 어느 칸의 물건을 집어라 / 놓아라 / 보기만 해라"를 시키고, 로봇이 결과와
선반 상태를 보고하는 메시지다. 서버 담당과 아직 맞추지 않은 **로봇 쪽 제안**이다. 형식이 바뀌면
`agv/link/task_msgs.py` 한 파일만 고치면 된다(로봇 안쪽 로직은 `agv/task/`의 객체만 본다).

## 흐름

1. 서버가 기존 주문(`agv/{id}/order`)을 보낸다. 경로는 **선반의 스퍼 끝(D0k)에서 끝나고**, 끝 동작
   `action_at_end`에 선반 작업을 넣는다.
2. 로봇은 지금처럼 이동하고, 해제 노드마다 `rear_clear` 이벤트를 보낸다.
3. 마지막 노드(스퍼 끝)에 서면 선반 작업을 시작한다. 상태 메시지의 `status`가 `WORKING`이 된다.
   - 카메라(1·5번)를 관측 자세로 → 칸 보기(여러 장) → 필요한 칸이 안 보이면 1번을 돌려 다시 보기
   - 할 일 결정 → 팔로 집기/놓기 → 다시 보기(확인) → 주행 자세로
4. 끝나면 선반 관측(작업 전·후, `agv/{id}/shelf`)과 작업 결과(`agv/{id}/task`)를 보낸다.
   그다음 작업이 끝난 것으로 보고 `status`는 `IDLE`이 되어 새 주문을 받는다.
5. 실패해도(칸이 비었음 등) 작업은 끝난 것이다. 로봇은 그 자리에 서 있고, 다음 일은 서버가 정한다.

작업하는 동안 같은 작업의 다른 주문이나 새 주문은 거절한다(`rejected`: "robot is busy with a shelf task").

## 주문의 끝 동작 `action_at_end`

| 필드 | 값 | 설명 |
| --- | --- | --- |
| `type` | `load` \| `unload` \| `observe` \| `none` | 집기 / 놓기 / 보기만 / 없음. 모르는 종류는 무시(지금처럼) |
| `shelf` | `"N01"`~`"N06"` | 선반 = 맵의 선반 패치 노드. 없으면 경로 마지막 스퍼 끝의 부모 패치 |
| `slot` | 1~4 | `load`·`unload`에 필요. 칸 1은 선반을 마주 본 로봇의 왼쪽 |
| `item` | `"milk"` \| `"tissue"` \| `"egg"` \| `"juice"` | 선택. `load`: 이 물건이어야 집음(없으면 아무거나), `unload`: 이걸 들고 있어야 놓음 |

```json
{"order_id": "T7", "seq": 1, "map_id": "mat_ssafy_v3@v0",
 "nodes": ["N05", "N08", "N06", "J06", "J07", "N03", "N02", "N07", "N01", "D01"], "released_until": "D01",
 "action_at_end": {"type": "load", "shelf": "N01", "slot": 2, "item": "tissue"}}
```

경로가 그 선반의 스퍼 끝에서 끝나지 않으면 주문을 거절한다(`rejected.reason`에 이유).

## 작업 결과 `agv/{id}/task` (QoS 1)

```json
{"type": "task_result", "event_id": "9a9b77ec8f674d34-t3", "robot_session": "9a9b77ec8f674d34",
 "ts": 1795662394.3, "map_id": "mat_ssafy_v3@v0", "order_id": "T7",
 "action": "load", "shelf": "N01", "slot": 2, "item": "tissue",
 "result": "done", "reason": null, "carrying": "tissue",
 "shelf_state": {"shelf": "N01", "seen": true, "wrong_shelf": null,
                 "slots": [{"slot": 1, "state": "milk", "marker_id": 30}, {"slot": 2, "state": "empty"},
                           {"slot": 3, "state": "egg", "marker_id": 32}, {"slot": 4, "state": "juice", "marker_id": 33}]}}
```

- `result`: `done` | `failed`. `carrying`: 작업이 끝난 뒤 들고 있는 물건(없으면 null).
- `shelf_state`: 작업 뒤 본 선반(못 봤으면 작업 전).
- `reason`(실패 이유):

| 코드 | 뜻 |
| --- | --- |
| `bad_task` | 칸 번호가 범위 밖 등 |
| `shelf_not_seen` | 선반(그 선반의 칸 마커)을 못 찾음 |
| `wrong_shelf` | 다른 선반의 마커만 보임(`shelf_state.wrong_shelf`에 그 선반) |
| `slot_unknown` | 그 칸을 판단할 수 없음(가려짐, 화면 밖, 마커 둘) |
| `slot_empty` | 집을 칸이 비어 있음 |
| `wrong_item` | 집을 칸에 다른 물건 |
| `slot_occupied` | 놓을 칸에 이미 물건 |
| `already_carrying` | 집으려는데 이미 들고 있음(한 번에 하나) |
| `not_carrying` | 놓으려는데 안 들고 있거나 다른 물건을 들고 있음 |
| `arm_unavailable` | 팔 동작이 연결되지 않음(지금 실물: 팔 담당 코드 전) |
| `arm_failed`, 그 밖의 팔 이유(`grip_missed` 등) | 팔이 실패를 알림 |
| `verify_failed` | 동작 뒤 다시 봤더니 기대와 다름(집었는데 칸에 그대로, 놓았는데 칸이 비어 있음 등) |
| `timeout` | 팔 동작이 제한 시간(30초)을 넘김 |

## 선반 관측 `agv/{id}/shelf` (QoS 1)

작업마다 작업 전(`before`)과 작업 후(`after`, 보기만이면 없음)에 한 번씩.

```json
{"type": "shelf_state", "event_id": "9a9b77ec8f674d34-t2", "robot_session": "9a9b77ec8f674d34",
 "ts": 1795662390.1, "map_id": "mat_ssafy_v3@v0", "order_id": "T7", "phase": "before",
 "shelf": "N01", "seen": true, "wrong_shelf": null,
 "slots": [{"slot": 1, "state": "milk", "marker_id": 30}, {"slot": 2, "state": "tissue", "marker_id": 31},
           {"slot": 3, "state": "egg", "marker_id": 32}, {"slot": 4, "state": "juice", "marker_id": 33}]}
```

칸 `state`: 물건 종류 | `empty`(칸 마커가 보이고 물건 없음) | `unknown`(판단 못 함). `unknown`은 "모른다"이지
"비었다"가 아니므로, 서버는 그 칸의 이전 상태를 그대로 두는 것이 맞다.

## 상태 메시지 `agv/{id}/state`에 더한 것

| 필드 | 값 |
| --- | --- |
| `status` | 기존 넷(IDLE, MOVING, WAITING, ERROR)에 `WORKING`(선반 작업 중) |
| `carrying` | 들고 있는 물건 종류 또는 null(예전에는 늘 null) |
| `task` | 작업 중이면 `{"action", "shelf", "slot", "phase"}`, 아니면 null. phase: `observe_pose`, `look_before`, `pan`, `act`, `look_after`, `drive_pose` |

## 서버 쪽과 다른 점 / 맞출 것

- 서버 브랜치는 이미 `{"type": "load"}`, `{"type": "unload", "shelf": "S2"}`를 쓴다. 종류 이름은 같게 했고,
  `slot`·`item`을 더했다. 선반 이름은 맵 노드(N01~N06)로 했다(서버의 `SHELF_RED` 같은 이름과 다르면 맞춘다).
- 기존 `event` 토픽에는 새 종류를 보내지 않는다(서버가 모르는 종류를 오류로 봄). 그래서 토픽을 둘 더 썼다.
- 결과·관측은 QoS 1이라 두 번 갈 수 있다. `event_id`로 중복을 거른다.
