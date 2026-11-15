# AGV Digital Twin

Unreal Engine 5 기반 **AGV(Automated Guided Vehicle) 디지털 트윈 프로젝트**입니다.

창고 환경과 AGV의 이동 경로를 3D로 시각화하고, 중앙 서버에서 전달하는 위치 및 작업 이벤트를 바탕으로 가상 AGV의 움직임과 상품 집기·적재 작업을 동기화합니다.

현재는 Python FastAPI 테스트 서버를 이용해 AGV 2대의 이동과 물류 작업을 검증하고 있으며, 향후 실제 Waveshare JETANK AGV와 연동할 예정입니다.

## 시스템 아키텍처

```text
[실제 AGV - Waveshare JETANK]
              │
              │ MQTT (연동 예정)
              ▼
       [Python 중앙 서버]
              │
              │ WebSocket
              ▼
       [Unreal Engine 5]
              │
              ├── 창고 맵 및 주행 경로 시각화
              ├── AGV 실시간 위치 및 회전 반영
              ├── AGV별 상태 관리
              ├── 상품 집기 및 적재 시각화
              └── 선반 슬롯 상태 관리
```

현재 개발 단계에서는 실제 AGV 대신 **Python FastAPI 테스트 서버**가 가상의 AGV 위치와 작업 이벤트를 생성하여 Unreal Engine으로 전달합니다.

실제 AGV 연동 시에는 MQTT를 통해 수집한 AGV 상태 및 작업 결과를 중앙 서버에서 처리하고 Unreal Engine에 전달하는 구조를 목표로 합니다.

## 주요 구현 기능

### 1. 창고 맵 로드 및 시각화

- `warehouse_map.json` 기반 창고 맵 구성
- 노드(Node)와 간선(Edge) 데이터 파싱
- Junction, Normal, Dock 노드 구분
- 노드 간 주행 경로 생성 및 시각화
- 경유점을 이용한 곡선 경로 표현
- Static Mesh 기반 3D 창고 환경 구성

### 2. AGV 이동 및 상태 동기화

- Manual / Simulation / External 이동 모드 지원
- WebSocket 기반 AGV 위치 데이터 수신
- `robot.state` 메시지를 통한 위치 및 회전 동기화
- `x`, `y`, `yaw` 기반 가상 AGV 이동
- AGV별 ID를 이용한 개별 상태 관리
- 이동 상태 및 배터리 정보 반영
- 위치 데이터 타임아웃 감지
- WebSocket 자동 재연결

### 3. 다중 AGV 시뮬레이션

- AGV_01, AGV_02 동시 이동 지원
- AGV별 독립적인 이동 경로 설정
- 각 AGV의 위치 및 작업 상태 개별 관리
- 하나의 WebSocket 연결을 통한 다중 AGV 상태 전송
- 동일한 창고 맵에서 여러 AGV의 이동 시각화

### 4. 노드 기반 작업 이벤트 처리

AGV가 지정된 작업 노드에 도착하면 집기 또는 적재 이벤트를 발생시키는 테스트 기능을 구현했습니다.

- `AGV_ROUTES`를 통한 이동 경로 정의
- `AGV_TASKS`를 통한 작업 위치 및 동작 정의
- 경로상 노드 도착 여부 판단
- `robot.action` 메시지를 이용한 작업 이벤트 전달
- AGV별 작업 순서 관리
- 중복 작업 이벤트 전송 방지

집기·적재 이벤트는 고정된 작업 시각이 아니라 경로상의 작업 노드 도착을 기준으로 생성됩니다.

현재는 테스트 서버가 작업 성공 이벤트를 생성하며, 실제 AGV의 작업 성공 여부를 수신하는 기능은 향후 연동할 예정입니다.

### 5. 상품 집기 및 선반 적재

- Red / Blue 상품 유형 구분
- AGV별 상품 집기 동작
- 상품 유형에 맞는 선반 검색
- AGV의 상품 운반 상태 시각화
- 지정된 선반 슬롯에 상품 적재
- 선반 슬롯 점유 여부 확인
- 상품 유형 불일치 및 슬롯 점유 시 적재 제한

현재 테스트 서버는 자체적으로 빈 슬롯을 검색해 적재 이벤트에 슬롯 번호를 포함합니다.

Unreal의 실제 적재 결과를 중앙 서버에 회신하여 슬롯 점유 상태를 동기화하는 기능은 향후 구현할 예정입니다.

### 6. 통신 및 연결 관리

- FastAPI 기반 WebSocket 테스트 서버
- JSON 메시지 송수신
- `robot.state` 메시지 처리
- `robot.route` 메시지 수신 및 처리
- `robot.action` 메시지 처리
- 연결 해제 및 재연결 처리
- 테스트 서버의 이동 진행 상태 저장
- 서버 재시작 후 이동 진행 상태 복원 기능

## AGV 작업 시나리오

현재 테스트 서버에서는 두 AGV가 각각 다른 상품을 운반하는 시나리오를 사용합니다.

### AGV_01 — Red 상품 운반

```text
D01 (출발 및 Red 상품 집기)
    ↓
N01 → J02 → J03 → N04
    ↓
D04 (SHELF_RED에 적재)
```

### AGV_02 — Blue 상품 운반

```text
D03 (출발)
    ↓
N03 → J07 → J06 → N06
    ↓
D06 (Blue 상품 집기)
    ↓
N06 → J05 → J04 → N05
    ↓
D05 (SHELF_BLUE에 적재)
```

