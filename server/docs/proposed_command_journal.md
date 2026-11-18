# 격리 시험용 명령·응답 장부 제안

`server.proposed_command_journal.ProposedCommandJournal`은 서버가 주문의
발행 시도와 로봇의 수락·거절 **가상 응답을 기록하는 방법을 시험**하는
별도 SQLite 장부다. 실제 팀이 합의한 응답 메시지는 아니다. 기존
`AllocationStore`·`Coordinator`·`SegmentProgress`, 웹 주문과 통합되지
않으며 MQTT에 접속·발행하거나 점유를 만들거나 해제하지 않는다.
이 기록은 실물 로봇의 신원, 명령 수신·수행, 위치·정지·후단 이탈이나
운행 허가를 증명하지 않는다. 실물 브로커·장비에는 연결하지 않는다.

`create(path, simulation=True)`는 새 파일을 배타적으로 만들고, 반환된
객체의 `creation_id`를 DB와 **별개로 보존**해야 한다. 다시 열 때
`open_existing(path, simulation=True, expected_creation_id=보존한_ID)`로
그 값을 제공해야 하며, ID 누락·불일치나 다른 유효 장부를 거부한다.
연결 전후 파일 식별자도 검사한다. 이 검사는 연결 과정의 모든 교체 경합을
원자적으로 차단하거나 내용의 위변조·출처를 인증하지 못한다. ID를 DB와
함께 바꾸거나 독립 보존값도 신뢰할 수 없다면 이 검사만으로 탐지하지 못한다.
동일한 `creation_id`가 들어 있는 과거 DB 백업으로 되돌리는 경우에도 이
장부만으로 롤백을 감지할 수 없다. 실물 연동에는 DB 외부에 독립적으로
보존되는 이력·세대 증거와 그 검증 절차를 별도로 마련해야 한다.
로봇 ID에는 격리 발행기와 같은 `sim-` 제한이 적용된다. 각 명령은
`(robot_id, order_id, seq)`와 로봇 세션·맵·실제 MQTT 직렬화 바이트,
그 바이트의 SHA-256을 저장한다. 이 digest는 복구 제안의 정렬된 JSON
digest와 다를 수 있다. 다른 로봇이 같은 주문 ID와 순번을 공유하지 못한다.

- `stage(robot, session, order)`는 팀 로봇 형식의 여섯 필드를 검사한다.
  첫 순번은 1이며, 이후 순번은 이전 순번이 시험용 `accepted`로 기록된
  직후에만 연속적으로 추가한다. 맵·세션·전체 노드·끝 동작은 같아야 한다.
  이는 **장부 시험의 보수적 규칙**이며 실제 주행 순서·경로 검증이 아니다.
  작업 완료나 점유 해제 계약이 아직 없으므로, 한 로봇에 한번 기록한
  `order_id`와 다른 주문은 상태가 무엇이든 같은 장부에 추가할 수 없다.
  `released_until`이 순방향으로 전진하는지, 반복 노드가 어느 방문 순번을
  뜻하는지는 이 장부가 검증하지 않는다. `accepted` 기록이나 연속 순번을
  주행 허가, 방문 순서 준수 또는 후속 구간 예약의 근거로 사용하면 안 된다.
- `mark_attempted(robot, order_id, seq, expected_payload=wire_bytes)`는
  불변 `bytes`로 전달한 발행 예정 내용이 저장된 전송 바이트와 정확히 같은지
  같은 트랜잭션에서 확인한 뒤 `pending`을 `uncertain`으로 한 번만 영속
  전이하고, 검사된 저장 바이트를 반환한다. 내용이 다르면 상태를 바꾸지
  않는다. 호출자는 반환된 그 바이트와 실제 전송 바이트가 동일하도록 별도
  보장해야 한다. 장부 메서드 자체는 전송을 강제하거나 막지 않으며, 단일
  장부의 성공이나 로컬 MQTT 결과는 실물에 대한 발행 승인이 아니다.
- `record_response(robot, response)`는 아래 시험용 응답을 동일 세션·맵·
  주문·순번·정확한 전송 바이트 digest와 대조한다. 동일 이벤트 ID와
  내용의 재입력은 변경 없이 처리하고, 같은 ID에 다른 내용이나 한 명령에
  대한 두 번째 응답은 거부한다. `retained=True` 또는 불명 값은 거부한다.
  수신 시 retain 플래그만으로 발행 시 retain 여부를 보장할 수는 없다.

```python
response = {
    'event_id': 'example-event', 'robot_session': 'example-session',
    'order_id': 'example-task', 'seq': 1, 'map_id': 'example@v1',
    'order_digest': 'the-exact-wire-sha256', 'outcome': 'accepted',
}
```

이 이벤트와 `accepted`·`rejected`의 의미는 팀 **협의 전 시험 제안**이다.
외부 메시지 출처·내용을 인증하지 않으므로 장부의 `accepted`는 입력된
주장에 불과하며 실제 로봇이 수락했다는 증거가 아니다.

DB를 다시 열면 새 세대가 이전 객체를 차단하고, 이전 `pending`은
`blocked`로 바뀌며 `uncertain`은 유지된다. 이전 세대의 기록이 하나라도
있으면 새 명령을 준비하지 못한다. 이전 세대의 결과 불명 응답도 새로
확정하지 않으며, 이미 기록한 동일 응답의 중복 조회만 허용한다. 재동기화·
수동 해소·실물 사용 절차는 구현하지 않았다. 장부 데이터와 스키마, 주문
바이트/digest, 응답 관계를 각 트랜잭션에서 점검하지만, 유효한 내용으로
변조한 DB의 진위나 외부 로봇의 실제 상태를 인증하지 못한다.

테스트는 실제 장비 대신 임시 SQLite 파일과 합성 응답을 사용한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python -m unittest server.tests.test_proposed_command_journal -v
```
