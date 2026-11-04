# HTTP 로봇 관측 조회 제안 (팀 협의 필요)

설계 문서 v0.2는 HTTP API 경로나 응답 규격을 정의하지 않는다. 다음은 FastAPI의 읽기 전용 조회를 위한 **추가 제안**이며, MQTT 토픽이나 로봇 상태 메시지 필드를 변경하지 않는다. 실제 트윈 연결 방식은 팀에서 별도로 정해야 한다.

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

이 API는 인증·인가 기능이 없다. 접근 정책을 정하고 구현하기 전에는 로컬 개발 환경에서만 사용해야 한다. 물리 안전 확인, 데이터 최신성 판단, 로봇 연결 확인, 구간 예약과 오류 복구는 별도 구현과 팀 합의가 필요하다.
