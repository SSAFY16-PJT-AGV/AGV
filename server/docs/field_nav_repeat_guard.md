# 팀 Navigator의 반복 노드 경로 실행 차단 제안

`feature/field-run` `ef9e60001a0eb67c21fb81ea10c262eec343a4e6` 기준의
별도 재현 패치다. 팀 원본 브랜치에는 적용되지 않는다.

팀 로봇은 경로의 방문 순번을 기록하지만, 다음 간선을 고를 때는 전체 경로의
간선 집합 안에서 목표까지 최단 경로를 선택한다. 이 때문에 예를 들어
`A → B → C → B → D`의 `B → C → B` 방문을 건너뛰어 `B → D`로 갈 수 있다.
따라서 경로의 예약과 실제 이동이 어긋날 수 있다.

[field_nav_repeat_guard.patch](field_nav_repeat_guard.patch)는 `set_route`가
내비게이터 상태를 바꾸기 전에 중복 노드가 있는 경로를 거부한다. 기존
`LinkController`는 이 오류를 받아 주문을 거절하고 주행을 시작하지 않는다.
중복이 없는 기존 경로는 그대로 처리한다. 이 변경은 **반복 경로를 지원하는
기능이 아니다**. 잘못된 실행을 피하는 임시 제한이며, 실제 물리적 정지나
충돌 안전을 보증하지 않는다.

동일한 팀 코드 사본을 준비한 뒤 팀 저장소의 해당 버전에서 패치의 적용
가능성을 먼저 확인한다. 원본 작업물에 영향을 주지 않도록 별도 사본에서
시험하고, 패치를 팀 브랜치에 반영하려면 팀과 검토한다.

```powershell
# $team: 팀 field-run 체크아웃. $server: 서버 저장소 경로
git -C $team apply --check "$server\server\docs\field_nav_repeat_guard.patch"
git -C $team apply "$server\server\docs\field_nav_repeat_guard.patch"
```

서버 쪽 선택적 시험은 지정된 팀 코드에서 새 임시 사본을 만들어 패치를
적용하고, 반복 주문 거부·기존 정상 경로 수락을 가짜 runner와 `FakeLink`로
확인한다. 테스트는 지정된 팀 원본을 수정하지 않는다.

```powershell
$env:AGV_FIELD_REPO = 'C:\팀코드경로'
.\.venv\Scripts\python -m unittest server.tests.test_field_navigator_repeat_guard -v
```

반복 방문을 실제로 지원하려면 각 위치 가설과 진행 이력에 방문 순번을
포함해 분기·유턴·스퍼·미관측 노드 처리 전체에서 순서를 검증해야 한다.
모호한 위치나 지연 이벤트에서는 움직임을 보류해야 하며, 서버 예약·복구
인덱스와 동일한지 검증하기 전에는 안전 차단을 해제하지 않는다.
