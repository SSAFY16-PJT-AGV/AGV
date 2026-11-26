# 6선반 합성 관측과 보충 작업 시험

`server.simulated_shelf_perception`은 팀 #61의 **제안된** 선반 인식 출력
형식을 이용한 격리 시뮬레이션이다. 카메라·실물 로봇·공용 브로커에
연결하지 않으며 실제 재고, 위치, 정지 및 안전을 증명하지 않는다. 메시지의
출처 인증이나 물리적인 관측 시각 검증도 제공하지 않는다.

팀 24칸 합성 장부의 활성 세션에서 `SimulatedShelfPerception`을 만들고,
매 관측 사이클 전에 `begin_capture()`를 호출한다. 합성 생산자는 반환된
임시 토큰으로 N01~N06 각각의 프레임을 같은 사이클과 유효 시간 내에
`accept_frame()`에 전달한다. 이 토큰은 같은 프로세스의 관측 연결·재전송
구분용이지 장치 인증 수단이 아니다. 6개 선반을 모두 받은 시점에 장부의
fence가 일치하는 경우만 한 번에 갱신한다. 도중에 슬롯 예약이나 완료
등의 쓰기가 발생하면 해당 관측을 거부하고 새 캡처가 필요하다. 수집이
시작되면 이전 관측으로 새로운 예약이나 동작을 시작하지 못한다. 이미
발송한 동작의 결과 확인은 계속 허용한다.

각 프레임은 `frame: robot_base`, `shelf.ok`, 해당 선반의 칸 마커 ID,
`slots`의 로컬 번호 1~4와 상태를 사용한다. 물품이 있으면 종류별 마커
30~33을 확인한다. 관측이 없는 칸이나 `shelf.ok: false`는 `unknown`이며
빈 칸으로 간주하지 않는다. 선반 마커가 다르거나 슬롯이 중복·손상된
관측은 전체 사이클을 거부한다. 같은 선반 프레임의 동일한 재전송은
한 번으로 취급하고 서로 다른 재전송은 거부한다. 완료된 사이클의 토큰은
다시 사용할 수 없다. 보류 중인 슬롯은 관측으로 확정하거나 점유를
해제하지 않는다.

`shortages(threshold=...)`는 합성 4칸이 모두 알려진 경우에만 상품별
부족을 표시한다. 불명 또는 다른 상품이 있는 선반은 각각 `unknown`,
`mismatch`이며 보충 작업을 만들지 않는다. `approve_and_dispatch(...,
approved=True)`는 명시적인 합성 승인과 시뮬레이터 후보를 요구하고,
부족 상태 확인과 픽업·목적 슬롯 선점을 직렬화한 뒤 기존
`TeamShelfSimDispatch`에서 격리 MQTT **픽업 지점까지의 주행**을 발행한다.
PUBACK이나 가상 도착은 집기·적재 성공이 아니다. 현재 이 경로는 출발
슬롯을 Unreal 동작으로 전달하지 않으며, 웹 주문·실제 비전·실물 팔과
자동으로 연결되지 않는다. 작업 결과 불명 시 두 슬롯의 선점은 유지된다.

관측 유효시간 검사는 이 어댑터를 통해 부족을 조회하거나 승인할 때
적용된다. 합성 장부에 직접 접근하는 별도 호출까지 보호하는 인증·TTL
경계는 아니므로 실제 운용 경로로 사용하지 않는다.

```powershell
$env:TEMP = (Resolve-Path .venv\pr59-test-temp).Path
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_simulated_shelf_perception `
  server.tests.test_simulated_shelf_perception_mqtt -v
```
