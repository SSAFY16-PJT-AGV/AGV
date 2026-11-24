# 실물 로봇(`physical-`)을 서버 구간 예약으로 움직이기

기존 팀 맵 런타임(`SimRuntime` + `field_simulation`)의 주문 → 구간 예약 → `rear_clear` →
다음 주문 흐름을 실물 JETANK(`feature/field-run`의 `serve` 모드)에도 쓰도록 연 변경이다.
가상 로봇(`sim-`)의 동작과 기존 시험은 그대로다.

## 바뀐 점

| 항목 | 내용 | 코드 |
| --- | --- | --- |
| 로봇 ID | 주문·예약·복구·진행 경로가 `sim-`과 `physical-`을 모두 받는다. 가상 집기·적재·Unreal 응답을 쓰는 모듈(웹 상품 시연, 합성 peer, handoff store)은 계속 `sim-`만 받는다 | `robot_ids.py` |
| 켜는 조건 | `physical-` 로봇은 `physical_robots=True`와 `field_simulation=True`를 함께 줘야만 받는다. handoff store·명령 관측·초기 위치 옵션과는 같이 쓰지 않는다 | `sim_runtime.py`, `main.py` |
| 세션 | 설정의 세션 값으로 `"from_state"`를 주면, 런타임 시작 때 그 로봇의 **실시간**(retained 아님) `agv/<id>/state`에서 `robot_session`을 읽어 넣는다. 로봇은 프로세스를 켤 때마다 세션을 새로 만든다. 나중에 세션이 바뀌면(로봇 재시작) 그 로봇의 작업을 모두 막고 서버 런타임을 다시 시작해야 한다 | `physical_state_proof.py` |
| 정지 증명 | 실물에는 challenge 응답 채널이 없다. 같은 세션의 실시간 상태가 서버가 마지막으로 보낸 주문(`order_id`, `seq`, `map_id`)의 `released_until` 노드에 `WAITING`/`IDLE`로 서 있음을 보이면, 서버가 challenge를 열고 서버가 저장한 주문 digest로 완료한다. 증명은 만료되므로 그 자리에 있는 동안 15초마다 갱신한다 | `physical_state_proof.py` |
| 이벤트 재시도 | 증명보다 먼저 온 `rear_clear`, 다른 로봇이 구간을 잡고 있어 막힌 `rear_clear`를 1초마다 다시 처리하고, 이어지는 주문을 기존 workflow로 보낸다 | `physical_state_proof.py` |
| 스퍼 경로 | 방향 기반 경로 계산이 스퍼 진입·진출을 지원한다. 목적지가 스퍼 끝(`D_P_red`)이면 부모 노드까지 간 뒤 스퍼 엣지를 붙인다. 스퍼 끝에 주차한 로봇은 `incoming_edge`로 그 스퍼 엣지 ID(`E_D_P_red`)를 주며, 후진해 부모 노드로 나온 뒤 어느 방향으로든 출발한다(유턴 비용 없음). 교차 노드(`P_yellow`)의 스퍼는 어느 선으로 나가는지 맵에 없어 거부한다 | `field_map_planner.py`, `field_dispatch.py` |

실제 매트 맵(`mat_ssafy_v2.json`)의 대기점은 스퍼 끝(`D_*`)뿐이다. 서버 규칙상 로봇은 대기점에서만
멈출 수 있으므로 **실물 작업의 목적지는 스퍼 끝**이어야 한다(`P_red`가 아니라 `D_P_red`).
패치 노드를 목적지로 주면 "대기점 없음"으로 거절된다.

## 정지 증명이 증명하지 않는 것

