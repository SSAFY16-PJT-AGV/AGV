# 가상 배송 도착과 적재 대기 관측

`observe_waiting_place`는 기존 in-process `ProductControlEngine`,
`SyntheticActionSession`, 실제 실행 중인 격리 `SimRuntime` 및 해당 가상
MQTT 로봇이 연결된 상태만 읽는다. 호출자가 `simulation=True`를 명시해야
하며 이 함수가 브로커에 연결하거나 place를 발행하지 않는다. 외부 로봇,
하드웨어나 UE 결과의 출처 인증, 물리 위치·정지, 집기·적재 성공, 운행
허가에는 사용할 수 없다. 합성 결과 피어와 웹 주문은 아직 이 경계에
연결되지 않았다.

합성 pickup 결과의 applied 주장·주문/slot과 엔진의 상품·pickup·목적지,
같은 grab action ID가 실제 `sim_handoff_transfers`의 pickup→delivery
기록과 일치하는지 대조한다. 이후 실제 delivery의 active robot, 맵,
최종 node 점유, acked 주문 이력, inbox/progress의 applied 최종 event,
live gate 세대, 가상 executor의 완료 receipt·pose와 엔진의 pending place를
검사한다. 팀 맵의 반복 방문은 `SegmentProgress`의 기존 순번 검증과
executor의 방문/해제 인덱스를 함께 확인한다. 주문 receipt digest와
rear_clear 이벤트 digest는 각 원본과 별도로 비교한다.

합성 주문·pickup 조회는 읽기 전용 연결을 사용하며 잘못된 request 등의
조회 실패가 synthetic session의 진행 gate를 변경하지 않는다. allocation
DB는 기존 gate transaction의 write 예약과 receiver 잠금을 사용해 일관성을
대조하므로, 관측이 끝날 때까지 기존 가상 이벤트 처리가 지연될 수 있다.
서버 runtime DB 소유권을 관측 중 고정하고 robot, runtime, receiver,
gate, executor 및 inbox의 기존 락 순서를 따른다. 관측 중 또는 직후의
단절이 물리적으로 발생하지 않았음을 보장하지 않는다. synthetic 주문 DB,
pickup DB, allocation DB, 가상 executor inbox는 **별도 시점과 별도
트랜잭션**에서 관측한다. 따라서 결과가 세 DB의 원자적 스냅샷이라는
뜻은 아니다. 같은 action ID 등을 가진 독립된 합성 입력을 대조할 뿐,
synthetic 입력이 실제 engine handoff를 일으켰다는 인과관계나 출처 진위를
증명하지 않는다. 반환값은 `snapshot_consistency: separate_databases`,
`causality: not_established`, `control_effect: none`,
`valid_for_later_action: false`를 명시하는 즉시 만료 관측이다.

이후에 별도의 place 시도 기록이나 결과·slot 갱신을 연결하려면 현재
관측값을 재사용하지 않고 세 DB 및 현재 gate/engine 상태를 해당 거래의
잠금 아래 다시 검증해야 한다. 그 연결과 DB·MQTT·가상 세계의 장애 처리,
전원 장애에서의 전체 원자성은 이 기능에 포함되지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv\Scripts\python.exe -m unittest server.tests.test_synthetic_place_observation_mqtt -v
```

시험은 테스트 소유 루프백 aMQTT와 가상 로봇·합성 pickup 결과를 사용한다.
팀 맵 사본의 대기점은 가상이며 실제 안전 대기점이나 장비 증거가 아니다.
