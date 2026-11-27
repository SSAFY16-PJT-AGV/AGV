# 격리 가상 AGV 자동 우회

`--auto-reroute-sim`은 기존 웹 시연에서만 명시적으로 선택하는 실험 기능이다.
서버는 로컬 격리 브로커와 가상 로봇에 한해 구간 충돌 대기, 방향별 후보,
기존·새 구간의 공동 점유, MQTT 요청과 수락, 실제 가상 로봇 tick, 새 후단
이탈 이벤트를 확인한다. 새 이탈이 확인되면 기존 구간을 해제하고 도착 노드는
점유한 채 다음 상품 단계로 인계한다. 구 경로와 기존 이벤트는 보존한다.

다음은 기존 합성 세계 실행에 추가하는 예다. 상태 경로는 마지막 폴더가
존재하지 않는 새 경로여야 한다. `--field-config`에 지정된 맵·좌표·상품은
가상 대응으로 검증된 것을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.order_demo `
  --map 'C:\경로\Content\Data\warehouse_map.json' --synthetic `
  --field-config server\examples\field_unreal_demo.json `
  --state-dir 'D:\관통\synthetic-reroute-01' --auto-reroute-sim
```

Unreal 기반 가상 시연에도 기존 `--editor`, `--project`, 해당 맵의
`--field-config`와 이 옵션을 함께 지정할 수 있지만, 자동 우회가 켜진 실제
Unreal 화면과 팀 v3 맵은 별도 시연 검증이 필요하다. 상태 API의
`automatic_reroute`는 `isolated_simulation`을 표시한다. 브로커는 실행기가
만든 루프백 전용 브로커여야 하며 실제 장비·공용 브로커에 사용하면 안 된다.
우회 수락·이탈 확인을 기다릴 때는 `reroute_wait`, 가상 로봇이 실제로
이동하는 동안에는 `reroute_moving` 상태를 표시한다.

현재 범위와 실패 처리:

- 중간 대기점이 있는 우회는 출발 전에 전체 우회 경로와 기존 구간을 함께
  원자적으로 예약한다. 각 중간 정지의 새 이벤트와 순번을 확인해 다음
  명령을 한 번만 발행하며, 마지막 후단 이탈 전까지 전체 점유를 유지한다.
  경로의 먼 구간이 사용 중이면 대기할 수 있고 처리량 향상을 보장하지 않는다.
- 기존 복구 challenge는 기본 30초 후 만료된다. 우회 중에는 만료 전 유효한
  기존 증거와 같은 연결·실행 이력·점유를 확인한 실제 가상 정지점에서만
  별도의 짧은 수명 증거를 발급한다. 일반 구간 대기 중에도 유효한 기존
  challenge와 현재 가상 로봇의 정지·명령·점유가 일치할 때만 새 challenge를
  발급하며, 실제 발급된 경우에만 대기 기한을 갱신한다. 연속 예약 대기는 최대
  120초이며 그 이후 안전하게 중단한다. 만료된 증거의 소급 연장이나 불명
  결과의 자동 재발행은 하지 않으며, 서버 재시작 후 증거를 복원하지 않는다.
  집기·적재 ACK가 유효한 증거보다 늦게 도착하면 인계를 차단한다.
- 발행·집기·적재 결과가 불명이거나 연결이 끊기면 작업은 불명/대기로 멈추고
  필요한 점유를 유지한다. 같은 요청을 다시 보내기 전에 상태를 확인해야 한다.
- 영속 팀 6선반 모드에서는 우회한 상품 인계를 포함해 **완료된** 주문을
  복원할 수 있다. 기존 상태 폴더를 보존하고 같은 Unreal 월드를 유지한 채
  서버를 재시작해야 한다. 저장된 주문·선반 동작·가상 로봇 명령과 우회
  기록을 대조하며, 같은 월드의 24칸과 양쪽 로봇의 위치·방향·상품 소지
  상태를 새로 확인하기 전까지 주문과 재고는 불명이고 신규 주문은 막는다.
  누락·불일치·진행 중 작업이나 결과 불명은 자동으로 재개하지 않는다.
  여러 로컬 DB를 함께 악의적으로 변경한 기록을 암호학적으로 인증하거나
  실물 장치의 안전을 증명하는 기능은 아니다. 다른 빨강/파랑 2선반 모드의
  우회 인계 기록은 계속 자동 복구가 차단된다. 확인이 막힌 팀 선반 기록의
  읽기 전용 조사 방법은 `server/docs/team_shelf_recovery_inspection.md` 참고.
