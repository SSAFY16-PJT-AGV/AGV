# 팀 로봇 MQTT·서버 연동 대조

이 문서는 `feature/field-run`의 `ef9e60001a0eb67c21fb81ea10c262eec343a4e6`과
서버·통신 브랜치의 기존 구현을 대조한 기록이다. 다음 단계의 **격리 가상
시험**을 위한 것이며, 실물 주행이나 팀 브로커 연결의 승인이 아니다.
브랜치나 규격이 바뀌면 다시 대조한다.

## 맞는 부분

| 항목 | 팀 로봇 | 서버 |
| --- | --- | --- |
| 주문 | `agv/{id}/order`, QoS 1. `order_id`, `seq`, `map_id`, `nodes`, `released_until`, `action_at_end` | 같은 여섯 필드를 발행하는 격리 시뮬레이션 발행기 |
| 상태 | `agv/{id}/state`, QoS 0, retained | 상태 관측 서비스가 구독. 늦게 들어온 retained 상태는 과거 관측으로 구분 |
| 연결 | `agv/{id}/conn`, QoS 1, retained, `online`/`offline` | 같은 값을 관측. 온라인 자체는 위치·정지·작업 수행의 증거가 아님 |
| 구간 이벤트 | `agv/{id}/event`, QoS 1, `rear_clear`의 8개 필드 | 격리 시뮬레이션 진행기가 같은 필드 형식을 사용 |

맵의 `map_id@v<version>` 표기와 경로에서 순차적으로 `released_until`을
앞당기는 주문 형식도 같다. 팀 로봇의 `SegmentRules`는 현재 서버
`plan_segment`의 자원 형식을 따라 계산하며, 양쪽의 대기 지점 설정과
동일한 맵이 전제다. 필드 형식이 같다는 것은 물리적 후단 이탈이나
실물 충돌 방지가 증명됐다는 뜻이 아니다.

## 실제로 다른 점

- **팀 맵 경로 제약:** `mat_ssafy_v2`의 `P_yellow` 교차점은 진입 간선별
  통과 가능 간선을 제한한다. 기존 서버 최단 경로가 선택한 `S3 → P_yellow
  → X2`는 팀 로봇 내비게이터에서 허용되지 않는다. 로봇 진행 방향, 유턴과
  스퍼를 고려하는 경로 계산이 준비되기 전까지 서버는 이런 주행 메타데이터가
  있는 맵의 이동 경로 배정과 저장된 작업의 구간 진행을 거부한다. 맵 로딩과
  `plan_segment` 자원 계산은 가능하지만 그 결과는 주행 허가가 아니다. 맵의
  식별 메타데이터가 빠진 경우 이 차단만으로 안전을 증명할 수 없다.
  팀 `NavMap`은 현재 진입 간선과 방향을 입력받아 교차·유턴을 고려하며,
  경우에 따라 같은 노드를 다시 방문한다. 연결하려면 같은 맵 버전에 대한
  신뢰할 수 있는 현재 진입 간선·방향과 스퍼 진출입, 반복 방문의 경로 순번을
  서버 예약·후속 명령·재시작 복구에 일관되게 전달하고 검증해야 한다.
  팀 내비게이터도 교차점의 일치하는 `through` 항목을 찾지 못하면 일반
  출구로 되돌아가는 경로가 있어 그대로 복사하는 것만으로 누락된 제약을
  안전하게 거부한다고 가정하지 않는다.
- **주문 검증 상한:** 로봇 `agv/link/orders.py`는 `seq >= 1`, 노드 2~256개,
  ID 128자 이하와 64 KiB 이하 UTF-8 바이트 payload를 요구한다. 서버의
  범용 시뮬레이션 `validate_order`는 `seq=0`, 노드 1개와 최대 256 KiB도
  구조적으로 허용한다. 실제 서버 coordinator와 구간 계획기는 별도 검증을
  수행하므로 모든 시뮬레이션 주문이 잘못됐다는 뜻은 아니다. 팀 로봇을
  대상으로 하는 경계에서는 더 엄격한 로봇 조건을 발행 전에 검증해야 한다.
  `server.field_robot_contract.validate_field_robot_order`는 이 정적 조건의
  사전검증 함수다. 기본 발행기에는 자동 적용되지 않는다. 명시적으로 켠
  팀 맵 격리 시뮬레이션은 초기·후속 주문에 이 형식 검사를 적용한다.
  해당 함수는 로봇의 현재 맵·위치·세션, 이전 주문의 상태나 후속 구간 허가를
  확인하지 않으며 실물 발행 또는 주행 승인 수단이 아니다.