상태 메시지는 로봇 스스로의 보고이고 `rear_clear` 이벤트와 같은 종류의 증거다. 로봇 후단이 실제로
자원을 벗어났는지 재지 않고, 로봇 신원을 인증하지 않는다. 공용 브로커에서는 장비별 인증·ACL
(`mqtt_security.py`, #49)을 쓴다.

## 실행

1. 노트북에서 Mosquitto를 띄운다(로봇이 붙을 수 있게 `listener 1883 0.0.0.0`). 서버 런타임은
   같은 PC의 `127.0.0.1`로 붙는다.
2. 젯슨마다 로봇 ID를 정하고(`python3 -m agv.tools.robotcfg init physical-1`) serve 모드를 띄운다.
   ```
   MQTT_HOST=<노트북 IP> python3 -m agv.explore --mode serve --map maps/mat_ssafy_v2.json --start P_orange --enable-motors --view
   ```
3. `AGV_SIM_CONFIG`에 아래처럼 쓰고(작업 ID는 로봇별로 미리 나열) FastAPI를 단일 프로세스로 띄운다.
   `map_file`은 설정 파일 기준 상대 경로이며, 로봇과 같은 `feature/field-run`의 `maps/mat_ssafy_v2.json`을
   가리킨다(이 브랜치에는 `maps/`가 없다).
   ```json
   {
     "database": "physical-run-1.sqlite3",
     "map_file": "maps/mat_ssafy_v2.json",
     "robots": {"physical-1": {"P1-1": "from_state"}, "physical-2": {"P2-1": "from_state"}},
     "mqtt_port": 1883,
     "mqtt_client_id": "physical-fleet-server",
     "field_simulation": true,
     "physical_robots": true
   }
   ```
   두 로봇의 serve 모드가 먼저 떠 있어야 한다(15초 안에 실시간 상태가 없으면 시작 실패).
4. `/simulation` API로 작업 생성 → `prepare-field`(목적지 `D_P_red`, 후보 `{"robot_id": "physical-1",
   "node": "P_orange", "incoming_edge": "E13", "available": true}`) → 발행. 공유 구간이 잡혀 있으면
   `prepare-field`가 거절되므로 잠시 뒤 다시 한다.

`incoming_edge`는 로봇이 그 노드에 어느 엣지로 들어왔는지(진행 방향)다. 매트를 시계 방향으로
돌며 `P_orange` 앞에 놓았으면 `E13`이다. 스퍼 끝에 주차해 있으면 그 스퍼 엣지(`E_D_P_red`)다.

## 시험

```
python -m unittest server.tests.test_physical_runtime_config server.tests.test_robot_ids \
    server.tests.test_field_map_planner server.tests.test_field_dispatch
# 팀 로봇 코드를 가상 매트로 돌려 physical-1/2 두 대로 끝까지(Mosquitto 필요)
AGV_FIELD_REPO=<field-run 폴더> AGV_FIELD_MAT_DEPS=<cv2·numpy 경로> MOSQUITTO_EXE=<mosquitto> \
    python -m unittest server.tests.test_physical_fleet_integration
```

통합 시험은 aMQTT 시험 브로커 대신 Mosquitto를 쓴다. aMQTT는 다른 클라이언트가
`agv/<id>/state`를 구독하고 있으면, `agv/+/event`를 구독한 클라이언트에게도 그 retained 상태를
보낸다. 이벤트 수신기는 이것을 잘못된 토픽으로 보고 닫는다(올바른 동작). Mosquitto는 보내지 않는다.

## 남은 것

- **로봇 한 대가 DB 하나에서 작업 하나만 할 수 있다.** 기존 `allocation_jobs.robot_id`가 UNIQUE라서
  같은 로봇의 다음 작업을 만들 수 없다. 가상 시연은 handoff store로 다음 구간을 넘기지만, 그 경로는
  Unreal 집기·적재 응답을 요구한다. 실물은 `action_at_end`가 `none`인 구간이면 응답 없이 넘기도록
  handoff를 넓히는 방안이 다음 단계다. 그 전까지는 작업 ID를 로봇마다 하나씩 쓰고, 다음 작업은
  새 DB로 런타임을 다시 시작한다.
- 로봇이 재시작하면 세션이 바뀌어 그 로봇의 작업이 막힌다. 자동 재개는 없다.
- 웹 주문 시연(`order_demo_app.py`)은 아직 `sim-r1`/`sim-r2`와 팀 가상 맵 기준이다.
- 트윈 브리지는 `battery`가 없는 상태를 전달하지 않는다(로봇은 미측정이면 `null`).
