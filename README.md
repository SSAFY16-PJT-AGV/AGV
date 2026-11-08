# AGV Digital Twin

Unreal Engine 5 기반 **AGV 디지털 트윈 프로젝트**입니다. 창고 맵을 시각화하고, 서버에서 전달하는 AGV 위치 정보를 바탕으로 가상 AGV의 움직임을 동기화합니다.

## 시스템 구조

```text
AGV (연동 예정) → MQTT → FastAPI 서버 → WebSocket → Unreal Engine
                              ↑
                     현재는 Python 테스트 서버 사용
```

## 구현 기능

- **창고 맵 로드:** `warehouse_map.json`의 노드·경로 정보를 읽어 맵과 주행 경로 생성
- **AGV 이동 모드:** Manual / Simulation / External 지원
- **실시간 위치 반영:** `robot.state`의 `x`, `y`, `yaw`를 받아 가상 AGV 이동 및 회전
- **서버 메시지 처리:** `robot.state`, `robot.route` 수신 및 상태 정보 갱신
- **연결 안정성:** 위치 데이터 타임아웃 감지, WebSocket 자동 재연결
- **주행 재개 테스트:** 테스트 서버의 진행 상태 저장을 통해 재시작 후 남은 구간 진행
- **창고 레벨 디자인:** Static Mesh를 활용한 창고 환경 구성

## 실행 및 테스트

1. Unreal 프로젝트(`.uproject`)를 열고 AGV 레벨을 실행합니다.
2. Python FastAPI 테스트 서버를 실행합니다. (기본 주소: `ws://127.0.0.1:8000/ws/digital-twin`)
3. `AGVRobot`의 이동 모드를 **External**로 설정합니다.
4. 서버에서 `robot.state` 좌표를 전송하면 가상 AGV가 해당 위치로 이동합니다.

> 테스트 서버는 미리 정의한 `test_steps`를 전송합니다. 처음부터 다시 테스트하려면 서버 종료 후 `agv_test_progress.json`을 삭제하세요.

## 개발 환경

- Unreal Engine 5 / C++
- Python, FastAPI, WebSocket (테스트 서버)
- JSON 기반 창고 맵 데이터

## 진행 상태

현재 **테스트 서버 기반 위치 동기화, 타임아웃 감지, 자동 재연결 및 주행 재개**를 검증했습니다. 실제 Waveshare JETANK의 위치 데이터를 연동하는 작업은 추후 진행할 예정입니다.
