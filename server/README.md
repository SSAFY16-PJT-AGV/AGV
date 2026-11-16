# 관제 서버

현재 진행 상황과 검증 범위는 [서버·통신 진행 보고](docs/progress_report.md)를 참고한다.

FastAPI와 MQTT 기반 관제 서버의 초기 구현이다. MQTT 상태 수신과 별도로
경로 계산, 구간 예약 자료구조, 기록·재생 구성요소를 단계적으로 개발한다.

## 준비와 실행

저장소 루트에서 Python 가상환경을 만들고 의존성을 설치한다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r server/requirements.txt
```

저장소 루트의 `.env`에 `MQTT_HOST`, `MQTT_PORT`를 설정하고, 해당 MQTT
브로커를 실행한 뒤 서버를 시작한다. 브로커 접속 정보와 비밀번호는 저장소에
커밋하지 않는다. 팀 브로커의 실제 주소와 포트, 인증 및 CA 설정은 사용 전
확인한다.

상태 수신기와 격리 시뮬레이션의 이벤트 수신·명령 발행기는 선택적으로
`MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS_CA`를
읽는다. 인증을 사용하려면 신뢰할 CA 파일 경로인 `MQTT_TLS_CA`가 반드시
필요하며, `MQTT_PASSWORD`가 설정돼 있으면 비어 있지 않은 `MQTT_USERNAME`이
필요하다. 사용자명만 설정하는 경우에도 TLS CA가 필요하다. 사용자명과
비밀번호 없이 `MQTT_TLS_CA`만 설정하는 TLS 전용 연결도 가능하다. 예를 들어
`.env`에 아래와 같이 설정한다(주소·포트·경로와 인증 정보는 예시다).

```dotenv
MQTT_HOST=broker.example.invalid
MQTT_PORT=8883
MQTT_TLS_CA=D:/path/to/trusted-ca.pem
MQTT_USERNAME=example-user
MQTT_PASSWORD=replace-with-private-value
```

TLS만 쓴다면 사용자명과 비밀번호 설정을 모두 제거한다. 인증 또는 TLS를
사용하지 않는 격리 브로커에 연결할 때는 세 선택 설정을 모두 제거해 기존
연결 방식으로 사용한다. `MQTT_PASSWORD`는 빈 값으로 설정해도 설정된 것으로
취급되므로 사용하지 않을 때는 항목 자체를 제거한다. `MQTT_USERNAME` 또는
`MQTT_TLS_CA`를 명시했다면 빈 값이나 공백만 넣을 수 없다. 인증 설정 조합이나 CA
파일에 오류가 있으면 브로커 접속 전에 실패하며, 자격증명이 잘못되면 브로커가
접속을 거부한다. 서버는 브로커 인증서와 호스트 이름 검증을 유지한다.
관측 브로커와 시뮬레이션 브로커가 다르더라도 이 클라이언트들은 같은
인증정보와 CA 설정을 사용한다. 시뮬레이션 런타임은 생성할 때 설정을 읽으므로
인증정보를 변경하려면 런타임을 다시 시작해야 한다. 시뮬레이션 브로커는 기존대로
`127.0.0.1`에 한정되며 TLS를 사용할 때 인증서가 이 주소에 유효해야 한다.
이 지원은 격리 시뮬레이션 전용으로, 실제 로봇 운행을 의미하지 않는다.

```powershell
.\.venv\Scripts\python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

기존 셸 환경변수는 `.env`보다 우선한다. 최초 브로커 소켓 연결이 실패하면
서버 시작도 실패한다. FastAPI 시작 완료는 MQTT 구독 완료를 보장하지 않으며,
구독 준비 여부는 내부 `app.state.mqtt_service.snapshot()`의 `ready`로 확인한다.
읽기 전용 `GET /robots` 조회는 팀 협의 전 제안이며 인증이 없으므로 로컬에서만
사용한다. 응답의 의미와 한계는 [HTTP API 제안](docs/http_api_proposal.md)을
먼저 읽는다.

## 구현 상태와 제한

