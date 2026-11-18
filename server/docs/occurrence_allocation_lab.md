# 반복 방문 구간의 격리 SQLite 시험

`server.occurrence_allocation_lab.OccurrenceAllocationLab`은 팀 맵의 방향 기반
경로 후보와 반복 방문 순번별 구간 자원을 별도 SQLite 파일에서 시험하는 도구다.
실제 서버의 `Coordinator`, `AllocationStore`, `SegmentProgress`, `RecoveryGate`,
MQTT 수신함이나 로봇 실행기에 연결되지 않는다. 실물·팀 브로커에 명령을 보내지
않으며, 입력하는 상태와 이벤트는 시험자가 만든 가상 자료다.

## 시험 범위

- 명시적 `simulation=True`, 테스트 맵, 진입 간선과 목표로 후보 경로를 구한다.
  초기 구간 점유, 주문과 방문 순번을 같은 DB 트랜잭션에 기록한다.
- 시험용 `rear_clear`가 현재 세션·맵·주문·방문 순번과 일치하고 다음 구간이
  비어 있으면 점유 이전, 다음 주문 기록, 이벤트 이력을 함께 커밋한다. 충돌
  또는 도중 오류가 나면 기존 기록과 점유를 보존한다.
- 새 작업·발행 상태 기록·진행과 재시작 시 같은 트랜잭션에서 다른 작업의
  이력·점유와 고아 DB 참조도 검사한다. 손상된 점유를 빈 구간으로 간주하지
  않고 오류로 보류한다.
- 발행 상태는 시험자가 `pending → uncertain → acked`로 바꾼다. 이 상태는
  실제 발행이나 로봇 수신을 증명하지 않는다. 불확실한 상태에서는 진행하지
  않고, 같은 이벤트 재전달은 중복 적용하지 않는다.
- 새 인스턴스를 초기화하면 기존 세대를 무효화하고 진행 중 작업을 `blocked`로
  남기며 점유를 보존한다. 새 작업도 보류한다. 인증된 위치·정지·후단 이탈
  증명이 없으므로 자동 재개를 제공하지 않는다.

DB의 지도 지문·세션·이벤트 이력 확인은 실험 데이터의 일관성 검사다. 값이
로봇에서 실제로 왔는지, 로봇이 경로를 따랐는지, 물리적으로 정지했는지를
증명하지 않는다. 반복 경로의 로봇 실행 순서와 스퍼는 별도 문제이므로 기존
팀 맵의 자동 배정·발행 차단은 유지한다.
이 DB는 작업 중인 가상 로봇의 점유만 다루고 미배정 로봇의 현재 위치를
자동 반영하지 않는다. 같은 로봇을 완료 후 새 작업에 재배정하는 기능도 없다.

## 실행

저장소 루트에서 기본 합성 맵과 오류 주입 시험을 실행한다.

```powershell
.\.venv\Scripts\python -m unittest server.tests.test_occurrence_allocation_lab server.tests.test_occurrence_allocation_lab_faults -v
```

팀 맵 사본에서도 확인하려면 해당 팀 코드가 준비된 경로를 지정한다.

```powershell
$env:AGV_FIELD_REPO = 'C:\팀코드경로'
.\.venv\Scripts\python -m unittest server.tests.test_occurrence_allocation_lab_team_map -v
```

팀 맵 시험은 사본에 `M3`, `P_green`, `P_purple` 시험용 대기점을 메모리에서
추가하고 합성 정지 이벤트로 공유 구역 경합을 유발한다. 이 대기점은 실제 맵의
안전한 정차 위치가 아니다. 두 시험 모두 임시 디렉터리의 새 DB만 사용한다.
