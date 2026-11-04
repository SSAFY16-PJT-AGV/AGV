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
| POST | `/simulation/tasks/{task_id}/prepare` | `pickup_node`, `candidates` (`robot_id`, `node`, `available` 목록), `action` | 시뮬 작업 배정·첫 구간 예약 |
| POST | `/simulation/tasks/{task_id}/publish` | 없음 | 준비된 첫 주문의 일회 발행 |
| POST | `/simulation/recovery/{task_id}/begin` | `session` | 새 세션의 일회용 재동기화 challenge 발급 |
| POST | `/simulation/recovery/{task_id}/complete` | `session`, `challenge`, `robot_id`, `order_id`, `seq`, `map_id`, `stopped_at`, `order_digest`, `stopped` | challenge와 저장된 주문 정보 대조 |
| POST | `/simulation/events/{robot_id}/{event_id}/retry` | 없음 | 저장된 후단 이탈 이벤트 명시적 재처리 |
| POST | `/simulation/orders/{task_id}/{seq}/submit` | `session` | 복구 확인 뒤 대기 중인 후속 주문을 명시적 제출 |

HTTP가 응답했다고 로봇이 주문을 받거나 수행했다는 뜻은 아니다. 재동기화
요청 값은 실제 위치나 정지 여부를 입증하지 않는다. 서버 재시작 후 기존 점유는
보존하며 새 확인 전에는 발행을 허용하지 않는다. 미확정 주문은 자동 재발행하지
않는다. MQTT 브로커 설정과 운영 보안은 별도로 정해야 한다.
