# 재시작 재동기화 메시지 제안: 격리 시뮬레이션 전용

설계 v0.2에는 재동기화 challenge와 응답의 상세 규격이 없다. 아래 필드와
절차는 **팀 협의 전 제안**이며, 서버와 로봇 사이의 확정된 MQTT 메시지가
아니다. 아직 MQTT·FastAPI에서 이 함수를 호출하거나 메시지를 발행하지 않는다.

## 제안 메시지

서버는 `task_id`와 현재 보고된 `robot_session`에 대해 임의의 한 번만 사용할
수 있는 `challenge`를 발급한다. 요청 대상 로봇은 저장된 배정 정보로 확인한다.
가상 로봇이 반환할 응답은 다음 필드로 제안한다.

```json
{
  "challenge": "예시-일회용-challenge",
  "robot_id": "sim-example",
  "robot_session": "예시-부팅-세션-ID",
  "stopped": true,
  "stopped_at": "N3",
  "order_id": "예시-작업-ID",
  "seq": 1,
  "map_id": "예시맵@v1",
  "order_digest": "주문 JSON 정규 표현의 SHA-256 소문자 hex"
}
```

`order_digest`는 주문 객체를 `json.dumps(order, sort_keys=True, ensure_ascii=True,
allow_nan=False, separators=(',', ':')).encode('ascii')`로 변환한 바이트의
SHA-256이며, 원본 JSON 문자열의 공백이나 키 순서는 영향을 주지 않는다.
`stopped_at`은 해당 주문의 `released_until`과 일치해야 한다. `seq`와 digest는
서버의 주문 이력에서 확인하며, 브로커 수신 확인은 로봇 실행 증거가 아니다.
`pending`은 발행되지 않은 주문이므로 로봇이 적용한 주문으로 취급하지 않는다.
이 응답의 실제 토픽, 인증·인가, 재부팅 후 물리 상태 확인 방법과 미수신·부분
수행 시 복구는 팀에서 정해야 한다.

## 구현 안전 경계

게이트 인스턴스 생성 후에는 이전 인증 상태를 사용하지 않는다. 새 challenge와
현재 로봇 세션의 응답이 일치한 작업만 해당 인스턴스의 재동기화 관측으로
기록한다. challenge는 실패한 응답도 한 번 소모하며, 이전 challenge의 재사용,
다른 세션, 다른 맵·작업·노드·주문 순번·digest는 거부한다. Challenge와 증명의
유효 시간은 임시 제한이며 팀과 협의해야 한다. 새 challenge를 발급하면 이전
관측은 무효다. 새 challenge 발급이 실패해도 이전 증명은 차단된다.

`require_reconciled`는 `with gate.transaction() as db:`가 만든 동일 DB 연결과
스레드 안에서만 호출할 수 있다. 이 컨텍스트는 gate 락을 먼저 잡고 SQLite
`BEGIN IMMEDIATE`를 시작하며, 작업 변경과 재동기화 확인 후 COMMIT 또는
ROLLBACK이 끝날 때까지 락을 유지한다. `invalidate`, 새 challenge, 완료 처리도
같은 락을 사용하므로 한 gate 객체 안에서는 연결 끊김 처리와 진행 거래의
순서가 선형화된다. SQLite 락을 먼저 잡고 gate 락을 잡는 호출은 금지한다.
단절이 실제로 발생했지만 아직 서버가 감지하지 못한 시간 동안 물리적 정지를
보장하는 것은 아니다. 여러 프로세스·원시 SQLite 접근·물리 장치 제어는 이
락으로 보호되지 않는다.

후속 주문의 발행 시도 상태를 DB에 커밋한 직후 Paho 큐에 넣을 때는
`with gate.serialized():` 안에서 `gate.transaction()`으로 시도를 기록한 뒤,
트랜잭션이 끝난 다음 Paho `publish`를 호출한다. 두 동작 사이 같은 gate의
`invalidate`가 끼어들지 않도록 락을 잡는다. `serialized()`는 DB 트랜잭션
안에서 중첩 사용할 수 없다. 큐 등록 후에는 락을 풀고 PUBACK을 기다린다.
이는 이 프로세스에서 관찰한 이벤트 순서에 관한 보장일 뿐, 실제 단절 시점을
정확히 알거나 기계가 즉시 멈춘다는 물리적 보장이 아니다.

게이트는 증명된 순번의 영속 주문 digest, 맵 fingerprint와 정지 노드 점유를
거래 안에서 다시 확인한다. 호출자는 그 밖에 전체 점유 집합·작업·후속 주문을
같은 거래에서 검증해야 한다. 새 구간으로 이동한 뒤의 이벤트는 새 순번과 정지
노드를 담은 새로운 challenge 확인이 필요하다. 이벤트 수신 측은 단절이나
부팅 세션 변경을 감지하면 `invalidate(task_id)`를 호출해야 한다.

여러 작업이 동시에 영향을 받는 브로커 단절 등에는
`invalidate_many(task_ids)`를 한 번 호출한다. 인자는 비어 있지 않은
list, tuple, set 또는 frozenset이며, 유효한 작업 ID만 담고 중복은 허용하지
않는다. 검증에 실패하면 어떤 작업도 차단하지 않는다. 검증에 성공하면 같은
gate의 락 아래에서 모든 작업을 메모리에서 먼저 차단하고 challenge 세대를
무효화한 뒤, SQLite 트랜잭션 하나로 모든 증명을 revoke한다. DB 오류가 나면
오류를 호출자에게 전달하고 이 gate에서는 모든 작업을 계속 차단한다. 단,
DB 반영이 실패한 경우 그 메모리 차단은 다른 gate나 프로세스에 전파되지
않으므로 이 기능만으로 다중 프로세스의 단절 처리를 보장할 수 없다.
`serialized()` 내부 호출은 허용한다. 이미 `transaction()` 내부라면 모든
작업을 이 gate에서 차단한 뒤 오류를 내며 DB 업데이트는 하지 않는다.

이 모듈은 점유를 해제하거나 주문을 발행하지 않는다. `sim-` ID와 문자열 비교는
메시지 인증이나 물리적 정지·후단 이탈을 증명하지 않는다. 반드시 별도 격리된
가상 로봇에서만 사용하며 실제 장비 운행에는 사용할 수 없다. 스냅샷 조회나
브로커의 online/retained 상태만으로 현재 위치와 정지를 증명하지 못한다.
