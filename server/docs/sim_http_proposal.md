# FastAPI 격리 시뮬레이션 API 제안

설계 v0.2는 HTTP 작업 제어 규격을 정의하지 않는다. 다음 경로는 기존 구간 진행과
재동기화 구현을 시험하기 위한 **임시 제안**이며, 실제 로봇 운행을 승인하지 않는다.
팀이 메시지와 인증·인가 및 물리적 후단 이탈 확인 방법을 확정하기 전에는 실물
로봇과 공유하지 않는 독립된 시뮬레이션 브로커에서만 사용한다.

모든 요청에 `Authorization: Bearer <토큰>`이 필요하다. 토큰은 환경 설정으로
주입하며 저장소에 넣지 않는다. 이 토큰 검사는 실물 운행용 접근 제어나 로봇
인증의 대체물이 아니다. 클라이언트 소켓 주소가 `127.0.0.1` 또는 `::1`인
요청만 허용하며 전달 헤더는 신뢰하지 않는다. HTTP 서버는 격리된 로컬 개발
환경에서 사용한다.
시뮬레이션이 비활성화되거나 런타임이 없으면 503, 인증 실패 시 401, 유효하지
않은 요청 형식은 422, 작업 상태 충돌은 409를 반환한다. 오류 응답에 내부 예외,
자격 증명, 접속 정보는 담지 않는다. 응답은 `Cache-Control: no-store`를 보낸다.

| 메서드 | 경로 | JSON 입력 | 의미 |
| --- | --- | --- | --- |
| GET | `/simulation/status` | 없음 | 런타임 상태 조회 |
| POST | `/simulation/tasks/{task_id}` | 없음 | PENDING 작업 생성 |
| GET | `/simulation/tasks/{task_id}` | 없음 | 작업과 주문 상태 및 예약 조회. uncertain은 수신·실행 여부를 뜻하지 않음 |
| POST | `/simulation/tasks/{task_id}/prepare` | `pickup_node`, `candidates` (`robot_id`, `node`, `available` 목록), `action` | 시뮬 작업 배정·첫 구간 예약 |
| POST | `/simulation/tasks/{task_id}/publish` | 없음 | 준비된 첫 주문의 일회 발행 |
| POST | `/simulation/recovery/{task_id}/begin` | `session` | 새 세션의 일회용 재동기화 challenge 발급 |
| POST | `/simulation/recovery/{task_id}/complete` | `session`, `challenge`, `robot_id`, `order_id`, `seq`, `map_id`, `stopped_at`, `order_digest`, `stopped` | challenge와 저장된 주문 정보 대조 |
| POST | `/simulation/recovery/{task_id}/reset-unallocated` | `session` | 아직 배정·예약·발행 이력이 없는 PENDING 작업의 시뮬레이션 차단을 명시적으로 초기화 |
| GET | `/simulation/events/pending` | 없음 | 재처리가 필요한 저장된 시뮬레이션 이벤트 조회 |
| POST | `/simulation/events/{robot_id}/{event_id}/retry` | 없음 | 저장된 후단 이탈 이벤트 명시적 재처리 |
| POST | `/simulation/orders/{task_id}/{seq}/submit` | `session` | 복구 확인 뒤 대기 중인 후속 주문을 명시적 제출 |

HTTP가 응답했다고 로봇이 주문을 받거나 수행했다는 뜻은 아니다. 재동기화
요청 값은 실제 위치나 정지 여부를 입증하지 않는다. 서버 재시작 후 기존 점유는
보존하며 새 확인 전에는 발행을 허용하지 않는다. HTTP 요청의 세션 문자열,
challenge와 `stopped` 값은 실물 로봇 인증이나 후단 이탈 증명이 아니다.
MQTT 브로커 설정과 운영 보안은 별도로 정해야 한다.

실패한 이벤트를 `retry`로 처리해 후속 주문이 만들어져도 자동 발행하지 않는다.
저장된 후속 `pending` 주문은 새 복구 확인 후 운영자가 `submit`을 명시적으로
요청해야 한다. 첫 주문의 기존 `pending`과 발행 여부가 불확실한 `uncertain`
주문은 자동 복구·재발행하지 않는다. 예약을 유지하고 결과를 수동으로 대조해야
하며 현재 일반적인 해결 절차는 구현되지 않았다. `submit` 응답은 워커 큐에
접수했다는 의미이며 발행이나 로봇 실행 완료를 뜻하지 않는다.

대기 이벤트 목록은 최대 100개이며 더 있으면 `truncated`가 참이다. 현재
페이지 이동 기능은 없으므로 순서대로 처리한 뒤 다시 조회해야 한다. 작업 조회의
주문과 점유 목록도 최대 100개와 500개이며 각각 잘림 여부를 반환한다.
하나의 DB는 하나의 시뮬레이션 런타임만 사용할 수 있다. Uvicorn 다중 워커나
여러 서버 인스턴스에서 같은 DB를 열지 않는다. 시뮬레이션 미설정이 기본값이며,
설정 시에도 독립된 루프백 브로커와 가상 로봇만 사용한다.

`reset-unallocated`는 운영자가 가상 로봇의 세션, 초기 위치와 가용성을 직접
확인한 경우에만 호출한다. 브로커의 구독 준비나 이 호출 자체가 로봇의 온라인
상태 또는 안전을 증명하지 않는다. `/status`의 `last_workflow_error`는 최근 고정
오류 분류의 이력이며 이후 성공해도 남을 수 있다. 현재 주문 상태는 작업 조회에서
확인한다.
