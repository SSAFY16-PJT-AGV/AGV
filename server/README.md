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
- `mqtt_log.py`: 기록 및 격리 재생 구성요소. [기록·재생 안내](docs/mqtt_logging.md)를
  먼저 읽는다. 재생은 설계의 원본 토픽 대신 `replay/` 접두사를 사용한다.
- `mqtt_capture.py`: 별도로 실행하는 MQTT 기록 수집기.
  [수집기 안내](docs/mqtt_capture.md)를 먼저 읽는다.

현재 구성요소는 작업 배정, DB, 경로 승인과 실제 로봇 운행으로 이어지는
완성된 서버가 아니다. 재시작 복구, 통신 장애 대응, 충돌 방지와 물리적
정지는 팀 간 프로토콜 합의와 실물 검증이 필요하다.

## 테스트

```powershell
.\.venv\Scripts\python -m unittest discover -s server/tests -v
.\.venv\Scripts\python -m unittest discover -s server/demo -v
```

단위 테스트는 실제 로봇과 모터를 사용하지 않는다.
