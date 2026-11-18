# 웹 주문용 내부 합성 세계

이 기능은 **가상 시연 전용**이다. Unreal 프로세스나 실물 로봇에 연결하지
않으며 물리 위치·재고·동작·정지 또는 로봇 인증을 증명하지 않는다. 기존
Unreal 시연이 기본값이고 이 모드는 명시적으로 선택할 때만 켜진다.

기존 Unreal 좌표 형식의 맵 JSON과 별도로 설치한 aMQTT 의존성을 준비한다.
맵에는 기존 fleet 시연의 `J01`, `J05`, 상품 노드가 있어야 한다. `--state-dir`은
부모 폴더는 이미 존재하고 마지막 폴더는 **존재하지 않는 새 경로**여야 한다.

```powershell
$map = 'C:\경로\Content\Data\warehouse_map.json'
$newState = 'D:\관통\synthetic-run-01'
.\.venv\Scripts\python.exe -m server.tools.order_demo --map $map --synthetic --state-dir $newState
```

실행기는 로컬 전용 브로커와 두 가상 로봇, 웹 주문 서버 및 프로세스 내부
합성 세계를 시작한다. 합성 세계는 상품 종류별 초기 수량 3개와 빈 6칸
선반, 두 로봇의 운반 여부를 자체 메모리에 유지한다. 기존 HTTP 주문과
예약·격리 MQTT 주행·`DemoActionSession` 및 pickup/선반 확인 경로를
그대로 사용한다. 합성 owner의 동작 결과는 이 합성 세계에만 적용된다.
기존 상태 API의 `unreal_connected` 필드는 호환성을 위한 과거 이름이며,
합성 모드에서는 내부 합성 owner 연결을 뜻한다.

기존 Unreal WebSocket의 owner는 합성 owner를 차지하거나 동작 결과를
제출할 수 없고, 합성 주문·pickup DB의 출처별 버전은 기존 Unreal 모드가
거부한다. 소스 표식은 내부 경로를 구분하기 위한 것이며 장치 인증이나 DB
위변조 방지가 아니다. Unreal `--editor`는 합성 모드와 함께 사용할 수 없다.

팀 맵 사본을 사용하는 경우 기존에 검증한 가상 대응 파일을 명시할 수 있다.

```powershell
$map = 'C:\경로\Content\Data\warehouse_map.json'
$newState = 'D:\관통\synthetic-field-run-01'
.\.venv\Scripts\python.exe -m server.tools.order_demo --map $map --synthetic `
  --field-config server\examples\field_unreal_demo.json --state-dir $newState
```

`--field-config`의 맵 해시·두 좌표계 기준점·시작 노드와 진입 간선·상품
대응을 먼저 검증한다. 합성 세계의 위치·방향은 이 대응의 변환을 사용하고,
세계의 pickup ID를 서버 팀 맵의 ID로 변환해 재고를 확인한다. 가상 executor의
초기 yaw는 0°이고 진입 간선은 경로 계산의 명시적 입력이지 물리 방향 측정이
아니다. 예제는 가상 대기점과 빨강 상품 한 종류, D01/D04 두 점의 임의
대응을 사용한다. 실제 대기점·상품 위치·지형 정합이나 물리 안전의 근거가
아니다. 격리 종단 시험에서는 세계 노드에 별도 `U_` 접두사를 붙여 실제
서버 노드 ID와 변환이 필요한 경우를 검사한다.

합성 동작 직전에 MQTT 위치 관측이 1초보다 오래되었거나 목표 좌표와 맞지
않으면 잠금을 풀고 새로운 관측을 기다린다. 원본 수신 시각을 임의로 바꾸지
않으며, 다음 시도마다 같은 owner·작업·연결 및 기존 동작의 원래 10초 기한을
다시 확인한다. 기한 만료, 연결 상실 또는 종료 때는 효과를 만들지 않고 불명
상태로 보존한다. 이 시간과 좌표 확인은 합성 시연의 조건일 뿐 실물 측정이
아니다.

프로세스가 종료되면 합성 세계 자체가 사라지므로 같은 상태 폴더로는 완료
여부와 무관하게 자동 재시작하지 않는다. 강제 종료 중 저장된 `inflight`는
결과 불명으로 해석해야 한다. 관측된 결과가 없는 집기·적재의 예약과 점유는
자동 해제하거나 다시 발행하지 않는다. 새 시연이 필요하면 기존 증거를
보존한 채 다른 새 경로를 지정한다. 메모리 세계와 주문·pickup·allocation
DB 및 MQTT는 단일 트랜잭션이 아니다. 별도 실험의
`SyntheticActionSession`/DB 쌍 원자적 pickup 경계도 이 모드에 연결되지
않는다. 종료 또는 owner 무효화가 반영된 후에는 새 합성 동작을 시작하지
않으며, 이미 시작한 메모리 효과는 종료가 반환되기 전에 끝날 수 있다.
MQTT 단절 콜백보다 먼저 발생한 단절이나 검사 직후의 경합을 물리적 정지로
해석해서는 안 된다. 종료는 진행 중인 peer 효과와 세션 콜백을 기다리므로
콜백이 오래 막히면 종료도 지연될 수 있다.

검증은 격리 aMQTT가 포함된 환경에서 실행한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:PYTHON_DOTENV_DISABLED = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_synthetic_peer server.tests.test_synthetic_peer_web_integration -v
.\.venv\Scripts\python.exe -m unittest server.tests.test_synthetic_peer_field server.tests.test_synthetic_field_web_integration -v
```
