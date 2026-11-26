# HTTP 로봇 관측 조회 제안 (팀 협의 필요)

설계 문서 v0.2는 HTTP API 경로나 응답 규격을 정의하지 않는다. 다음은 FastAPI의 읽기 전용 조회를 위한 **추가 제안**이며, MQTT 토픽이나 로봇 상태 메시지 필드를 변경하지 않는다. 현재 Unreal 시연의 상태 전송은 WebSocket을 사용한다.

## `GET /robots`

현재 서버가 수신한 MQTT 관측의 복사본을 반환한다. 예시의 값은 실제 로봇 정보가 아니다.

```json
{
  "connected": true,
  "ready": true,
  "robots": {
    "robot1": {
      "state": {"last_node": "N1", "status": "IDLE"},
      "state_received_live": false,
      "connection_report": "online",
      "connection_received_live": false
    }
  }
}
```

- `connected`는 **서버와 MQTT 브로커 사이의 연결**을 나타내며, 로봇의 연결 여부가 아니다.
- `ready`는 서버의 두 구독 요청에 대한 브로커 응답을 나타낸다. 이것만으로 로봇의 접속이나 유효한 관측을 보장하지 않는다.
- `robots`의 각 항목은 마지막으로 받은 상태 및 연결 보고다. 값이 없을 수 있고, MQTT가 끊어진 뒤에도 이전 값이 남을 수 있다.
- `state_received_live`, `connection_received_live`는 해당 메시지가 이 서버에 전달될 때 MQTT의 RETAIN 플래그가 없었는지를 나타낸다. **현재 신선함이나 온라인 상태를 뜻하지 않으며**, 이후 시간이 흘러도 자동으로 만료되지 않는다. `connection_report`도 과거 보고일 수 있다.
- MQTT 3.1.1에서는 이미 구독 중에 발행된 retained 메시지가 RETAIN 플래그 없이 전달될 수 있다. 따라서 위 플래그로 발행자의 retain 사용 여부를 판별할 수 없다.
- `200 OK`는 관측 복사본을 반환했다는 의미다. `connected` 또는 `ready`가 `false`일 때도 과거 관측이 반환될 수 있다. **주행 허가, 위치 확인, 충돌 방지 또는 작업 배정에 이 응답만 사용하면 안 된다.**
- MQTT 서비스가 없거나 관측을 읽을 수 없으면 내부 오류나 접속 정보를 포함하지 않는 `503 Service Unavailable`을 반환한다.
- 응답에는 중간 캐시가 오래된 관측을 재사용하지 않도록 `Cache-Control: no-store`를 설정한다.

세 로봇 관측 API는 직접 루프백 요청과 로컬 Host만 허용하고 전달 프록시 헤더를 거부한다. `AGV_OBSERVATION_API_TOKEN`을 설정하면 로컬 요청에도 Bearer 토큰이 필요하며 잘못된 설정은 조회를 차단한다. 토큰이 없으면 로컬 요청에는 인증을 요구하지 않는다. `--host 127.0.0.1 --no-proxy-headers`로 실행하고 프록시 뒤에 두지 않는다. 같은 PC의 다른 프로세스나 사용자로부터의 격리, 원격 서비스용 TLS·인가를 보장하지 않는다. 물리 안전, 최신성, 장치 신원과 작업 허가는 별개다.

## `GET /robots/battery`

운영 화면에서 미측정값을 0%와 구분할 수 있도록 동일한 MQTT 관측을 요약한다.
기존 `/robots` 응답과 Unreal WebSocket의 메시지 형식은 바꾸지 않는다. 팀 로봇
문서는 0~1 비율을 제안하지만 단위와 범위가 확정된 계약은 아니다. 아래 값들은
수신한 상태에 관한 설명이지 측정 시각이나 실제 배터리 상태를 인증하지 않는다.

```json
{
  "connected": true,
  "ready": true,
  "robots": {
    "robot1": {"status": "unknown", "value": null, "ratio_range": null,
      "state_received_live": false, "connection_report": "online",
      "connection_received_live": false},
    "robot2": {"status": "reported", "value": 0.75, "ratio_range": true,
      "state_received_live": true, "connection_report": null,
      "connection_received_live": false}
  }
}
```

- `reported`: 유한한 숫자를 받았다. 값은 해석이나 단위 변환 없이 그대로 둔다.
  `ratio_range`는 값이 제안된 0~1 범위에 있으면 true, 밖이면 false다. 범위 안에
  있어도 실제 단위가 비율이라는 뜻은 아니다. 0은 미측정이 아닌 보고된 수치다.