- `mqtt_client.py`: `agv/+/state` 및 `agv/+/conn` 수신. 토픽의 로봇 ID별
  관찰값은 프로세스 메모리에만 있다. 보관 메시지와 통신 두절 전 관찰값은
  위치 확정이나 주행 허가의 근거가 아니다. payload, 중첩 깊이, 로봇 ID와
  로봇 개수의 임시 상한은 코드에 명시되어 있으며 팀 협의 후 조정한다.
  현재 허용된 로봇 목록 및 상한 초과 경보가 없고 빈 retained 삭제 메시지도
  마지막 관측을 지우지 않는다. `conn`은 UTF-8 `online`/`offline`만 받는다.
  연결 메시지 표현과 인증은 팀과 협의해야 한다.
- `routing.py`: 맵 그래프의 폴리라인 길이로 최단 경로 계산. 노드 위치와
  엣지 끝점은 임시로 정확한 일치를 요구한다. 실제 주행 명령에는 미연결.
- `dispatch.py`: 호출자가 가용성과 위치를 검증한 후보 중 상차 지점까지의
  최단 거리로 로봇을 선택한다. 실제 배정·예약은 하지 않는다.
  [배정 후보 안내](docs/dispatch.md)를 먼저 읽는다.
- `reservations.py`: 단일 프로세스에서 자원 집합을 원자적으로 예약하는
  자료구조. [예약 제한 사항](docs/reservations.md)을 먼저 읽는다.
- `tasks.py`: 단일 프로세스에서 작업 상태 전이를 기록하는 자료구조.
  [작업 상태 제한 사항](docs/task_state.md)을 먼저 읽는다.
- `task_store.py`: SQLite 파일에 작업 상태를 저장하는 독립 저장소.
  [저장소 안내](docs/task_store.md)를 먼저 읽는다.
- `coordinator.py`, `segments.py`, `allocation_store.py`, `order_publisher.py`:
  첫 이동 구간의 배정·예약·주문을 연결한다. **기본 비활성화된 격리
  시뮬레이션 전용**이며, 명시적으로 시뮬레이션 설정을 켠 FastAPI에서만 연결된다.
  실행 조건과 제한은 [통합 안내](docs/coordinator.md)를 먼저 읽는다.
- `recovery.py`, `segment_progress.py`, `progress_publisher.py`: 가상 로봇의
  재동기화, 후단 이탈 후 다음 구간 예약 및 후속 주문을 처리한다. 새 이벤트와
  복구 메시지는 [팀 협의용 제안](docs/segment_recovery_proposal.md)이며,
  실제 로봇 통신 규격으로 확정되지 않았다.
- `mqtt_log.py`: 기록 및 격리 재생 구성요소. [기록·재생 안내](docs/mqtt_logging.md)를
  먼저 읽는다. 재생은 설계의 원본 토픽 대신 `replay/` 접두사를 사용한다.
- `mqtt_capture.py`: 별도로 실행하는 MQTT 기록 수집기.
  [수집기 안내](docs/mqtt_capture.md)를 먼저 읽는다.

구간 진행과 재동기화도 격리 시뮬레이션에서만 연결되어 있다. 실제 로봇
운행을 위한 후단 이탈 증명, 이벤트 인증, 점유 재동기화, 주문 중복 처리와
물리적 정지는 팀 간 프로토콜 합의와 실물 검증이 필요하다. 기본 설정으로는
시뮬레이션 workflow가 시작되지 않는다.

## FastAPI에서 격리 시뮬레이션 켜기

실물과 공유하지 않는 독립 로컬 MQTT 브로커와 가상 로봇을 사용한다.
루프백 주소나 `sim-` 이름만으로 브로커가 격리되었다는 보장은 없다.
관측 서비스도 실물 브로커와 분리하려면 `.env`의 `MQTT_HOST`와 `MQTT_PORT`를
해당 시뮬레이션 브로커로 설정한다. 토큰과 접속 설정은 커밋하지 않는다.

