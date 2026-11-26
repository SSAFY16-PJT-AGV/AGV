# v3 맵 Unreal 격리 시연

이 설정은 팀 v3 맵과 Unreal 팀 프로젝트를 연결하는 **가상 시연**이다.
실측한 상품·선반 위치나 물리 주행 허가가 아니다. 원본 맵의 대기점만
사용하며, 로봇·상품·선반의 대응은 다음과 같이 명시한다.

| 대상 | 시연 위치 |
| --- | --- |
| AGV_01 / AGV_02 시작 | D07 / D08 |
| 빨강 상품 / 빨강 선반 | D01 / D04 |
| 파랑 상품 / 파랑 선반 | D06 / D03 |

`server/examples/field_unreal_v3.json`은 원본 `mat_ssafy_v3.json`을 사용한다.
Unreal의 맵 파일은 원본에서 만든 `mat_ssafy_v3_unreal_sim.json`으로,
모든 노드와 간선이 서버와 일치해야 한다. 팀 원래 `warehouse_map.json`은
다르므로 단순히 같은 노드 이름이라는 이유로 섞어 쓰지 않는다.

## 독립 Unreal 프로젝트 준비

팀 Unreal의 `feature/digital-twin` 커밋 `ce23ec88f8caceb90eaea73931801bb7d0ff99bd`
를 **별도 사본**으로 준비한다. 기존 프로젝트와 Content를 공유하는
junction이나 심볼릭 링크를 사용하지 않는다. 팀 원본에 직접 적용하지 않는다.
UE 5.7과 C++ 빌드 도구, 서버 Python 의존성 및 별도 aMQTT가 필요하다.

아래 패치를 표시된 순서로 적용한다. 각 패치마다 먼저 `git apply --check`를
실행하고 실패하면 중단한다. 1번과 6번은 `--unidiff-zero`도 필요하다.

1. `server/docs/unreal_local_auth.patch`
2. `server/docs/unreal_action_ack.patch`
3. `server/docs/unreal_action_position_guard.patch`
4. `server/docs/unreal_shelf_inventory.patch`
5. `server/docs/unreal_world_recovery.patch`
6. `server/docs/unreal_pickup_inventory.patch`
7. `server/docs/unreal_v3_scene_guard.patch`
8. `server/docs/unreal_v3_display_scale.patch`
9. `server/docs/unreal_v3_camera.patch`

각 패치는 Unreal 프로젝트 루트에서 적용한다. Python 에디터 자동화에 필요한
`PythonScriptPlugin`과 `EditorScriptingUtilities` 플러그인을 별도 사본에서
활성화하고 Unreal Editor 타깃을 빌드한다. Windows C++ 도구가 한글 경로를
읽지 못할 수 있으므로 영문 경로의 독립 사본을 사용하는 편이 좋다.
패치 스택의 최신 팀 소스 적용 여부는 다음으로 확인할 수 있다.

```powershell
.\.venv\Scripts\python.exe -m unittest server.tests.test_v3_unreal_patch_stack -v
```

## 레벨 구성

`server/tools/create_v3_unreal_scene.py`는 독립 프로젝트의 `NewMap`을 열어
기존 상품과 경로 액터를 정리하고 v3 맵, 상품·선반·로봇·카메라를 배치한다.
기존 대형 창고 소품을 게임에서 숨기고, 시연 사본에서만 노드 마커를 2cm로
축소한다. 원본 팀 Blueprint와 레벨은 변경하지 않는다.
**지정된 프로젝트의 레벨과 맵 파일을 수정하므로 반드시 버려도 되는 독립
사본에서만 실행한다.** 원본 Content가 junction이면 실행을 거부한다.

```powershell
$project = 'D:\경로\독립-Unreal-사본'
$env:AGV_V3_SCENE_PROJECT = $project
$env:AGV_V3_SCENE_CONFIRM = 'ISOLATED'
& 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor-Cmd.exe' `
  "$project\agvSimulation.uproject" `
  "-ExecutePythonScript=$PWD\server\tools\create_v3_unreal_scene.py" `
  -nullrhi -unattended -NoSplash -NoSound -nop4
```

`AGV_V3_ISOLATED_SCENE_SAVED` 로그와 프로세스 종료 코드를 확인한다. 레벨이
월드 파티션 외부 액터를 사용하므로, 저장 뒤 다시 열어 상품과 선반 위치가
유지됐는지 확인해야 한다. 같은 명령의 Python 스크립트 경로를
`server/tools/verify_v3_unreal_scene.py`로 바꿔 실행하고
`AGV_V3_SCENE_VERIFIED` 로그와 프로세스 종료 코드를 확인한다.

## 렌더링 시험

빌드와 레벨 준비가 완료된 뒤, 8000 포트가 비어 있을 때 실행한다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.v3_rendered_test `
  --unreal-editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --uproject "$project\agvSimulation.uproject" `
  --field-config server\examples\field_unreal_v3.json `
  --ddc 'D:\경로\별도-Unreal-DDC' --sim-speed-mm-s 100
```

시험은 임시 토큰, 로컬 격리 MQTT와 새 DB를 사용한다. 두 주문의 완료,
Unreal이 받은 위치·동작 메시지 및 픽업·선반 스냅샷 변화를 확인하고
시험 소유 프로세스만 종료한다. 로그와 `result.json`은 출력된 증거 폴더에
남는다. 데이터만으로 화면을 직접 관찰했다고 주장하지 않는다.
`--sim-speed-mm-s`는 시험용 가상 로봇의 속도만 바꾸는 선택 사항이며,
생략하면 원래 시뮬레이션 속도를 사용한다.

별도로 `server.tools.v3_unreal_guard_probe`는 자체 로컬 WebSocket 서버를
사용해 잘못된 위치의 집기·적재와 선반 위치 불일치를 거부하는지 시험한다.
이 검사는 위 서버와 같은 포트를 사용하므로 순차 실행한다.
