# 최신 팀 로봇의 반복 방문·단절 차단 통합 제안

팀 `feature/field-run`의 `0e00569`를 대상으로 한 별도 재현 결과다. 팀 로봇
원본에는 적용하지 않았다. [`field_sequence_disconnect_0e00569.patch`](field_sequence_disconnect_0e00569.patch)는
기존 제한적 반복 경로 제안과 단절 시 주문 처리 차단 제안을 한 파일로 합친
**대안**이며, 기존 두 패치와 같은 파일을 수정하므로 중복 적용하지 않는다.

기존 반복 패치를 최신 소스에 그대로 적용하면 `controller.py`의 한 부분이
최신 상태 표시·초기 앵커 변경과 충돌한다. 통합 제안은 해당 최신 변경을
보존하며 반복 경로의 순서 검증과 단절 경합 차단을 함께 적용한다.

## 동작과 범위

- 반복 경로의 노드 방문 순번·진입 방향·관측을 확인한다. 모호하거나 관측이
  빠지면 도착으로 추측하지 않는다. 비가시 via와 스퍼 끝이 포함된 반복 경로는
  기존 제한적 제안처럼 거부한다.
- MQTT 연결 세대로 주문을 구분하고 단절 콜백이 시작되면 신호를 남겨 대기
  주문을 버린다. 이미 꺼낸 구세대 주문도 거부하며, **반복 여부와 관계없이**
  재연결만으로 새 주문이나 주행을 재개하지 않는다. 새 작업의 첫 모터 출력
  반환 전후에 관측된 단절을 추가로 확인한다.
- 반복 경로의 정지 이벤트는 일회 MQTT 연결로만 발행한다. PUBACK이 없거나
  단절이 감지되면 결과 불명으로 보류하고 자동 재발행하지 않는다. 반복이 아닌
  기존 이벤트의 Paho 큐·불명 결과와 서버/로봇 재동기화 문제는 해결하지 않는다.
- 이 제안은 물리적 단절 직후의 즉시 정지, 모터 출력 검사 후 실제 적용 전의
  경합, 실제 위치·후단 이탈, 신원 인증, TLS/ACL, 정지·제동 거리 또는 서버
  인계의 안전을 증명하지 않는다. 반복 경로용 임시 이벤트 클라이언트 ID,
  TLS, ACL, 동기 네트워크 대기 및 PUBACK의 의미에 관한 추가 제한은
  [기존 설명](field_nav_sequence_limited.md)을 그대로 따른다.
- 로봇이나 서버를 재시작해도 기존 작업·방향·점유가 실제 상태와 일치한다고
  간주하지 않는다. 실물 런타임 차단은 팀 코드 반영·서버 계약 확정 및 별도
  검증 없이 해제하지 않는다.

## 재현 시험

1. `0e00569`의 **새 독립 사본**을 만들고 아래 명령을 그 폴더에서 실행한다.
   `GIT_CEILING_DIRECTORIES`는 사본이 서버 저장소 `.venv` 안에 있을 때 Git이
   바깥 저장소를 찾아 패치 파일을 조용히 건너뛰는 것을 막는다.
2. 두 번째 작은 패치는 기존 반복 테스트에만 적용한다. 최신 로봇은 새 작업의
   초기 앵커를 기록하므로 실패 전 앵커를 비교하게 하고, 재연결 차단 기대를
   비반복 경로까지 확장한다. 기존 테스트 원본은 바꾸지 않는다.

```powershell
Set-Location 'D:\관통\AGV-analysis\.venv\field-merged-verify'
$server = 'D:\관통\AGV-analysis'
$env:GIT_CEILING_DIRECTORIES = (Split-Path (Get-Location).Path -Parent)
Copy-Item "$server\server\tests\field_nav_sequence_cases.py" 'agv\tests\test_sequence_visit_patch.py'
git apply --check "$server\server\docs\field_sequence_disconnect_0e00569.patch"
git apply "$server\server\docs\field_sequence_disconnect_0e00569.patch"
git apply --check "$server\server\docs\field_sequence_test_updates_0e00569.patch"
git apply "$server\server\docs\field_sequence_test_updates_0e00569.patch"
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = "$server\.venv\field-mat-deps"
$env:TEMP = "$server\.venv\pr59-test-temp"
$env:TMP = $env:TEMP
& "$server\.venv\Scripts\python.exe" -m unittest `
  agv.tests.test_sequence_visit_patch.SequenceTests `
  agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_revisit_completes_with_observed_split `
  agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_single_release_revisits_each_junction `
  agv.tests.test_sequence_visit_patch.SequenceMatTests.test_team_mat_repeated_release_never_fakes_a_missed_junction `
  agv.tests.test_link_disconnect_fence agv.tests.test_link -q
```

별도 원본 사본에 두 패치를 적용하고 실행해 **56개 중 54개 통과, 2개 생략**을
확인했다. 두 생략은 기존 팀 테스트의 `AGV_SERVER_REPO` 미설정 때문이다.
단절 신규 6개, 반복 방문 단위 시험과 세 종류의 가상 매트 사례, 최신 팀 링크
시험이 포함된다. Paho 2.x에서 callback API v1 사용 중단 예정 경고가 나온다.
대역·합성 영상 시험이며 전체 로봇 테스트, 실물 JETANK, 팀 공용 브로커 및
실브로커 단절을 검증한 결과가 아니다.
