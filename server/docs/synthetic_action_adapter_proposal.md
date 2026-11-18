# 합성 시연의 출처와 동작 경계

## 현재 구현

기존 Unreal 시연이 기본값이다. 명시적 합성 모드에서는 새 디렉터리와
합성 버전의 주문·pickup DB를 만들고, `DemoActionSession`의 synthetic
owner가 프로세스 내부 합성 세계와 통신한다. 기존 WebSocket은 Unreal
출처로만 owner를 요청하므로 synthetic 세션을 차지할 수 없다. 새 세계와
실행 방법, 종료·재시작 제한은 [합성 웹 주문 시연](synthetic_web_order_demo.md)을
참고한다. 저장소 버전과 출처 표식은 실수로 다른 형식의 DB를 여는 일을
막기 위한 것이며 장치 인증이나 위변조 방지가 아니다.

합성 peer는 실제 웹 주문, 격리 MQTT 가상 로봇, 기존 `DemoActionSession`,
`PickupInventory` 및 선반 확인 경로를 사용한다. 세계의 재고·슬롯·운반
상태는 peer 메모리에 별도로 있다. 결과가 불명하면 false로 변환해 예약을
해제하지 않는다. 기존 세계를 재구성할 수 없으므로 합성 DB 디렉터리의
재시작을 금지한다. 이는 실제 UE 또는 실물 로봇 동작을 증명하지 않는다.

## 아직 연결되지 않은 제안

- `SyntheticActionDbPair`의 별도 식별·검사 경계와
  `SyntheticActionSession`의 두 DB pickup 시도·결과 트랜잭션은 위 웹
  모드에 연결되지 않았다. 웹 모드는 기존 pickup 콜백 경로만 사용한다.
  별도 실험 세션의 generation과 기존 pickup 세션 토큰이 같은 DB 필드를
  차지하고, 두 경로의 준비·결과 처리를 함께 호출하면 이중 전이가 되므로
  세션과 콜백을 단순 중첩해서는 안 된다.
- 현재 합성 웹 모드의 주문 DB, pickup DB, allocation DB, MQTT 및 메모리
  세계는 원자적인 하나의 트랜잭션이 아니다. 동작 결과 유실·종료 시
  미확정 예약과 점유를 보존하고 저장 디렉터리 재시작을 차단한다.
- 팀 맵 합성 peer는 명시적인 가상 맵 대응과 가상 대기점만 사용한다.
  팀 로봇의 명령 수신 확인, 실제 위치와 정지, 집기·적재 결과의 인증 및
  물리 안전은 로봇 팀과의 프로토콜 합의와 실물 검증이 필요하다.

추가 종단 검증과 현재 시험 범위는
[합성 웹 주문 종단 시험](synthetic_action_end_to_end_test_plan.md)을 참고한다.
