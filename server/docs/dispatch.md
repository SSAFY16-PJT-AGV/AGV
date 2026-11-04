# 작업 배정 후보 계산

설계 문서 7.1의 가용 로봇 중 상차 지점까지 그래프 거리가 가장 짧은 로봇을
선택하는 독립 계산이다. `MapGraph`의 방향성과 실제 엣지 폴리라인 거리를 따른다.
도달 불가능한 로봇은 후보에서 제외하고, 정확히 같은 거리인 경우 입력 순서와
관계없이 로봇 ID의 Python 문자열 정렬 순서가 앞선 쪽을 선택한다. 동률 처리는
설계에 구체화되지 않아 정한 내부 규칙이며 팀의 실제 배정 우선순위가 아니다.

```python
from server.dispatch import RobotCandidate, choose_robot

# graph는 이미 검증된 server.routing.MapGraph 객체다.
candidates = [RobotCandidate("robot1", "N1", True),
              RobotCandidate("robot2", "N2", False)]
choice = choose_robot(graph, "N4", candidates)
if choice is not None:
    print(choice.robot_id, choice.route.nodes, choice.route.distance_mm)
```

선택 가능하고 도달 가능한 로봇이 없으면 `None`을 반환한다. 중복 ID, 잘못된
후보, 맵에 없는 상차 노드나 가용 로봇의 노드는 오류로 처리한다. 비가용 로봇의
노드는 거리 계산에 사용하지 않는다.

결과는 시점에 따른 **배정 후보**일 뿐이다. 호출자가 각 로봇의 현재 노드와 가용성,
연결·위치 정보의 유효성과 최신성, 맵 버전을 검증해야 한다. 계산 이후 상태가 바뀔 수
있으며 이 모듈은 작업의 원자적 할당, 경로·구간 예약, 로봇 제어 명령, 충돌 방지,
접속 상태 확인 또는 장애 복구를 수행하지 않는다. 실제 명령 전에 별도의 원자적 할당과
구간 예약 및 현재 상태 재확인이 필요하다. MQTT·HTTP 메시지 필드는 추가하지 않는다.
