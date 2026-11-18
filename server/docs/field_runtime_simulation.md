# 팀 맵 경로의 서버 연동 시험

기존 `SimRuntime`과 `Coordinator`, SQLite 구간 점유·진행·복구, MQTT 명령을
방향이 있는 팀 맵 형식에 연결하는 **격리 가상 시험**이다. 노드를 재방문하는
경로는 노드 이름뿐 아니라 방문 순번을 저장하고, 다음 구간의 예약·명령과
복구 검증에 사용한다. 팀 로봇이나 실물 브로커에 대한 운행 허가가 아니다.

`AGV_SIM_CONFIG` JSON에 `field_simulation: true`를 명시해야 한다. 기본값은
`false`이며 구성은 기존 격리 시뮬레이션과 동일하게 `database`, `map_file`,
`robots`, `mqtt_port`, `mqtt_client_id`를 요구한다. `robots`에는 실행 전
로봇·작업·세션을 명시해야 한다. 별도의 `AGV_SIM_API_TOKEN`으로 인증하는
기존 `/simulation` API에서 작업 생성 후
`POST /simulation/tasks/{task_id}/prepare-field`에 목적지 `destination`,
`candidates`(각 `robot_id`, `node`, `incoming_edge`, `available`),
`action`을 제공하고 기존 publish API로 명령을 전송한다. 런타임은 미리
해당 작업에 연결된 가상 로봇만 받아들이므로, 웹 주문의 동적 차량 배정과는
다르다. 입력한 진입 간선과 정지 증거는 시험에서 제공한 가정이다.

브로커 연결이 복구되면 살아 있는 가상 로봇의 checkpoint와 서버 점유·방문
순번이 일치하는 경우 허가된 진행 구간을 재개한다. 서버 프로세스 재시작 시
기존 작업은 차단되며, 이름이 같은 노드만으로 이동 중 위치를 추정하거나
자동 재개하지 않는다. `SimHandoffStore`를 사용하는 기존 웹 상품 주문·Unreal
시연과 이 팀 맵 경로는 아직 연결되어 있지 않다. 실물 위치와 정지·후단 이탈을
증명하는 규격, 맵의 실제 안전 대기 지점, 로봇과 Unreal 맵의 좌표 정합이
확정되기 전에는 공유 또는 실물 장치에 사용하지 않는다.

격리 aMQTT를 별도 의존성 폴더에 설치한 환경에서 저장소 루트 기준 시험:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_field_runtime_mqtt_integration -v
```

실제 팀 저장소의 `maps/mat_ssafy_v2.json` 복사본을 쓰는 추가 시험은
`AGV_FIELD_REPO`를 해당 저장소로 지정하고
`server.tests.test_field_runtime_mqtt_team_map`을 실행한다. 이 시험은 복사한
맵에 **가상 대기점**을 추가하고 가상 로봇 및 가상 정지 증거를 사용한다.
추가된 대기점이 실제 맵의 안전 지점이라는 뜻이 아니다.
