# 팀 맵 가상 경합 중 지연 이벤트와 런타임 재시작 시험

`server.tests.test_field_contention_restart_mqtt`는 저장소에 포함된 팀 맵
사본에서 세 가상 대기점을 선택하고, 두 가상 AGV와 별도로 기동한 루프백
aMQTT 브로커를 사용한다. 다른 프로세스의 서버가 아니라 한 Python 프로세스에서
`SimRuntime` 인스턴스를 중지하고 같은 DB로 새 인스턴스를 여는 시험이다.

1. 두 로봇이 교차 구역을 경합할 때 첫 로봇의 반복 방문 이벤트를 대기시킨다.
2. 두 번째 로봇의 정지 이벤트를 전송하지 않아 유실 상황을 모의하고 첫 이벤트의
   재시도도 점유 충돌로 보류되는지 확인한다.
3. 별도 시험용 MQTT 발행기로 첫 이벤트를 중복 전달해도 작업·점유가 바뀌지
   않는지 확인한다.
4. 런타임을 재생성한 뒤 지연된 두 번째 이벤트를 전송한다. 이전 가상 정지
   확인이 무효화되어 두 이벤트가 자동 적용되지 않고 작업·점유가 보존되는지
   확인한다.

이 이벤트의 위치·방향·정지와 후단 이탈은 시험 가정이며 실제 로봇의 관측,
물리 안전 또는 서버 프로세스 강제 종료 시험이 아니다. PUBACK은 구독자 처리
증거가 아니다. 의도된 점유 충돌과 복구 차단으로 오류 로그가 나올 수 있다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:PYTHON_DOTENV_DISABLED = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_field_contention_restart_mqtt -v
```
