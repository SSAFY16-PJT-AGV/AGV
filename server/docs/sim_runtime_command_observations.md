# 격리 런타임의 가상 명령 응답 관측

`SimRuntime`은 명시적으로 활성화한 팀 맵 격리 시뮬레이션에서만
미합의 `accepted`/`rejected` 응답을 기존 예약 SQLite에 관측 기록할 수 있다.
기본값은 꺼져 있으며 기존 `rear_clear`·예약·발행·복구 처리에는 연결하지
않는다. 실물 로봇, 공유 브로커, 물리 위치·정지나 작업 수락의 증거가 아니다.

`AGV_SIM_CONFIG` JSON에서 다음 두 설정을 모두 **불리언**으로 지정한다.
나머지 필수 설정은 [기존 런타임 안내](field_runtime_simulation.md)를 따른다.

```json
{
  "field_simulation": true,
  "command_observations": true
}
```

위는 전체 실행 설정이 아닌 추가 설정 예제다. 일반 시뮬레이션에서 관측만
켜거나 숫자·문자열로 설정하면 시작 전에 거부한다. 별도 토큰으로 인증된
기존 `/simulation/status`의 `command_observation_ready`는 이 옵트인을 켠
경우에만 반환되며, 기존 `ready`와 별도 시점에 수신기 상태를 읽는다.
관측 수신기 단절·입력 오류는 그 수신기만 닫고 해당 값은 `false`가 된다.
기존 `ready`를 관측 수락 여부나 실제 로봇 연결 증거로 해석하지 않는다.
이 옵션은 `AGV_SIM_CONFIG`를 사용하는 시뮬레이션 실행기용이며, 별도의 웹
주문 데모 `order_demo_app`은 현재 이 설정을 전달하거나 활성화하지 않는다.

런타임은 첫 가상 배정 전에 기존 복구 게이트와 진행기를 초기화한 후 같은
DB에 [관측 저장소](sim_command_observations.md)를 초기화하고 별도의
[루프백 수신기](sim_command_observation_receiver.md)를 시작한다. 수신기는
`127.0.0.1`의 격리 브로커에만 연결하며 `sim-observation/{robot}/order-response`
토픽과 응답 내용은 팀이 합의한 실물 규격이 아닌 시험 제안이다. 런타임에서
읽은 MQTT 인증·TLS 설정이 시작 시점과 다르면 시작을 거부한다. 호출자는
루프백 주소나 `sim-` 이름만으로 브로커가 격리됐다고 간주하지 말고 시험
소유 브로커를 준비해야 한다. 관측만으로 다른 명령이나 점유 해제·복구를
승인하지 않으며, 기존 서버·로봇 코드가 응답을 자동 생성하지도 않는다.

관측 테이블은 생성 시 복구 게이트 세대에 묶인다. 서버를 재시작하면 새
세대에서 **같은 DB의 관측 opt-in 시작은 거부**되며 이전 세대를 자동으로
갱신하거나 이전 응답을 새 복구 증거로 사용하지 않는다. 기본 런타임으로
다시 열면 기존 작업은 원래 시뮬레이션 복구 규칙에 따라 보류된다. 별도
실험은 새 격리 DB를 사용한다. 런타임의 기존 `reconnect()`는 기존 진행
이벤트 수신기만 다시 만들고 이 수신기를 자동 재연결하지 않는다.

관측 수신 콜백은 DB 검증·기록을 동기 수행하므로 이 동안 수신기의 상태
조회와 종료가 지연될 수 있다. 다만 상태 조회가 관측 수신기 잠금을 기다릴 때
런타임 전체 잠금을 유지하지 않는다. 시작 중 한 구성 요소가 실패하면 이미
시작한 수신기와 소유 리소스를 정리한다. 이 구현은 실시간 정지, 메시지
무손실, DB 롤백 감지 또는 실물 인증을 제공하지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv\Scripts\python.exe -m unittest `
  server.tests.test_sim_runtime_command_observations `
  server.tests.test_sim_runtime_command_observations_mqtt -v
```