1. `.env`에 `AGV_SIM_CONFIG`로 아래 설정 파일 경로를, `AGV_SIM_API_TOKEN`으로
   임의로 생성한 32자 이상의 공백 없는 ASCII 토큰을 지정한다. PowerShell에서
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`로 만들 수 있다.
2. 저장소 루트에 무시 대상인 `.env.sim.json`을 만들고 예시처럼 설정한다.
   아래 브로커 포트는 **예시**이며 실제 장비가 사용하지 않는 별도 포트를 쓴다.
   상대 경로는 이 JSON 파일이 있는 폴더를 기준으로 한다.

```json
{
  "database": "sim-test.sqlite3",
  "map_file": "sim/examples/two-segment-map.json",
  "robots": {"sim-r1": {"T1": "boot-1"}},
  "mqtt_port": 18883,
  "mqtt_client_id": "isolated-sim-server"
}
```

3. 브로커를 먼저 실행하고 다음 명령으로 서버를 **단일 프로세스**로 시작한다.
   관측 서비스의 `MQTT_HOST`, `MQTT_PORT`도 `.env`에 있어야 한다. 여러 worker나
   reload, 역방향 프록시는 사용하지 않는다. DB별 잠금은 중복 기동을 막기 위한
   보조 장치이며 네트워크 격리나 장치 인증을 대신하지 않는다.

```powershell
.\.venv\Scripts\python -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

4. `http://127.0.0.1:8000/docs`에서 **Authorize**를 눌러 토큰 값만 입력한다.
   `/simulation/status`가 준비 상태를 보여 주고, 작업 생성 → 준비 → 첫 주문
   발행을 각각 호출할 수 있다. 경로와 요청 값은 [API 제안](docs/sim_http_proposal.md)을
   참고한다. `ready`는 브로커 구독 준비를 뜻하며 로봇의 위치나 안전 확인이 아니다.

고정 로봇·작업·세션 매핑은 시작 시 설정한다. 로봇 위치와 가용성은 별도로
확인한 시뮬레이션 값이어야 한다. 첫 주문 후 새로운 challenge와 가상 로봇이
제공한 상태로 복구 확인을 해야 후단 이탈을 처리한다. 서버 재시작 후 남은
이벤트는 조회·재처리하며, 재처리로 생성된 후속 주문은 별도로 submit한다.
`uncertain` 주문과 재시작 전 준비만 된 첫 `pending` 주문은 자동 발행하지 않는다.
후자는 아직 API에서 안전한 취소·재배정을 제공하지 않으므로 예약을 유지하고
수동으로 상태를 확인해야 한다. MQTT PUBACK은 실행 완료가 아니다.

수신 세션이 단절되거나 세션이 다르면 기존 복구 확인을 폐기한다. 수신 이벤트는
SQLite에 먼저 저장되지만, 브로커 재시작 이후까지 MQTT 미전달 메시지가 보존되는지
여부는 브로커의 영속 세션 설정에 달려 있다. 가상 로봇의 재전송 규칙도 별도로
확인해야 한다.

## 테스트

```powershell
.\.venv\Scripts\python -m unittest discover -s server/tests -v
.\.venv\Scripts\python -m unittest discover -s server/demo -v
.\.venv\Scripts\python -m unittest discover -s sim/tests -v
```

단위 테스트는 실제 로봇과 모터를 사용하지 않는다.

## 웹 주문 → 가상 AGV → Unreal 시연

서버 PC에서 사용할 수 있는 로컬 웹 시연이다. 기본 실행기는 두 가상 로봇을
`sim-r1`/J01과 `sim-r2`/J05에서 시작하고 Unreal `AGV_01`과 `AGV_02`에 각각
연결한다. 빨간 상품 또는 파란 상품을 주문하면 서버가 대기 중인 로봇의 현재
위치에서 픽업 지점까지의 그래프 거리를 비교해 배정하고, 픽업 후 선반까지의
경로도 계산한다. 한 로봇이 주문을 처리하는 동안 다른 로봇에도 주문할 수 있다.
각 로봇은 한 번에 한 주문만 처리한다.

두 로봇은 하나의 격리 예약 DB를 공유한다. 시작 위치와 작업 완료 위치도 점유로
보존하며, 다른 작업이 예약한 노드·간선·구역과 충돌하면 새 구간을 발행하지 않고
대기한다. 각 가상 로봇은 `agv/sim-r1/order` 또는 `agv/sim-r2/order`의 MQTT
명령을 별도 inbox로 검사한 뒤 `released_until`까지만 이동한다. 정지 후 가상
증명과 `rear_clear` 이벤트를 거쳐 다음 구간을 예약·발행한다. 명령 수락과 이동
갱신 때마다 명령·순번·허가 지점 및 점유가 서버 예약과 일치하는지 검사한다.
불일치하면 시연을 중단한다. 가상 상태는 각각의 MQTT 토픽과 WebSocket 브리지를
거쳐 Unreal에 전달되며, Unreal의 기존 좌표 보간과 집기·적재 기능을 사용한다.