- **반복 노드 경로:** 팀 로봇은 같은 노드가 경로에 다시 등장해도 해제 순번을
  추적하지만, 서버의 `plan_segment`와 복구·진행 처리는 모호한 `released_until`
  해석을 피하기 위해 반복 노드가 있는 경로를 거부한다. 이 제약을 없애려면
  순번을 포함한 계약과 예약·복구 처리를 함께 검증해야 하며 단순히 검사를
  제거해서는 안 된다.
  추가로 팀 `NavMap.route_edges`는 전달된 경로를 순서 없는 간선 집합으로
  취급한다. `A→B→C→B→D`에 대해 다음 간선으로 `B→D`를 골라 C 방문을
  건너뛸 수 있음을 팀 코드의 격리 시험으로 재현했다. 해제 순번을 추적한다는
  사실만으로 로봇이 전체 방문 순서를 지킨다는 뜻은 아니므로, 팀 내비게이터의
  실행 순서와 서버 예약의 일치를 별도로 해결하고 시험해야 한다.
  검토용 [반복 경로 거부 패치](field_nav_repeat_guard.md)는 순서 지원 전까지
  팀 로봇이 이런 경로의 실행을 시작하지 않도록 한다. 팀 원본에는 미적용이다.
- **관측되지 않는 배터리:** 로봇은 배터리를 측정하지 않으면 `battery: null`을
  보낸다. 서버 MQTT 관측은 원본을 보관하지만 현재 Unreal 브리지는 유효한 숫자
  배터리가 없는 상태 전체를 전달하지 않는다. 기존 Unreal 클라이언트는
  배터리 필드를 읽지 못하면 0을 적용하므로 null을 생략하거나 임의의 숫자를
  넣으면 미측정이 실제 수치처럼 표시된다. unknown 표현과 Unreal 처리 방식을
  맞추기 전에는 값을 만들어 내지 않는다.
- **세션과 복구:** 로봇은 프로세스마다 `robot_session`을 새로 만들고 수신
  주문 이력을 메모리에만 보관한다. 서버 시뮬레이션은 로봇·작업별 세션을
  미리 지정하고 새 challenge, 마지막 명령 digest와 정지 확인을 요구한다.
  팀 로봇에는 이 challenge 응답과 재부팅 후 주문 이력 복원 규약이 없다.
  세션 ID만 관측해 자동 재개나 점유 해제를 허가해서는 안 된다.
- **후단 이탈과 이벤트 유실:** 로봇의 현 `rear_clear` 자원은 해제 노드에
  정지했을 때 맵과 다음 구간으로 계산하며 로봇 후단이 실제 자원을 벗어났는지
  측정하지 않는다. 이벤트 발행 결과도 controller가 확인하지 않으며 MQTT
  클라이언트는 단절 중 QoS 1 이벤트를 보관해 재연결 후 발행할 수 있다.
  서버의 현재 진행 처리기는 가상 환경 전용이다. 실물에는 안전한 확인·중복
  처리·유실 및 순서 역전 대응이 별도로 필요하다.
- **명령 거절과 동작:** 팀 로봇은 주문 거절을 상태의 `rejected`에 담는다.
  서버 상태 관측은 이 필드를 저장하지만 이것만으로 주문 실패를 확정하는
  실물 작업 흐름은 없다. 팀 로봇의 연결 controller는 `action_at_end`를
  검사하고 보관하지만 해당 코드에서 실제 집기·적재 실행이나 완료 ACK를
  연결하지 않으며 `carrying`도 현재 null이다. Unreal에서의 집기·적재 ACK는
  가상 액터의 결과다.
- **연결 복구 기본값:** 팀 로봇은 단절이 설정 시간 이상 지속되면 정지를
  요청하고, 구간 중간에 선 경우 기본적으로 재연결만으로 주행을 재개하지
  않는다. `resume_after_reconnect` 설정은 존재하지만 기존 서버의 가상 복구
  검증이 실물의 자동 재개에 적용된다는 뜻은 아니다.
