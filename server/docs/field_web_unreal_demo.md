# 팀 맵 경로를 쓰는 격리 웹·Unreal 시연

`server/examples/field_unreal_demo.json`은 웹 주문과 기존 서버의 경로 계산·
구간 예약·MQTT 가상 AGV·Unreal WebSocket을 연결하기 위한 **가상 예제**다.
빨강 상품 하나를 `P_orange`에서 집어 `P_purple`로 운반한다. 두 가상 AGV가
있지만 이 예제는 빨강 상품만 등록하므로 한 번의 주문을 두 대가 동시에
처리한다는 뜻은 아니다. Unreal에서는 각각 `D01`, `D04`, `SHELF_RED`에
대응시키며 빈 슬롯을 사용한다.

이 대응은 팀이 실측하거나 승인한 지도가 아니다. `P_orange → D01`과
`P_purple → D04`라는 두 점을 임의로 맞춘 회전·이동·축척 변환이다. 그 외
경로와 Unreal 창고 지형의 정합이나 실제 물품 위치를 증명하지 않으며,
실물 로봇이나 공유 브로커로 명령을 보내는 용도로 사용하지 않는다.

## 예제 맵의 출처와 가정

`mat_ssafy_v2_simulated_waits.json`은 팀 브랜치
`origin/feature/field-run` 커밋 `ef9e60001a0eb67c21fb81ea10c262eec343a4e6`의
`maps/mat_ssafy_v2.json`을 복사한 뒤 `zones`에 아래 가상 대기점을 추가한
것이다. 맵의 노드·간선·좌표와 기존 구역은 바꾸지 않았다.

| 추가된 가상 대기 노드 | 용도 |
| --- | --- |
| `P_blue`, `P_green` | 가상 로봇 시작 위치 |
| `P_orange`, `P_purple` | 가상 픽업·배송 위치 |
| `P_yellow`, `P_red`, `M3`, `M1` | 가상 경로 중간 정지점 |

이 대기점들은 팀 맵에 원래 없으며 실제 안전 정지 가능 지점이 아니다.
가상 시작 위치의 진입 간선은 `sim-r1: E12 → P_blue`,
`sim-r2: E6 → P_green`으로 설정했다. 입력 엣지의 끝점과 시작 노드는
런타임에서 검증하지만, 실제 로봇의 현재 위치와 방향을 관측한 값은 아니다.

원본 Git 파일의 SHA-256은
`aacaeed25ff3cddf31dfd17a3197f29741940ca0a54bb90cf1dba8a29ea5561b`이다.
예제의 `field_map_sha256`은 파일 바이트가 아닌 맵 JSON을 키 정렬,
ASCII 이스케이프 및 공백 없는 구분자로 직렬화한 뒤 계산하며, 맵이 바뀌면
설정과 일치하지 않아 시작을 거부한다.

## 실행

먼저 [기존 시연 준비](../README.md)의 서버 의존성, 별도 aMQTT 의존성,
Unreal 패치 6개를 적용한 UE 5.7 프로젝트를 준비한다. 아래 `$map`은
Unreal 프로젝트가 실제로 읽는 `warehouse_map.json`과 같은 파일이어야 한다.
예제의 Unreal 맵 ID는 `warehouse_01`이며 `D01`, `D04` 노드가 필요하다.

```powershell
cd D:\관통\AGV-analysis
$map = 'C:\경로\Content\Data\warehouse_map.json'
$editor = 'C:\경로\UnrealEditor.exe'
$project = 'C:\경로\agvSimulation.uproject'
.\.venv\Scripts\python -m server.tools.order_demo `
  --map $map --editor $editor --project $project `
  --field-config server/examples/field_unreal_demo.json
```

Unreal에서 `Content → AGV → NewMap`을 열고 플레이한 다음
`http://127.0.0.1:8000/order-demo/`에서 빨강 상품을 주문한다. 실행기는
격리 브로커와 로컬 서버를 시작하며, Ctrl+C를 누르면 이 프로세스들을
정리한다. Unreal은 별도로 닫는다. 결과가 불확실한 작업은 자동으로
재발행하지 않는다.

영속 상태 기본 경로는 `data/field-order-demo-state/`로 기존 시연과 구분된다.
다른 실험에는 `--state-dir`로 별도 폴더를 지정한다. 저장된 주문·선반·
가상 로봇 상태와 현재 Unreal 월드가 다르거나 구동 중단 결과가 불명확하면
자동 운행 대신 복구를 보류한다. 기존 상태 폴더를 임의로 삭제하지 않는다.

맵이나 대응점을 수정할 때는 `field_map_sha256`을 맵의 변경된 내용에 맞춰
다시 계산하고, 두 앵커와 상품 위치가 같은 변환에서 `tolerance_m` 이내로
맞는지 확인해야 한다. 파일의 상대 맵 경로는 설정 JSON이 있는 폴더 기준이다.

## 시험

아래 통합 시험은 포함된 팀 맵 사본과 별도 MQTT, Unreal **대역**을 사용한다.
실제 Unreal 렌더링·실물 주행 시험이 아니다.

별도 실험에서 위와 같은 두 점 대응과 가상 대기점을 사용해 실제 UE 5.7
게임 프로세스를 `-nullrhi`로 실행했다. 웹 주문에 대해 실제 Unreal의 집기와
선반 0번 적재 성공을 확인했고, 같은 게임 월드를 유지한 채 서버를 재시작한
뒤 주문·재고 11개를 복원하고 두 번째 주문을 선반 1번에 적재했다. 이는
화면 렌더링 또는 실물 로봇 안전 시험이 아니며, 두 점 외 창고 지형은
보정하지 않았다. 테스트 결과·DB는 Git에 포함하지 않는다.

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python -m unittest server.tests.test_field_twin_mapping `
  server.tests.test_field_web_handoff server.tests.test_field_web_unreal_integration -v
```

다른 팀 맵 원본으로 검증하려면 `AGV_FIELD_REPO`를 해당 소스 사본으로
지정한다. 로봇 패치를 적용하는 별도 시험은 팀 소스 사본이 필요하다.