예약 충돌 시 기존 점유를 보존한 채 대기하며 예약을 다시 확인한다. 기본 30초
동안 진행되지 않으면 결과 불명(`unknown`)으로 중단한다. 이는 자동 교착 감지나
교착 해소 기능이 아니다. Unreal 집기·적재 액션은 한 연결에서 직렬로 보내며,
각 액션의 주문·동작·로봇 ID와 해당 로봇의 위치를 확인한다.

상품은 시연용 고정 매핑이다. 빨강은 D01에서 집어 D04 `SHELF_RED` 0번
슬롯에, 파랑은 D06에서 집어 D05 `SHELF_BLUE` 0번 슬롯에 적재한다. 각 슬롯은
한 번의 시연 실행에서 한 주문에만 사용할 수 있으므로 같은 상품의 재주문은
서버가 거부한다. 선반 슬롯을 초기화해 다시 시연하려면 서버와 Unreal을 함께
종료하고 다시 실행한다. 상품이 없거나 슬롯이 차 있으면 Unreal도 적용을 거부한다.
실제 Unreal Editor와 격리 MQTT 브로커를 연결한 두 로봇 시험에서는 빨강·파랑
주문을 동시에 처리하고 각각 집기·적재 성공 응답을 확인했다. 이 시험에서 두
로봇이 같은 구간에 경합하도록 강제하지는 않았다. 별도의 가짜 Unreal 연결과
격리 MQTT 브로커 통합 시험에서는 교차 구간 경합 시 한 로봇이 대기하는 것을
확인했다. 이전 단일 로봇 시험에서도 빨강과 파랑을 연속 주문해 각각 집기·적재
성공 응답을 확인했다. 이 결과들은 실물 충돌 방지를 검증하지 않는다.

필요한 것: 서버 Python 의존성, 별도로 설치한 `amqtt==0.11.3`, Unreal
창고 맵 JSON과 아래 세 패치를 적용해 빌드한 Unreal 테스트 프로젝트. 별도 aMQTT는
아래처럼 준비한다. 서버 브랜치에는 Unreal 엔진과 프로젝트가 포함되지 않는다.

```powershell
.\.venv\Scripts\python -m pip install --target .venv/broker-deps 'amqtt==0.11.3'
```

기존 Unreal 에디터를 닫고, 경로를 자신의 PC에 맞게 바꾼 뒤 저장소 루트에서
다음을 실행한다. Unreal 별도 테스트 사본에 순서대로 [로컬 인증 패치](docs/unreal_local_auth.patch),
[동작 결과 회신 패치](docs/unreal_action_ack.patch), [동작 위치 확인 패치](docs/unreal_action_position_guard.patch)를
적용하고 빌드해야 한다. 각 패치는 적용 전 `git apply --check`로 확인한다.
인증 패치는 `--unidiff-zero`도 필요하다. 위치 확인 패치는 결과 회신 패치 적용 후에
검사한다. Unreal 맵에는 `AGV_01`, `AGV_02` ID를 가진 두 AGV 액터가 필요하다.
Unreal에서는 `Content/AGV/NewMap`을 열고 플레이한다.

```powershell
$map = 'C:\경로\Content\Data\warehouse_map.json'
$editor = 'C:\경로\UnrealEditor.exe'
$project = 'C:\경로\agvSimulation.uproject'
.\.venv\Scripts\python -m server.tools.order_demo --map $map --editor $editor --project $project
```

브라우저에서 `http://127.0.0.1:8000/order-demo/`를 열고 상품을 선택한다.
웹 화면은 모바일 크기에도 맞지만 서버와 브로커가 로컬에 묶여 있어
다른 기기에서 접속할 수 없다. 실행기에서 Ctrl+C를 누르면 해당 시험 서버와
브로커가 종료된다. Unreal은 직접 닫는다. Unreal이 움직이지 않으면 인증된
WebSocket 연결과 `AGV_01`, `AGV_02` 수신 로그를 확인한다.
실행기는 Unreal 연결 성공이나 맵 일치를 자동 확인하지 않으므로 `--map`에
Unreal이 실제로 읽는 맵과 같은 파일을 지정해야 한다. C드라이브 공간이
부족하다면 `--ddc-path`로 존재하는 D드라이브의 Unreal 캐시 폴더를 지정한다.

