# 관제 서버

FastAPI와 MQTT 기반 관제 서버의 초기 구현이다. MQTT 상태 수신과 별도로
경로 계산, 구간 예약 자료구조, 기록·재생 구성요소를 단계적으로 개발한다.

## 준비와 실행

저장소 루트에서 Python 가상환경을 만들고 의존성을 설치한다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r server/requirements.txt
```

저장소 루트의 `.env`에 `MQTT_HOST`, `MQTT_PORT`를 설정하고, 해당 MQTT
브로커를 실행한 뒤 서버를 시작한다. 브로커 접속 정보와 비밀번호는
저장소에 커밋하지 않는다. 현재 상태 수신기는 사용자 인증·TLS 설정을
아직 지원하지 않으므로 인증이 필요한 팀 브로커에는 그대로 연결할 수 없다.

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