- 격리 합성 맵과 합성 세계에서 두 웹 주문의 경합·우회·가상 집기/적재를
  시험했다. 두 작업의 첫 구간을 동시에 배정하도록 시험에서 실행 순서를
  조정했다. 실제 물리 위치·후단 이탈·센서·실물 운행이나 장치 인증의 증거가 아니다.
- 별도로 팀 v3 맵의 가상 대기점 사본에서 가상 시작점 J02/N02와 목적지
  J07/N01을 지정한 MQTT 시험에서 두 가상 로봇의 경합과 우회 후 목적지
  도착을 확인했다. 기본 시작점, 웹 상품·Unreal 화면 또는 실물은 이 시험의
  범위가 아니다. 재현: `python -m unittest server.tests.test_sim_reroute_auto_v3`
  (별도 격리 aMQTT 경로 `AMQTT_TEST_PATH` 필요).
- 동일한 v3 가상 사본에서 N01/D02에서 출발하는 두 AGV의 경합 중 한 대가
  `N07→N01→J02→J07→N03` 다중 구간을 우회하고 두 대가 목적지에 도착하는
  MQTT 시험도 수행했다. 시작점·대기점은 가상이고 action은 `none`이다.
- 팀 v3 가상 맵에서 가상 시작점 N05/J02와 시험용 동시 배정 순서를 사용해
  웹 주문 두 건의 다중 구간 우회·합성 집기/적재와 후속 빨강 주문 완료를
  검증했다. 실제 Unreal 화면, 기본 시작점과 실물 로봇 시험은 아니다. 재현은
  `python -m unittest server.tests.test_sim_reroute_multi_auto_web`이며 격리
  aMQTT 경로 `AMQTT_TEST_PATH`가 필요하다.
## 실제 Unreal 렌더링 격리 시험

`server.tools.v3_rendered_reroute_test`는 별도 Unreal v3 시험 사본,
새 SQLite DB, 루프백 전용 MQTT와 웹 주문 API를 사용한다. 실제 상품·장비가
아닌 가상 배치다. 두 AGV 시작점 N05/J02와 두 주문이 배정될 때까지
진행을 보류하는 테스트 전용 장벽은 경합을 재현하기 위해 설정한다. 그
이후 이동은 가상 AGV가 격리 MQTT 명령을 처리하고 서버가 Unreal에
WebSocket 상태를 보내는 경로다. 초기 배치는 주행으로 간주하지 않는다.

준비는 `field_v3_unreal.md`의 **독립 Unreal 프로젝트 준비**를 먼저 따른다.
기존 v3 패치 스택의 별도 사본에서만 `unreal_v3_reroute_fixture.patch`를
`git apply --check`로 확인하고 적용한 뒤 빌드한다. 이는 상품 픽업을
N01/N04, 빨강/파랑 선반을 D01/D04로 옮긴 시험 전용 UE 검증 설정이다.
기존 빨강·파랑 두 품목과 선반 두 개를 쓰며, 팀 6선반 SKU 모드의
자동 우회 시험이 아니다.
원본 팀 프로젝트나 다른 사용 중인 프로젝트에 적용하지 않는다. 동일한
독립 사본에서 `AGV_V3_SCENE_VARIANT=reroute`를 설정해 기존
`create_v3_unreal_scene.py`와 `verify_v3_unreal_scene.py`를 차례로
실행하고 저장·재개방 성공 마커를 확인한다. 사본 Content는 공유 링크가
아닌 독립 디렉터리여야 한다.

8000 포트가 비어 있을 때 다음을 실행한다. 실행마다 이전 결과가 없는
새 `--output-dir`을 지정한다. Unreal 게임 창을 띄우며 해당 도구가 시작한
프로세스만 종료한다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.v3_rendered_reroute_test `
  --unreal-editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --uproject 'D:\경로\독립-Unreal-사본\agvSimulation.uproject' `
  --ddc 'D:\경로\독립-DDC' --output-dir 'D:\경로\새-시험-결과'
```

두 주문 완료, 로봇별 Unreal 수신 위치 변화, 집기·적재 요청과 동기화된
재고·선반 슬롯, DB의 완료된 다중 구간 우회를 모두 검사한다. 결과와
Unreal 로그 및 `reroute-db-evidence.json`은 출력된 증거 폴더에 남는다.
시각적 장면 관찰은 자동화하지 않으며 로그 검사만으로 화면을 보았다고
주장하지 않는다. 장치의 물리적 정지·충돌 안전을 검증하는 시험이 아니다.
