# 가상 명령 응답 관측의 예약 DB 기록

`server.sim_command_observations.SimCommandObservations`는 **격리 가상
환경**에서만 로봇의 `accepted`·`rejected` 응답을 시험용 주장으로 기록한다.
팀이 합의한 응답 규격이 아니며, 이 저장 모듈 자체는 네트워크 연결을 소유하지
않고 기본 `SimRuntime`에 자동 연결되지 않는다. 별도로 명시적으로 실행하는
[가상 MQTT 수신기](sim_command_observation_receiver.md)는 제공된다.
기록은 로봇의 신원·수신·실행, 위치·물리 정지나 구간
이탈을 증명하지 않으며 명령 발행·구간 이동·점유 해제에 영향을 주지 않는다.

초기화에는 팀 맵 경로가 켜진 `SegmentProgress`, 해당 가상 복구 게이트,
명시적인 로봇·작업·세션 대응과 `simulation=True`가 필요하다. 별도 실험용
DB가 아니라 해당 progress와 **같은 allocation SQLite 파일**의 별도 테이블에
저장한다. 새 파일을 만들지 않고 기존 테이블을 덮어쓰지 않는다.
최초 `initialize()`는 **allocation_jobs가 하나도 없을 때**, 즉 가상 작업을
배정하기 전에 호출해야 한다. 이때 현재 복구 게이트 epoch를 DB의 별도
메타데이터에 고정한다. 이미 배정된 DB에 관측 기능을 뒤늦게 설치하면 이전
작업의 출처를 구별할 수 없어 거부한다. 기존 테이블이 있어도 새로운 gate
epoch에서는 초기화와 기록을 모두 거부한다. 현재는 재시작 뒤 이 관측 테이블을
새 epoch에 다시 묶거나 보류를 해소하는 절차를 제공하지 않는다. 이 제한은
완료된 과거 작업이 남아 있어도 유지된다.

```python
from server.sim_command_observations import SimCommandObservations

observations = SimCommandObservations(
    progress, {'sim-r1': {'T1': 'boot-1'}}, simulation=True)
observations.initialize()
result = observations.record('sim-r1', response, retained=False)
```

`response`는 팀 협의 전 시험 제안인 다음 필드만 받는다.

```python
response = {
    'event_id': '예시', 'robot_session': 'boot-1', 'order_id': 'T1',
    'seq': 1, 'map_id': 'lab@v1', 'order_digest': '전송바이트의 SHA-256',
    'outcome': 'accepted',  # 또는 rejected
}
```

정확한 기존 주문 전송 바이트와 SHA-256, 현재 순번·맵·로봇·설정된 세션,
기존 방문 이력과 현재 구간 점유를 **같은 SQLite 쓰기 트랜잭션**에서
확인하고 주장만 삽입한다. 순번 1은 기존 outbox, 이후 순번은 기존 progress
주문을 확인한다. 전송 시도 전 `pending`은 거부하고, 로컬 전송 결과 불명인
`uncertain`과 브로커 PUBACK이 기록된 `acked`는 모두 허용한다. 이것이 해당
주문이 실제 전송됐다는 증명은 아니다. 재전송 메시지의 retain 플래그도
발행 당시 retain 여부를 증명하지 않는다.

같은 이벤트 ID·본문의 반복은 `duplicate`, 같은 ID에 다른 본문이나 동일
명령에 다른 이벤트 ID가 오면 충돌로 거부한다. 저장 당시 현재 주문의 순번만
다루며 진행한 뒤의 늦은 이전 순번 응답은 중복 여부와 관계없이 거부한다.
시뮬레이션 게이트의 현재 세대가 바뀌거나 작업이 차단되면 기록하지 않는다.
세션은 호출자가 설정한 가상 대응과 비교할 뿐 수신 측 인증이 아니다.
관련 런타임 재시작 시험은 같은 Python 프로세스에서 런타임 인스턴스만
재생성한다. 서버 프로세스 강제 종료 시험은 아니다.

관측 및 epoch 메타 테이블에 연결된 트리거는 모두 거부한다. 관측 삽입이
다른 테이블을 수정하는 숨겨진 동작을 차단하기 위한 제한이다. 기존
`SimHandoffStore`의 **다른 테이블에 연결된 트리거**는 그대로 사용할 수 있다.
동일 스레드가 이미 gate 트랜잭션 안에 있으면 재진입은 즉시 거부한다.
`SimHandoffStore`를 사용하는 경우 기존 `validate_bound`를 DB 연결 전후와
커밋 전에 실행해 저장소 파일 정체성을 확인한다. 이 검사는 파일 교체의 모든
시점 경합을 원천적으로 막거나 파일 내용을 인증하지는 않는다. 일반
`AllocationStore`에는 독립적인 파일 정체성 검증이 없다.

이 저장 모듈은 기존 데모나 `SimRuntime`에 자동 연결되지 않는다. 테이블 내용의
외부 위변조 방지·복구, 수동 해소, 실물 재시작 후 상태 확정, 실제 ACK 계약은
다루지 않는다. 단일 DB 트랜잭션은 기록 시점의 논리적 정합성만 제공하며 실제
장비의 안전을 보장하지 않는다. 별도 [실험 장부](proposed_command_journal.md)의
응답과 동기화하지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest server.tests.test_sim_command_observations -v
```