이동 경로는 `warehouse_map.json`에 정의된 간선을 사용하며, 테스트 서버의 `AGV_ROUTES`에서 설정합니다.

현재 경로는 미리 지정된 경로를 따르며, 자동 최단 경로 탐색은 아직 적용되지 않았습니다.

## WebSocket 메시지 구조

### AGV 상태 메시지

서버에서 Unreal로 전송하는 AGV 상태 메시지 예시입니다.

```json
{
  "type": "robot.state",
  "schema_version": "1.0",
  "data": {
    "agv_id": "AGV_02",
    "mode": "external",
    "status": "moving",
    "current_node": "N06",
    "battery": 90,
    "x": 12.0,
    "y": 7.5,
    "yaw": 90.0
  }
}
```

### 상품 집기 이벤트

```json
{
  "type": "robot.action",
  "schema_version": "1.0",
  "data": {
    "agv_id": "AGV_02",
    "action": "grab",
    "result": "success",
    "product_type": "Blue"
  }
}
```

### 상품 적재 이벤트

```json
{
  "type": "robot.action",
  "schema_version": "1.0",
  "data": {
    "agv_id": "AGV_02",
    "action": "place",
    "result": "success",
    "shelf_id": "SHELF_BLUE",
    "slot_index": 0
  }
}
```

현재 `robot.action`의 성공 결과는 테스트 서버에서 생성한 가상 작업 이벤트이며, 실제 AGV의 물리적 작업 성공을 검증한 결과는 아닙니다.

## 실행 및 테스트

### 1. Unreal Engine 실행

1. Unreal 프로젝트의 `.uproject` 파일을 엽니다.
2. AGV 및 창고 맵이 구성된 레벨을 실행합니다.
3. AGV_01과 AGV_02의 이동 모드를 `External`로 설정합니다.
4. 선반 Actor의 `Shelf Id`를 각각 `SHELF_RED`, `SHELF_BLUE`로 설정합니다.
5. 상품과 선반의 위치 및 상품 유형을 확인합니다.

### 2. Python 테스트 서버 실행

필요한 Python 패키지를 설치합니다.

```bash
pip install fastapi uvicorn
```

테스트 서버 파일이 있는 디렉터리에서 실행합니다.

```bash
python test_ws_server.py
```

기본 WebSocket 주소:

```text
ws://127.0.0.1:8000/ws/digital-twin
```

### 3. 동작 확인

- AGV_01과 AGV_02가 지정된 경로를 따라 이동하는지 확인
- 각 AGV가 작업 노드에서 상품을 집는지 확인
- 상품 유형에 맞는 선반으로 이동하는지 확인
- 지정된 슬롯에 상품이 적재되는지 확인
- 서버와 Unreal 간 상태 및 작업 이벤트가 정상적으로 전달되는지 확인

테스트 서버의 `RESET_PROGRESS_ON_START`가 `True`이면 서버 재시작 시 이동 진행 상태를 초기화합니다.

## 개발 환경

| 구분 | 기술 |
|---|---|
| 게임 엔진 | Unreal Engine 5 |
| Unreal 개발 언어 | C++ / Blueprint |
| 중앙 서버 테스트 | Python / FastAPI |
| 실시간 통신 | WebSocket |
| 창고 맵 데이터 | JSON |
| AGV 하드웨어 | Waveshare JETANK (연동 예정) |
| AGV–서버 통신 | MQTT (연동 예정) |
| 형상 관리 | Git / GitHub |

## 개발 진행 상태

### 구현 및 테스트 완료

- [x] JSON 기반 창고 맵 로드
- [x] 창고 노드 및 주행 경로 시각화
- [x] AGV 이동 모드 구현
- [x] WebSocket 기반 위치 동기화
- [x] AGV 2대 동시 이동
- [x] AGV별 독립적인 이동 경로 관리
- [x] 노드 도착 기반 작업 이벤트 생성
- [x] Red / Blue 상품 집기 및 적재 시뮬레이션
- [x] 상품 유형별 선반 구분
- [x] 선반 슬롯 점유 검사
- [x] WebSocket 자동 재연결 및 타임아웃 감지
- [x] 테스트 서버 이동 진행 상태 저장

### 향후 개발 예정

- [ ] Unreal에서 선반의 빈 슬롯 자동 선택
- [ ] Unreal → 중앙 서버 적재 결과 회신
- [ ] 중앙 서버의 선반 적재 상태 동기화
- [ ] 작업 이벤트별 성공·실패 응답 및 확인 처리
- [ ] 실제 Waveshare JETANK AGV 연동
- [ ] MQTT 기반 AGV 상태 및 작업 결과 수신
- [ ] 실제 AGV의 작업 결과 기반 디지털 트윈 동기화
- [ ] 자동 경로 탐색 및 경로 최적화
- [ ] 다중 AGV 충돌 방지 및 작업 관리

## 프로젝트 목표

본 프로젝트의 최종 목표는 실제 AGV의 이동 및 작업 정보를 중앙 서버에서 수집하고, Unreal Engine에서 창고 환경과 AGV의 상태를 실시간으로 시각화하는 **AGV 디지털 트윈 시스템**을 구축하는 것입니다.

현재는 Python 테스트 서버를 이용해 이동·집기·적재 시나리오를 검증하고 있으며, 이를 실제 AGV 기반 시스템으로 확장할 계획입니다.