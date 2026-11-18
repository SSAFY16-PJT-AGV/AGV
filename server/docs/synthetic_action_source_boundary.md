# 가상 동작 출처의 기초 경계

기존 Unreal 시연용 `PersistentOrderStore`, `PickupInventory`,
`DemoActionSession`은 기본 `action_source='unreal'`을 사용한다. 기존
Unreal 주문 DB의 `user_version=2`, pickup DB의 `user_version=1`과 다른
저장 형식은 바꾸지 않았다. `action_source='synthetic'`을 명시하면 새
주문 DB는 버전 3, 새 pickup DB는 버전 2를 사용하고, 각 출처의 코드는
상대 출처 DB를 거부한다. 이전 서버도 기존에 기대한 버전과 다르므로 새
합성 DB를 정상 DB로 열지 못한다. 저장된 이전 Unreal world를 합성 세계로
변환하거나 기존 파일을 초기화하지 않는다.

두 저장소는 연결할 때뿐 아니라 쓰기 트랜잭션 `BEGIN IMMEDIATE` 이후에도
DB의 application ID와 출처별 버전을 확인한다. 읽기 및 재열기 검증은 읽기
트랜잭션 안에서 같은 표식을 확인하고 같은 스냅샷의 내용을 읽는다. 읽기
스냅샷 시작 뒤 다른 연결이 변경한 내용은 해당 읽기에 반영되지 않을 수 있다.
pickup DB는 기존 creation ID 및 파일 식별자 확인도 유지한다. 이는 실수로
다른 형식의 DB를 열지 않기 위한 검사이며 DB 위변조, 과거 백업 롤백이나
파일 교체의 모든 경합을 방지하는 인증 수단은 아니다. 각 DB는 서로 다른
트랜잭션을 사용한다. 두 DB의 쌍·world 결합과 실행기 startup은 아직 이
변경으로 연결되지 않는다.

세션은 생성 시 고정한 출처만 `attach`에서 받아들이고, 명시하지 않은
호출은 기존과 같이 Unreal로 취급한다. 예컨대 synthetic 세션은 일반 UE
WebSocket의 기존 attach 호출을 거부한다. `source`는 **신뢰된 서버 내부
호출자**가 정해야 하며 외부 요청 본문이나 peer가 출처를 주장하게 하는
인증 수단이 아니다. attach 이후에는 기존 owner 객체·action 상관관계,
타임아웃, lock과 motion fence가 그대로 적용된다.

합성 결과 peer, 웹 주문·MQTT 실행 연결, DB 쌍 결합, 합성 결과의 장애
처리는 아직 구현되지 않았다. 예제 단위 시험은 기존과 같이 단절 시
`on_detach`로 pickup 세션을 종료하도록 **직접 연결**하여 inflight 예약이
unknown으로 보존되는지를 검사한다. `applied=False`는 미실행이 확실할 때의
기존 UE 의미이며, 단절이나 불명 결과를 false로 처리하지 않는다. 실제
로봇 ACK, 물리 재고, 장치 신원, 위치·정지 또는 운행 승인과는 무관하다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest server.tests.test_demo_action_source_isolation -v
```
