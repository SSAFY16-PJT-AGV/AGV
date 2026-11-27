# 팀 6개 선반 웹 주문·Unreal 가상 시연

명시적으로 선택하는 로컬 가상 모드다. 팀 맵 v3의 가상 대기점과 예제 좌표를
사용하고, 두 가상 AGV에만 MQTT 명령을 보낸다. 실물 로봇·카메라·실제 재고,
물리적 위치나 안전은 검증하지 않는다.

## 준비와 실행

Unreal Engine 5.7과 네 품목용으로 패치·빌드한 별도 Unreal 프로젝트가 필요하다.
프로젝트 준비와 빌드는 `unreal_team_shelf_simulation.md`를 확인한다. 이 PC에서
시험한 독립 프로젝트는 `D:\관통\AGV-analysis\.venv\v3-team-shelf-full\agvSimulation.uproject`,
빌드 캐시는 `D:\관통\AGV-analysis\.venv\v3-warm-render-ddc`다. 이 경로의
프로젝트와 캐시는 Git에 포함되지 않는다.

저장소 루트에서 다음처럼 실행하고, Unreal이 열리면 `Content → AGV → NewMap`을
열어 플레이한다. 서로 다른 환경에서는 설치·프로젝트 경로를 해당 PC에 맞춘다.
8000 포트가 비어 있어야 하며 `.venv\broker-deps`에는 별도로 설치한 aMQTT가
필요하다. 실행기는 임시 loopback 브로커·서버만 시작하고 인증 토큰을 Unreal
프로세스에 전달한다. 기존 장비나 공용 브로커를 사용하지 않는다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.order_demo `
  --map server\examples\mat_ssafy_v3_unreal_sim.json `
  --field-config server\examples\field_unreal_v3_sim.json `
  --team-shelves `
  --editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --project 'D:\관통\AGV-analysis\.venv\v3-team-shelf-full\agvSimulation.uproject' `
  --ddc-path 'D:\관통\AGV-analysis\.venv\v3-warm-render-ddc'
```

웹 화면은 <http://127.0.0.1:8000/order-demo/>다. Ctrl+C로 실행기 소유 서버와
브로커를 종료하며 Unreal 에디터는 직접 닫는다. 네 품목은 가상 상품이며
색상·모델은 실제 상품과 일치하지 않을 수 있다.

## 동작과 제한

- 24칸 관찰에서 N01의 상품과 N02~N05의 빈 칸을 함께 선점한다. 순서가 맞는
  기존 가상 로봇 배정·경로 계산·SQLite 구간 예약·MQTT를 거쳐 Unreal에
  슬롯 ID가 포함된 별도 집기·적재 요청을 보낸다. UE 응답과 기존 작업 ID,
  로봇, 세계, 슬롯을 대조한다. N06도 관찰하지만 현재 목적지로 배정하지 않는다.
- 두 주문이 같은 N01 픽업 통로를 사용하면 첫 로봇이 집은 뒤 D01/N01과 관련
  구간·공유 구역 점유를 해제할 때까지 다음 주문은 대기한다. 관측 실패·단절·
  불명 결과는 빈 구역으로 추정하지 않는다. 이것은 가상 예약에 대한 검증이다.
- 이 모드의 주문·재고·가상 세계는 **프로세스 종료 후 복구되지 않는다**.
  기존 영속 모드의 `--state-dir` 및 `--synthetic`과 함께 쓸 수 없다. 서버가
  중단되면 자동 재시작하거나 진행 중인 명령을 자동 재발행하지 않는다.
- 사용한 Unreal 세계 ID는 기본 고정 경로
  `.venv/team-shelf-worlds.sqlite3`에 기록되어 같은 PC·같은 저장소의 서버가
  재시작돼도 같은 세계를 새 시연으로 수락하지 않는다. 시험 전용 환경 변수
  `AGV_ORDER_TEAM_WORLD_REGISTRY`로 격리 경로를 지정할 수 있지만 일반 실행에서는
  기본 경로를 계속 써야 한다. 이 파일을 삭제·교체하거나 다른 PC/다른 저장소를
  사용하면 재사용 차단이 이어지지 않는다. 새 시연은 Unreal 게임을 종료하고
  새 세계를 연 후 시작한다. 이 장부는 주문·재고를 복구하는 데이터베이스가 아니다.
- 집기·적재 결과 유실, 슬롯 저장 실패, 연결 해제 시 관련 작업을 불명으로
  보존한다. 서로 다른 DB와 MQTT를 하나의 트랜잭션으로 간주하지 않는다.
  화면의 `확인 불가`와 `예약`은 빈 칸이 아니다.

## 개발 환경 직접 실행

실행기가 설정하는 핵심 서버 환경은 `PYTHON_DOTENV_DISABLED=1`,
`AGV_ORDER_DEMO_ENABLED=1`, `AGV_ORDER_COORDINATED=1`, `AGV_ORDER_FLEET=1`,
`AGV_ORDER_SHELF_SYNC=1`, `AGV_ORDER_TEAM_SHELVES=1`, `MQTT_HOST=127.0.0.1`,
격리 브로커의 `MQTT_PORT`, v3 `AGV_ORDER_DEMO_MAP` 및 `AGV_ORDER_FIELD_CONFIG`다.
WebSocket 인증과 로봇 대응의 `AGV_TWIN_*` 설정도 필요하다. 직접 실행할 때도
`AGV_ORDER_PERSIST_DIR`을 설정하지 않고 사용한 Unreal 세계는 재사용하지 않는다.