이 시연은 Unreal 맵의 미터 좌표를 밀리미터로 변환하고 모든 간선을 양방향
단일 차선, 모든 노드를 대기 지점으로 가정한다. 공유 구역은 추정하지 않는다.
이 가정은 실물 맵의 방향성·안전 구역이나 좌표 정합을 확정하지 않는다.
실행기는 직접 만든 임시 루프백 브로커만 사용한다. 로컬 주소나
`sim-` ID만으로 브로커 격리가 보장되지는 않으므로 앱 모듈을 공유 브로커에
직접 연결하지 않는다. 실행마다 새 임시 서버·로봇 SQLite DB를 사용하며,
서버 재시작 후 주문·예약을 복원하는 운영 기능은 제공하지 않는다. 가상
명령의 PUBACK은 로봇 수행 완료가 아니며, 가상 로봇의 수신·정지 이벤트를
별도로 확인한다. 각 로봇에 사전 매핑된 작업은 최대 16개지만, 현재 상품별
고정 슬롯 사용 제한 때문에 한 실행에서 가능한 상품 주문은 빨강·파랑 각 한 번이다.
현재 구간이 다음 순번으로 진행된 뒤 이전 순번의 QoS 1 명령이 중복 수신되면,
가상 로봇은 이를 재실행하지 않고 시연을 결과 불명으로 중단한다. 늦은 중복을
확인만 하고 계속 진행하는 복구 기능은 아직 제공하지 않는다.
가상 상태 토픽의 `WAITING`과 `ERROR`는 이 시연에서만 쓰는 표현이며, 실제
로봇 상태 규약이나 물리적 정지의 증거는 아니다.

상품 주문은 픽업과 목적지에서 멈추며 Unreal 액터가 요청 위치의 25cm 이내에
있는지 확인한 뒤 집기·적재를 적용한다. 각 동작의 실제 적용 성공 응답을 받은
경우에만 다음 단계로 진행하며, 둘 다 성공해야 '가상 적재 확인됨'을 표시한다.
픽업 후 배송, 이전 상품 적재 후 다음 상품 픽업 시에는 기존 점유와 이력을
보존하면서 종점 점유를 새 작업에 원자적으로 인계한다. 응답 지연·연결 단절
시 결과 불명으로 중단하고 액션을 자동 재시도하지 않는다. 이 예약 모드에서는
직접 노드 지정 기능을 제공하지 않는다.

재고 관리, 실제 고객 주문, 실물 로봇 MQTT 명령과 실물 운행은 이 시연 범위가
아니다. 이 시험 맵의 점유 규칙은 가정에 기반하므로 실물 충돌 방지나 안전 운행
검증을 뜻하지 않는다. 주문이 실패했거나 결과 불명이면 서버가 추가 주문을 막는다.
시연 서버와 Unreal 에디터를 모두 종료하고 실행기를 다시 시작한 후 맵을 열고
플레이한다. 앱에는 `AGV_ORDER_FLEET`을 활성화하지 않은 기존 단일 로봇 예약
모드도 남아 있지만, 위 실행기는 두 로봇 모드를 기본으로 설정한다.

## 트윈 로컬 관측 연결 제안

현식님 트윈의 WebSocket 클라이언트와 연결하기 위한 기본 비활성화된
실험 기능이다. [연결 제안과 제한](docs/twin_websocket_proposal.md)을 먼저
읽는다. 실물과 공유하지 않는 MQTT 브로커 및 가상 로봇을 준비하고, 서버를
단일 프로세스로 실행할 때만 아래 환경 변수를 설정한다.

