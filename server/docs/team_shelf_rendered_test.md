# 웹·MQTT·Unreal 팀 선반 격리 시험

`team_shelf_rendered_test.py`는 독립 Unreal 6선반 사본, 새 가상 슬롯 DB와
루프백 MQTT 브로커·FastAPI 서버를 실행한다. 웹과 동일한 HTTP 주문 API로
우유를 한 번 주문하고 AGV 배정, MQTT 명령, Unreal의 위치·슬롯별 집기와
적재 수신, 서버 슬롯 장부의 완료와 재고 이동을 확인한다. `--two-orders`를
추가하면 우유와 휴지를 동시에 주문하고 두 가상 AGV에 각각 배정되는지와
두 품목을 확인한다. HTTP 결과가 불명이라도 POST를 다시 보내지 않는다.

Unreal 사본은 [팀 6선반 가상 실험](unreal_team_shelf_simulation.md)을
따라 준비한다. 8000 포트가 비어 있어야 하며 Unreal 5.7, 서버 가상환경과
별도의 aMQTT 의존성이 필요하다. 다음의 경로는 이 PC의 예시다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.team_shelf_rendered_test `
  --unreal-editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --uproject '.venv\v3-team-shelf-full\agvSimulation.uproject' `
  --ddc '.venv\v3-warm-render-ddc' --two-orders
```

서버·로봇·브로커·게임은 시험 전용이고 출력된 증거 디렉터리에 `result.json`,
서버·게임·브로커 로그가 남는다. 테스트 종료 시 생성한 프로세스만 종료한다.
입력된 월드를 영속 레지스트리에 기록하므로 매번 새 시험 세계를 사용한다.
`rendered: true`는 렌더링 프로세스 실행을 뜻하며 사람이 화면을 관찰하거나
실물 로봇의 정지·충돌 방지를 검증했다는 뜻이 아니다. 레벨의 선반 좌표와
품목 메시 및 재고는 가상 대응이다. 셰이더 캐시가 없으면 Unreal 초기화가
오래 걸릴 수 있으므로 필요한 경우 `--start-timeout`을 늘린다.