- **인증 조건:** 서버는 MQTT 사용자명만 설정한 경우에도 TLS CA를 필수로
  요구한다. 팀 로봇 코드는 비밀번호가 있을 때 CA를 요구하지만 사용자명만
  있을 때는 CA 없이 허용한다. 팀 운영 설정과 인증 정책을 맞추기 전 격리
  브로커 시험 결과로 팀 브로커의 TLS를 검증했다고 말할 수 없다.

## 시험과 후속 확인 원칙

- 팀 `test_link.py` 전체 11개는 안전 차단 전에는 통과했다. 차단 후에는
  이 중 서버의 팀 맵 최단 경로 호출을 기대하는 2개가 `UnsupportedRobotRoutingError`로
  실패한다. 나머지 9개는 통과했다. 이는 현재 자동 배정이 팀 맵을 지원하지
  않는다는 뜻이며, 자원 계산 비교는 별도 서버 시험으로 확인한다.
- 팀 로봇 구현을 가상 runner·가짜 장치와 연결하고, 테스트가 직접 만든 별도
  브로커만 사용해 실제 주문 파싱과 상태·이벤트 필드를 확인한다.
- 서버의 `sim-` 대상, 기본 비활성화, 구간 점유 및 복구 증명 검사를 완화하지
  않는다. `sim-` 이름과 루프백 주소만으로 네트워크 격리가 증명되지는 않는다.
- 실제 장비 연동에는 현장 맵의 단위·좌표·주행 방향·공유 구역, 장치별 인증,
  명령 수락과 종료 증거, 후단 이탈 판정, 정지·재시작 복구, 집기·적재 결과를
  팀과 함께 정하고 별도로 검증해야 한다.

### 방향 및 방문 순번 계산 실험

`server.field_map_planner.plan_field_route`는 동일 맵 버전과 **명시된 진입
간선**을 입력받아 방향·교차·유턴을 고려한 본선 경로 후보를 계산한다.
비용에는 팀 로봇의 측정 길이와 유턴 비용을 쓰며 노드 재방문을 보존한다.
교차 규칙이 불완전하면 거부하고, 목적지 스퍼 출입은 아직 지원하지 않는다.
입력 간선은 현재 위치 증명이 아니다.

`server.field_dispatch.choose_field_robot`은 `sim-` 후보의 명시된 진입
간선과 현재 노드가 일치하는지 검사한 뒤, 방향상 갈 수 있는 후보 중 가장
가까운 가상 로봇을 선택한다. 신선한 상태·위치·로봇 신원은 호출자가 별도
검증해야 하며, 선택만으로 점유 예약이나 명령 발행을 하지 않는다. 현재는
기존 `Coordinator`에 연결되지 않은 시험용 계산이다.

`server.route_occurrence.plan_occurrence_segment`와 `occurrence_clearance`는
노드 이름이 아닌 경로의 방문 순번으로 구간 자원과 이탈 **후보**를 계산한다.
`visit_indices_from_orders`는 전달된 연속 주문 이력이 서로 일치하는지 확인해
방문 인덱스를 복원하지만, 이력이 실제 수신됐는지 증명하지는 않는다. 세 함수는
DB에 순번을 저장하거나 물리적 이탈을 확인하지 않는다. 현재 세 계산은 기존
배정·예약·복구·MQTT 발행에 연결되지 않으며, 팀 맵 자동 운행 차단은 유지된다.

`server.occurrence_journal.OccurrenceJournal`은 이 순번과 전체 주문 이력을
**별도의 실험용 SQLite 파일**에 저장·재검산한다. 재조회·추가 시 호출자가
지정한 로봇·세션·맵 지문을 기존 기록과 비교하며, 두 동시 갱신 중 하나만
허용하고 처리 중 오류가 나면 트랜잭션을 되돌린다. 입력된 식별자와 지문은
호출자가 제공한 값으로 로봇 인증, 현재 위치, 주문 수신이나 정지의 증거가
아니다. 기존 주문·예약 DB나 MQTT 발행과 한 트랜잭션으로 묶여 있지 않으므로
이를 서버 재시작 후 자동 주행 또는 기존 구간 점유 복구에 사용하면 안 된다.
방문 순번·점유·후속 주문을 한 DB 트랜잭션에서 다루는 별도 실험은
[반복 방문 구간 SQLite 시험](occurrence_allocation_lab.md)을 참고한다. 이 역시
기존 워크플로에 연결되지 않으며 실제 AGV 운행에 사용할 수 없다.