```powershell
$env:AGV_TWIN_WEBSOCKET = '1'
$env:AGV_TWIN_ROBOT_ID = 'sim-r1'
# Unreal 테스트를 할 때 이 셸에서 새 토큰을 생성하고, 같은 환경을 상속해
# 서버와 Unreal 테스트 사본을 각각 실행한다. 토큰 값을 출력하거나 저장하지 않는다.
$env:AGV_TWIN_UNREAL_TOKEN = .\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
Start-Process -FilePath 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' -ArgumentList '"D:\AGV-Twin-Validation\agvSimulation.uproject"', '-NoSplash'
.\.venv\Scripts\python -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

`/ws/digital-twin`은 연결 후 선택한 ID의 새 실시간 MQTT 상태를 관측용
`robot.state`로 보낸다. 루프백 외 접속은 거부한다. Origin이 없는 로컬
클라이언트는 기존처럼 토큰 없이 접속 가능하다. Origin이 있으면 정확히 하나의
`Origin: http://127.0.0.1`과 하나의 `X-AGV-Twin-Token`이 필요하며, 헤더 값은
서버의 `AGV_TWIN_UNREAL_TOKEN`과 일치해야 한다. 토큰은 영문 대소문자, 숫자,
`_`, `-`로 구성된 32~128자다. 다른 Origin, 중복 헤더, 누락·불일치 토큰은
거부한다. 기본값에는 토큰이 없어서 Origin이 있는 접속은 허용되지 않는다.

위 예시의 환경변수를 서버와 Unreal 테스트 사본 양쪽에 전달해야 한다. 현재
토큰 헤더를 보내도록 변경한 것은 `D:\AGV-Twin-Validation`의 Unreal 테스트
사본뿐이며, 현식님의 원본 프로젝트나 GitHub 브랜치에 반영된 것은 아니다.
테스트 사본은 정확히 `ws://127.0.0.1:8000/ws/digital-twin`으로 접속할 때만
토큰을 전송한다.
토큰을 소스·URL·로그에 기록하거나 공개하지 않는다. 이 기능은 Origin이 없는
로컬 프로세스 인증을 제공하지 않고, 같은 계정의 프로세스에서 환경변수를
읽을 가능성도 있으므로 외부 네트워크나 실물 브로커에 노출하지 않는다.

### 격리된 가상 로봇 두 대 관측 시험

실물과 공유하지 않는 MQTT 브로커에서 두 가상 발행기를 사용할 때,
서버를 실행하기 전 MQTT의 가상 ID와 Unreal 액터 ID를 명시적으로 매핑한다.

```powershell
$env:AGV_TWIN_ROBOT_MAP = '{"sim-r1":"AGV_01","sim-r2":"AGV_02"}'
```

이 설정에서는 `sim-r1`, `sim-r2`의 새 상태를 각각 `AGV_01`, `AGV_02`로
하나의 WebSocket에서 전달하며 `AGV_TWIN_ROBOT_ID`는 무시한다. Unreal 액터의
실제 `AGVId`도 고유하게 일치해야 한다. ID에 `sim-`을 붙이는 것만으로 브로커
분리나 실물 안전을 보장할 수 없다.

정확히 서로 다른 두 항목만 허용하고 MQTT ID는 `sim-` 뒤 ASCII 영문·숫자·
`_`·`-` 1~60자, Unreal ID는 같은 문자 1~64자여야 한다. 빈 값이나 잘못된
JSON, 중복, 와일드카드, 비시뮬레이션 ID, 다른 항목 수는 연결을 거부한다.
매핑이 없는 기존 단일 모드로 돌아가려면 빈 값을 넣지 말고 환경변수를 제거한다.

```powershell
Remove-Item Env:AGV_TWIN_ROBOT_MAP -ErrorAction SilentlyContinue
```

두 로봇 관측이 동시에 연결된다는 사실은 충돌 방지 또는 실물 운행의 검증을
의미하지 않는다. 위치 전송은 아래의 별도 가상 pose 설정과 유효성 제한을 따른다.

#### Unreal 화면 시험 재현

2026-11-16에 두 가상 로봇이 서로 다른 직선 구간에서 부드럽게 왕복하는
것을 Unreal Editor 화면에서 확인했다. 현식님이 구현한 좌표 보간을 사용한다.
집에서 시험하려면 서버 의존성을 설치하고 별도의 aMQTT 의존성을 준비한다.

```powershell
.\.venv\Scripts\python -m pip install --target .venv/broker-deps 'amqtt==0.11.3'
```

Unreal 프로젝트는 별도로 준비한다. 기존 Unreal 에디터를 닫고, 별도 테스트
사본에 [로컬 인증 패치](docs/unreal_local_auth.patch)를 `git apply --check
--unidiff-zero`로 확인한 뒤 적용·빌드한다. 패치는 `feature/digital-twin`의
`44cc000` 기준이며, 다른 커밋은 적용 가능 여부를 먼저 확인한다. 액터의
`AGVId`는 각각 `AGV_01`, `AGV_02`여야 한다. Unreal 엔진과 프로젝트는
이 서버 브랜치에 포함되지 않는다.