- `unknown`: 상태에서 `battery: null`을 받았다.
- `not_reported`: 상태는 받았지만 `battery` 필드가 없었다.
- `no_state`: 연결 보고만 받았고 상태는 아직 받지 않았다.
- `invalid`: 숫자가 아니거나 유한하지 않은 값이었다. `value`는 null로 반환한다.

`reported`가 아닌 상태의 `ratio_range`는 null이다. `connection_report`와
`connection_received_live`는 해당 로봇의 마지막 연결 보고와 그 전달 형식이다.

`state_received_live`, `connection_report`, `connection_received_live`, `connected`,
`ready`는 `/robots`와 같은 관측 의미다.
보관 메시지나 연결이 끊기기 전의 값도 반환될 수 있으며 현재 신선도, 로봇
신원, 주행 가능 여부의 증거는 아니다. 현재 관측이 없는 로봇은 목록에 없으며
보고된 `value`도 실제 센서 측정을 보장하지 않는다. API 오류는 503이고 응답에는
`Cache-Control: no-store`가 적용된다. 위의 로컬 접근 제한을 적용한다.
Unreal에서의 미측정 표시는 클라이언트와 별도 협의가
필요하며 이 API 추가로 해결되는 것은 아니다.

## `GET /robots/diagnostics`

기존 두 응답 형식은 그대로 두고, MQTT 관측기의 **서버 수신 처리 경과시간**과
입력 거부 건수를 별도로 조회한다. 상태·연결 메시지 각각 마지막으로 유효한
메시지를 처리한 단조 시계 시점부터 조회 시점까지의 초가 `age_seconds`다.
아직 받지 않았거나 시계가 역행하는 등 계산할 수 없으면 `null`이다. 재시작
전의 시각은 보존·비교하지 않는다. 수신 본문과 배터리값은 반환하지 않지만,
`robots`에는 마지막으로 관측한 **로봇 ID**가 표시된다.

```json
{
  "connected": true,
  "ready": true,
  "robots": {
    "robot1": {
      "state": {"age_seconds": 2.4, "last_delivery": "non_retained"},
      "connection": {"age_seconds": null, "last_delivery": "not_received"}
    }
  },
  "dropped": {"invalid_topic": 0, "invalid_size": 0, "invalid_payload": 1,
              "robot_limit": 0, "disconnected": 0}
}
```

`last_delivery`는 이 서버가 마지막으로 받은 메시지의 RETAIN 플래그에 따라
`retained` 또는 `non_retained`이며, 받지 않았으면 `not_received`다. 단절
후에도 마지막 전달 종류와 경과시간은 남고 최상위 `connected`는 false다.
`non_retained`는 원래 발행자가 retain을 설정하지 않았다는 증거가 아니다.
경과시간은 서버가 수신한 뒤 흐른 시간일 뿐 원본 생성 시각·실제 장치의
신선도·장치 신원·연결 상태·물리 정지나 운행 허가를 증명하지 않는다.
오래된 상태의 수신이나 MQTT 연결만으로 주행을 허용하지 않는다.

`dropped`는 수신 콜백이 거부한 메시지를 고정된 다섯 범주로 집계한다.
토픽 구조 오류, ID 또는 payload 크기 초과, 내용/형식 오류, 단절된 서비스로
도착한 유효 메시지, 새 로봇 ID 수 상한 초과 순서로 판정한다. 각 건은
한 범주에만 더하며 본문·토픽·ID별 오류 내용은 저장하거나 내보내지 않는다.
카운터는 각각 2,147,483,647에서 포화되고 프로세스에서만 유지된다.
브로커에 도달하지 않았거나 구독에 전달되지 않은 메시지는 집계하지 못한다.
로봇 수는 기존 관측기 상한인 128개이며, 누락된 로봇의 존재는 확인하지 않는다.
거부된 메시지는 마지막 유효 수신 시각을 갱신하지 않는다.

기존 `/robots`와 같은 로컬 접근 제한을 적용한다. 내부 오류에는 일반 503,
모든 응답에는 `Cache-Control: no-store`를
사용한다. 테스트 소유 루프백 브로커에서만 실행하는 회귀:

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv\Scripts\python.exe -m unittest server.tests.test_mqtt_observation_diagnostics server.tests.test_mqtt_observation_diagnostics_integration -v
```
