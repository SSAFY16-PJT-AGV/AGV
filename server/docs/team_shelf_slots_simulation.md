# 팀 v3 선반 슬롯의 격리 예약 실험

팀 [선반 인식 설계 #61](https://github.com/SSAFY16-PJT-AGV/AGV/pull/61)은
N01~N06 선반마다 4칸, 칸 마커 1~24를 지정한다. N01은 미정렬 물품,
N02 우유, N03 휴지, N04 계란, N05 주스, N06은 정렬된 물품이다.
선반의 실제 재고·위치·물리적 정지와 카메라 판정은 아직 실측하지 않았다.

`server.team_shelf_order_slots`는 **별도 시뮬레이션 SQLite 장부**다. 네 종류의
합성 상품에 대해 N01의 일치하는 상품 칸과 N02~N05의 빈 칸을 한 트랜잭션에서
동시에 예약한다. 팀 마커의 1~24번 `slot_id`와 Unreal 등에서 흔히 사용하는
선반별 0~3 `slot_index`를 구분한다. N06은 4개의 상태·마커를 보존하지만,
별도 작업 규칙이 확정되지 않아 자동 목적지로 사용하지 않는다.

기본 칸 상태는 `unknown`이다. 명시된 합성 세계의 인증된 호출자가
`begin_session` → `begin_snapshot` → `apply_snapshot`으로 24칸 전체의
관측을 제출해야 새 예약을 받을 수 있다. 관측 전 fence 이후 다른 예약이나
동작으로 장부가 변경되면 관측 전체를 거부한다. 점유 중인 칸은 새로운 관측을
근거로 자동 해제하지 않고 `unknown`으로 보존한다. 중복 주문 ID는 다른 상품을
요청할 수 없다. 동시 주문은 동일 출발·목적 칸을 선점할 수 없다.

`mark_dispatched`는 호출자가 시뮬레이션 집기/적재를 보내기 전에 기록하며,
`complete_action`은 동일 세션·주문·동작 ID의 명시적인 합성 결과만 받는다.
연결 종료 또는 재시작 시 미완료 주문은 `unknown`으로 표시하며 두 칸을
계속 보유한다. 적재 실패 또한 보유 물품의 상태가 불확실하므로 반환하지
않는다. 이 API의 동작 결과와 인증은 **호출자의 합성 세션**을 전제로 하며
실제 장비의 보고나 물리적 상태를 증명하지 않는다.

`server.team_shelf_sim_dispatch.TeamShelfSimDispatch`는 위 예약 이후 기존
`SimRuntime`의 방향 기반 경로 계산·구간 예약·격리 MQTT를 사용하여 가상
로봇을 N01의 스퍼 D01로 보낸다. `sim-` 로봇, loopback 브로커, 원본 v3 맵만
허용한다. 슬롯 장부에 발행 시도를 먼저 기록하고 동일 주문의 중복 발행을
막는다. 발행 결과가 불명이면 두 칸을 유지하고 주문을 `unknown`으로 둔다.
슬롯 장부와 기존 런타임 DB/MQTT는 단일 트랜잭션이 아니다. PUBACK은 로봇의
도착·집기·적재 성공이 아니다.

현재 **기존 웹 주문 API, Unreal의 집기·적재 메시지 또는 실제 로봇에는
연결되지 않는다**. 기존 Unreal의 grab에는 출발 `slot_id`가 없고 기존 웹
상품 모델은 출발 노드마다 한 종류만 허용한다. 팀 4종이 N01을 공유하므로
그대로 연결하면 잘못된 상품·슬롯을 집을 수 있다. 같은 슬롯 선택·결과를
웹 주문, 로봇/Unreal 동작 메시지와 인증된 응답까지 안전하게 전달하는
연동이 별도로 필요하다. 이 실험은 실물 발행 제한을 해제하지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv\pr59-test-temp).Path
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_team_shelf_order_slots `
  server.tests.test_team_shelf_sim_dispatch -v
```