```powershell
.\.venv\Scripts\python -m unittest server.tests.test_occurrence_journal server.tests.test_occurrence_journal_faults -v
```

합성 대기점을 추가한 팀 맵 사본에서 두 가상 AGV가 `X1`과 `X2`의 같은 공유
구역에 경합하는 계산 시험도 제공한다. 이 시험의 점유 해제는 가정이며 메모리
자원의 순차 lease를 사용하므로 DB의 원자적 전이나 실물 안전 증거가 아니다.

```powershell
$env:AGV_FIELD_REPO = 'C:\팀코드경로'
.\.venv\Scripts\python -m unittest server.tests.test_field_map_planner server.tests.test_route_occurrence server.tests.test_route_history server.tests.test_field_planner_occurrences_integration -v
```

### 주문 사전검증 사용법

가상 시험 코드에서 서버 주문 dict를 별도로 검사할 수 있다. 실패하면
`ValueError`를 발생시키고 입력을 변경하지 않는다.

```python
from server.field_robot_contract import validate_field_robot_order

validate_field_robot_order(order)  # 통과해도 수신·주행은 보장하지 않음
```

검증기는 서버가 실제로 사용하는 UTF-8 직렬화 크기를 검사한다. robot의
상태에 따라 달라지는 주문 순서·현재 위치·맵 일치는 검사할 수 없다. 기본
시뮬레이션 발행기에는 연결되지 않으며 실물 주문을 발행하는 기능도 없다.
아래 단위 시험을 실행할 수 있다. 팀 코드와의 비교까지 하려면 별도로 준비한
`feature/field-run` 사본의 `agv/link/orders.py` 경로를 환경변수에 넣는다.

```powershell
.\.venv\Scripts\python -m unittest server.tests.test_field_robot_contract -v
$env:AGV_FIELD_ROBOT_ORDER_PATH = 'C:\팀코드경로\agv\link\orders.py'
.\.venv\Scripts\python -m unittest server.tests.test_field_robot_contract -v
Remove-Item Env:AGV_FIELD_ROBOT_ORDER_PATH
```

로봇 사본 경로를 지정하지 않으면 로봇 실제 구현 비교 테스트 3개만 생략된다.
검증 시에는 기록한 field-run 커밋의 원본 `orders.py`를 사용하고, 새 팀 코드가
올라오면 변경 사항을 다시 확인한다.

### 격리 MQTT 및 구간 자원 비교 시험

팀 `feature/field-run`의 기록된 커밋을 별도 디렉터리에 준비하고,
OpenCV와 NumPy를 별도 폴더에 설치한다. 팀 코드의 실행에 필요한 의존성을
관리하는 방법은 팀 저장소 안내를 따른다. 아래 경로는 자신의 PC에 맞춘다.
`AMQTT_TEST_PATH`는 격리 시험용 aMQTT가 설치된 폴더다.

```powershell
$env:PYTHONUTF8 = '1'
$env:AGV_FIELD_REPO = 'C:\팀코드경로'
$env:AGV_FIELD_MAT_DEPS = 'C:\가상매트_의존성경로'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python -m unittest server.tests.test_field_map_progress_guard server.tests.test_field_robot_mqtt_integration -v
```

MQTT 시험은 팀 맵의 `zones` 대기 구역과 수동 경로
`P_orange → S1 → M1 → P_purple → D_P_purple`을 사용한다. 서버 발행기로
명령을 보내고 가상 매트의 팀 로봇이 보내는 상태·`rear_clear`와 서버의
구간 자원 계산을 비교한다. 배터리 미측정 값도 MQTT 관측에서 null로 남는다.
자동 경로 계산, 예약·후속 명령 및
실물 주행의 종단 시험은 아니다. 구간 자원 비교 시험은 서버와 로봇이 같은
자원 이름을 계산하는지 확인할 뿐 주행 가능한 경로라는 증거가 아니다.
별도 수동 경로를 `D_P_green`까지 보낸 가상 MQTT 시험은 `P_green`을 인지한
뒤 `nav_off_route`로 중단됐다. 같은 경로를 MQTT 없이 팀 FakeLink와 가상
매트에서 세 시드로 반복해도 분기 진입 기록 없이 본선 E7로 가다 중단됐다.
서버 MQTT가 아닌 분기 인식·주행이나 맵·가상매트 정합 쪽의 조사 대상이며,
정확한 원인은 아직 확정되지 않았다. 자원 계산만 성공한 경로였다.
