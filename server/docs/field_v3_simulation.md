# #62 팀 맵 v3 격리 시연

이 예제는 팀 맵의 경로 계산과 기존 가상 작업 배정·예약·MQTT, 웹 주문 및
Unreal 통신 형식을 시험하기 위한 것이다. **실물 운행 설정이 아니다.**
별도 합성 세계나 Unreal 대역을 사용하며, 실제 장비의 위치·정지·후단 이탈,
실측 재고 또는 안전한 구간 인계를 증명하지 않는다.

## 맵과 Unreal의 차이

`examples/mat_ssafy_v3.json`은 팀 `feature/field-run` 커밋
`7e6e6bb741d35a490d8c14d56cb92d2544c4976b`에 포함된 #62 맵 원본이다.
20개 노드와 22개 간선이 있다. 팀 Unreal의
`feature/digital-twin` 커밋 `ce23ec88f8caceb90eaea73931801bb7d0ff99bd`의
`Content/Data/warehouse_map.json`은 구성과 좌표가 다르다. 예를 들어
v3의 `N08`, `D08`이 Unreal 원본에 없고 `N07`의 색과 위치도 다르다.
서로 다른 맵을 노드 이름이나 두 기준점만으로 일치한다고 취급하지 않는다.
v3 매핑은 서버 시작 시 전체 노드와 간선 및 좌표를 검증한다.

`mat_ssafy_v3_simulated_waits.json`은 원본의 노드·간선은 그대로 두고
N01~N08과 J03/J07에 **시험용 가상 대기점**만 추가한 사본이다.
`mat_ssafy_v3_unreal_sim.json`은 사본에서 만든 Unreal 좌표 형식의
**가상 투영 맵**이며 팀이 제작한 Unreal 레벨이 아니다. 설정 파일
`field_unreal_v3_sim.json`은 가상의 초기 위치, 빨강·파랑 상품, 픽업과
적재 위치 및 여섯 칸 선반을 지정한다. 이 값들은 실측 또는 팀 합의가 아니다.

원본의 `E3(J03–N04)`는 논리 경로 비용이 0이지만 도형 길이는 약 55.8mm다.
경로 비용과 화면 보간은 구분하며 실제 주행 거리나 정지 가능성의 근거가
아니다. 맵이 달라지면 대응 설정과 테스트도 다시 검토해야 한다.

## 합성 웹 시연

aMQTT 의존성을 `.venv/broker-deps`에 설치한 환경에서 프로젝트 루트에서
실행한다. `$newState`는 **아직 존재하지 않는 새 경로**여야 한다.

```powershell
cd D:\관통\AGV-analysis
$newState = 'D:\관통\v3-synthetic-run-01'
.\.venv\Scripts\python.exe -m server.tools.order_demo `
  --map server\examples\mat_ssafy_v3_unreal_sim.json `
  --field-config server\examples\field_unreal_v3_sim.json `
  --synthetic --state-dir $newState
```

`http://127.0.0.1:8000/order-demo/`에서 주문하고 Ctrl+C로 종료한다.
이 모드는 Unreal을 실행하지 않는다. 메모리 세계가 종료되면 기존 상태
경로를 다시 사용하지 않으며, 다른 새 경로로 시연한다. 실행 조건과 불명
결과의 제한은 [합성 웹 시연 안내](synthetic_web_order_demo.md)를 따른다.

## Unreal 및 시험 범위

가상 투영 맵이 Unreal의 JSON 입력 형식과 맞는다는 것과 서버 WebSocket의
pose·집기·적재 메시지는 Unreal **대역**을 이용해 검증한다. 파일을 기존
Unreal 프로젝트에 복사하는 것만으로 레벨의 선반·상품·로봇 액터가 v3에
배치되는 것은 아니다. v3에 맞춘 실제 팀 Unreal 레벨, 액터의 위치 및 집기·
적재 설정을 팀과 맞추고 실제 화면에서 시험하기 전에는 정합을 주장하지 않는다.
기존 팀 Unreal 원본 파일은 수정하지 않았다.

```powershell
$env:TEMP = (Resolve-Path .venv\pr59-test-temp).Path
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_v3_example `
  server.tests.test_v3_web_unreal_integration server.tests.test_v3_contention -v
```

경합 시험은 두 가상 AGV가 공통 노드 J07을 요구하도록 한다. 첫 가상
로봇이 점유하는 동안 두 번째 예약과 명령을 막고, 합성 후단 이탈 뒤
두 번째가 진입하는지 확인한다. 이는 물리적 정지·후단 이탈의 증거가 아니다.
