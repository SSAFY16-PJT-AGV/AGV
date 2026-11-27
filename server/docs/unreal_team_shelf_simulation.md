# 팀 6개 선반 Unreal 가상 실험

이 코드는 별도 실험 프로젝트에서 팀 v3 맵의 D01~D06에 N01~N06을 가상으로
대응한다. 선반당 네 칸으로 전체 24칸이며 N01의 우유·휴지·달걀·주스가
N02~N05로 옮겨진다. 좌표와 재고는 시연용이고 실제 매장이나 센서 상태가
아니다. 네 품목은 기존 빨강·파랑 임시 메시를 재사용하므로 외형만으로 서로
구분되지 않는다. N06은 빈 선반이다.

`team.shelf.snapshot.request.v1`에 요청 ID를 넣으면 동일 ID의
`team.shelf.snapshot.v1`으로 월드 ID, 두 로봇 포즈와 1~24번 슬롯 상태를
반환한다. `team.shelf.action.v1`과 `team.shelf.action.result.v1`은 기존
슬롯 무시 `robot.action`과 별개다. 액션에는 월드·요청·액션·로봇·상품·
선반·슬롯·좌표가 필요하며 잘못된 상품, 월드 또는 위치의 작업은 거부한다.
`server/team_shelf_twin_protocol.py`는 동일 액션 응답을 검증하고 불명
결과를 보류한다. 이 프로토콜은 실물 로봇의 작업이나 정지를 증명하지 않는다.

## 독립 사본 준비

1. [v3 사본 준비](field_v3_unreal.md)의 팀 소스 커밋과 패치 1~9를 순서대로
   적용하고 v3 씬을 만든다. 기존 프로젝트와 Content를 공유하지 않는다.
2. 같은 독립 사본의 프로젝트 루트에서 차례로 다음 두 패치를 `git apply
   --check` 후 적용하고 UE 5.7 Editor 타깃을 다시 빌드한다.
   `server/docs/unreal_team_slot_milk_pilot.patch`,
   `server/docs/unreal_team_shelf_four_product.patch`. 두 번째 패치는 첫 번째
   패치에 이어 적용한다. 원본 파일의 기존 인코딩을 유지해야 한다.
3. Python 에디터 스크립트 `server/tools/create_team_shelf_ue_scene.py`를
   실행해 씬을 변환한다. `AGV_TEAM_FULL_PROJECT`는 독립 프로젝트 폴더의
   절대 경로, `AGV_TEAM_FULL_CONFIRM`은 `ISOLATED`로 설정해야 한다. 해당
   프로젝트 Content의 맵과 레벨 파일을 변경하므로 반드시 별도 사본에만
   실행한다. 한 번만 적용할 수 있다.
4. `server/tools/verify_team_shelf_ue_scene.py`를 같은 사본에서 실행해 저장된
   선반 6개와 소스 상품 4개, 노드 대응을 검증한다. 선택적으로
   `server/tools/set_team_shelf_demo_camera.py`로 카메라를 조정할 수 있다.

패치 스택은 `python -m unittest server.tests.test_team_shelf_unreal_patch_stack`
으로 팀 커밋 원본에서 재적용할 수 있다. 팀 커밋이 로컬에 없다면 해당
테스트는 생략된다.

## 독립 Unreal 시험

8000 포트가 비어 있을 때 서버 저장소에서 다음 명령을 실행한다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.team_shelf_unreal_integration_test `
  --unreal-editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --uproject 'D:\경로\독립사본\agvSimulation.uproject' `
  --ddc 'D:\경로\독립사본\DerivedDataCache'
```

테스트는 자체 루프백 WebSocket, 새 토큰과 새 SQLite DB를 사용하며 24칸
스냅샷, 네 상품의 명시적 슬롯 집기·적재, 다른 상품 집기의 거부를 확인한다.
출력된 폴더에 `result.json`과 Unreal 로그를 남기고 본인이 실행한 게임만
종료한다. 이 시험은 로봇 위치를 직접 주입하므로 웹 주문, MQTT 운행 및
충돌 방지의 종단 시험은 아니다. WebSocket 송신 전에 실제 가상 배정과
정지 확인을 결합한 통합 시험은 별도로 필요하다.
