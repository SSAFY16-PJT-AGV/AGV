# Unreal 팀 선반 서버 재시작 격리 시험

`team_shelf_rendered_restart_test.py`는 별도 Unreal 6선반 사본, 새 DB,
루프백 브로커 및 서버를 사용한다. 첫 우유 주문을 완료한 뒤 **같은 게임과
브로커를 유지하면서 서버만 종료·재시작**한다. 같은 주문 키·완료 기록,
24개 슬롯, 두 가상 AGV의 저장 위치·방향과 웹 재고를 대조한 다음 휴지를
주문해 두 번째 집기·적재를 확인한다. 불명 결과에서 POST는 재발행하지 않는다.

테스트는 각 동작의 Unreal 수신 로그와 슬롯 장부를 검증한다. `rendered: true`는
렌더링 게임 프로세스를 사용했다는 뜻이지 화면을 사람이 관찰했다는 뜻은
아니다. 실제 로봇·카메라·실측 선반·물리 정지 또는 운영 브로커는 시험하지
않으며, 작업 중단 후 자동 재개를 주장하지 않는다.

Unreal과 독립 사본의 준비·패치는 [팀 선반 Unreal 시연](unreal_team_shelf_simulation.md)을
참조한다. 8000 포트와 aMQTT 의존성이 필요하다. 아래 경로는 이 PC의 예시다.
인증 토큰은 각 시험에서 새로 생성하고 출력하지 않는다.

```powershell
.\.venv\Scripts\python.exe -m server.tools.team_shelf_rendered_restart_test `
  --unreal-editor 'D:\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe' `
  --uproject '.venv\v3-team-shelf-full\agvSimulation.uproject' `
  --ddc '.venv\v3-warm-render-ddc'
```

새 증거 폴더의 `result.json`, 두 서버 로그와 Unreal 로그에 결과가 남으며,
테스트가 시작한 서버·브로커·게임 프로세스만 정리한다. 셰이더 캐시가 없으면
초기화가 오래 걸릴 수 있으므로 `--start-timeout`으로 대기 시간을 늘린다.
