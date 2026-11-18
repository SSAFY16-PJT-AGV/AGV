# 제안 명령 장부의 격리 MQTT 통합 시험

`server.tests.test_proposed_journal_mqtt_integration`은 시험이 직접 시작한
`127.0.0.1` 전용 임시 aMQTT 브로커, 새 SQLite 장부와 가짜 `sim-r1`을
사용한다. 실제 로봇, 공유 브로커, 기존 주문·예약 DB에는 접속하지 않는다.
가짜 응답 토픽과 `accepted`/`rejected` 본문은 **팀 협의 전 시험 제안**이며
현재 로봇 구현이나 확정된 실물 MQTT 규격이 아니다.

테스트는 기존 시뮬레이션용 `OrderPublisher`로 여섯 필드 주문을 보내고,
발행 예정 바이트를 장부의 바이트와 비교해 `uncertain`으로 커밋한 뒤
그 반환 바이트에서 독립된 주문을 복원해 전송한다. 불일치한 본문은 발행하지
않고, 발행 도중 원래 입력 객체를 바꾸어도 전송 내용이 달라지지 않는다.
가짜 로봇이 수신한
바이트의 digest가 장부에 기록한 것과 일치하는지 확인한다. 가짜 로봇의
응답을 MQTT로 받아 같은 이벤트의 중복을 무변경으로 처리하고, 같은 이벤트
ID에 다른 결과가 오면 거부하는 것도 확인한다.

별도 시험에서는 브로커의 주문 PUBACK을 받은 뒤에도 가짜 로봇이 응답하지
않으면 장부가 `uncertain`으로 남음을 확인한다. 별도 Python 프로세스에서
장부를 재열고 새 MQTT 클라이언트를 만들었을 때, **시험 코드의** 발행 전 `mark_attempted` 검사에
재발행이 차단되는지와 가짜 로봇의 수신 큐에 추가 주문이 없는지 확인한다.
빈 큐를 관찰하기 전과 후에 별도 토픽의 표식을 수신해 가짜 로봇의 관찰
경로가 살아 있음을 확인한다. 가짜 로봇 수신 큐가 1초 동안 비어 있다는
결과는 그 시험 시간 동안의 관찰이며, 이후 전달이나 다른 경로의 발행까지
없다는 증명은 아니다.
이는 해당 테스트 하네스의 프로세스 교체 시험이며 실제 서버 런타임 재시작
통합을 뜻하지 않는다.

이 연결은 시험 하네스에만 있다. 장부 자체는 MQTT 발행이나 다른 코드의
직접 발행·재연결을 막지 않으며, PUBACK은 실제 로봇 수신·실행 증거가 아니다.
이 시험은 `SimRuntime`이나 그 예약 DB를 장부와 연결하지 않으므로 서로 다른
DB의 예약·명령 변경이 원자적이라는 뜻이 아니다. 물리 정지·점유·후단 이탈,
실물 인증과 재시작 복구도 검증하지 않는다. 본 시험을 실제 장비에 적용하지
않는다.

저장소 루트에서 별도 aMQTT 의존성 경로와 쓰기 가능한 임시 경로를 지정한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv\Scripts\python -m unittest server.tests.test_proposed_journal_mqtt_integration -v
```

`AMQTT_TEST_PATH`가 없으면 테스트는 생략된다. 루프백 소켓이나 자식 프로세스
생성이 제한된 환경에서는 실행 권한이 필요하다.