저장소 루트에서 실제 PC에 맞게 아래 두 경로를 바꾸어 실행한다.

```powershell
$editor = 'C:\경로\UnrealEditor.exe'
$project = 'C:\경로\agvSimulation.uproject'
.\.venv\Scripts\python -m server.tools.twin_multi_test --editor $editor --project $project
```

스크립트는 루프백 전용 임시 브로커와 서버, 가상 발행기를 시작하고 Unreal을
연다. `Content/AGV/NewMap`을 열고 플레이한다. 로봇들이 움직이지 않으면
Unreal 로그에서 WebSocket 연결과 `AGV_01`, `AGV_02` 상태 수신을 확인한다.
스크립트가 Unreal 연결 자체를 보증하지는 않는다. 콘솔에서 Ctrl+C를 누르면
시험 서버와 브로커는 종료되며 Unreal은 직접 닫는다. 8000번 포트가 이미
사용 중이면 시험을 시작하지 않는다. 두 로봇의 직선 시험은 충돌 방지나
실물 주행 시험이 아니다.

ID만으로 가상 장비와 네트워크의 격리를 보장할 수 없다. 오래된 상태는 다시
보내지 않고 경로 주문은 전송하지 않는다. 기본 모드는 노드 상태만 보내며,
현재 트윈은 노드 메시지를 받으면 표시 위치를 바꿀 수 있으므로 같은 맵의
노드 ID를 쓰는지 확인해야 한다. 실물 운행이나 안전 판단에 사용하지 않는다.

### 격리된 가상 pose 시험

실물과 분리된 브로커 및 가상 발행기를 사용할 때만 서버 환경변수
`AGV_TWIN_POSE_MAP_ID=twin-validation-v1`을 선택적으로 설정할 수 있다.
미설정 또는 빈 값이면 기존 노드 상태만 보내고, 이 값이 아닌 비어 있지 않은
설정값은 pose 시험을 활성화하지 않고 상태 전송을 거부한다. 시험 발행기가
보내는 상태의 `map_id`도 정확히 `twin-validation-v1`이어야 한다.

```json
{"map_id":"twin-validation-v1", "last_node":"J01", "status":"MOVING", "battery":0.8,
 "pose":{"x":1500,"y":2000,"th":90}}
```

입력 `pose.x`, `pose.y`는 mm, `pose.th`는 도이며 서버는 이를 WebSocket의
최상위 `data.x=1.5`, `data.y=2.0`(m), `data.yaw=90`(도)으로 변환한다.
셋 모두 유한한 숫자여야 하며 x/y는 각각 ±1,000,000 mm, 각도는 ±3,600도
안에 있어야 한다. 불리언·누락·범위 초과·맵 불일치는 상태 메시지 전체를
보내지 않는다. 이는 노드만 전송했을 때 Unreal이 순간 이동하는 것을 피한다.
이 수치 한계나 맵 ID 일치가 실제 맵 경계 또는 물리적 안전을 보장하지 않는다.

이 실험은 가상 발행기와 Unreal 맵이 원점과 축, 각도 방향을 공유한다고
가정한다. 실물 좌표계와의 정합은 팀 합의 및 검증이 필요하다. Unreal의 첫
pose는 초기 위치로 즉시 배치되고 그 뒤의 좌표가 보간된다. 이 기능은 로봇의
명령·경로 전송이나 실물 주행 검증이 아니다.

### 격리 브로커 연결 시험

아래 선택 시험은 별도의 테스트 전용 aMQTT 브로커와 FastAPI를 임시 루프백
포트에 띄우고, 가상 로봇 발행과 실제 WebSocket 수신을 확인한 뒤 종료한다.
브로커 의존성은 서버와 별도 경로에 설치한다.

```powershell
.\.venv\Scripts\python -m pip install --target .venv\broker-deps amqtt==0.11.3
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python -m unittest server.tests.test_twin_broker_integration -v
```

이 시험의 WebSocket 클라이언트는 Unreal을 대체하는 테스트 클라이언트다.
Unreal Editor 화면의 수신·시각화나 실물 로봇의 동작을 검증하지 않는다.
