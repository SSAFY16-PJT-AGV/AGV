# 합성 집기 시도 저장 경계

`SyntheticActionSession`은 **새로 만든 가상 실험 전용** 주문 DB와 pickup
DB에 한 번의 grab 시도를 저장한다. 실물 로봇, 피어, 웹 주문 런타임,
allocation DB 및 MQTT와는 연결되지 않는다. 저장된 `uncertain`은 시도가
외부로 전달됐거나 집기가 일어났다는 증거가 아니다. 현재 place와 결과
확정·재고 차감은 지원하지 않는다.

명시된 `sim-` 로봇/가상 AGV 대응을 검증한 후 독립된 새 DB 쌍과 세션
세대를 만든다. 기존 UE 경로·DB 형식은 변경하지 않는다. 호출자의 활성
가상 주문에 지정된 pickup task와 robot/AGV, 상품·pickup 예약, 선반 예약이
일치하고 가상 재고 수량이 보유 예약을 감당할 수 있을 때만 기록한다.
현재 `stage_attempt`는 실제 웹 주문을 만들지 않으며, 호출자가 저장소에
이 조건이 성립하도록 연결하는 운영 경로는 아직 없다. 테스트의 주문과
재고는 합성 fixture다. 저장된 수량과 revision은 물리적인 재고나 시의성
증명이 아니다.

시도 시 한 SQLite 연결에서 주문 DB에 pickup DB를 ATTACH하고
`BEGIN IMMEDIATE`를 사용한다. 두 DB의 source, world, pair 및 creation ID,
스키마, 파일 식별자, 관계와 journal 설정을 트랜잭션 안에서 검사하고,
pickup 예약의 `reserved → inflight`와 주문 DB의 `uncertain` attempt를
같은 트랜잭션으로 쓴다. 두 DB 모두 rollback journal(`delete`, `truncate`,
`persist`)과 FULL 이상의 synchronous를 요구하며 WAL, 예상 밖 trigger,
스키마 변경은 차단한다. 기존 action/order/AGV·예약 불일치나 중복 시도를
거부한다. 미해소 시도가 있는 task 또는 가상 AGV에는 다른 주문·action을
추가하지 않으며, 이미 저장된 시도들의 task·AGV·주문·예약 중복도 거래 안에서
차단한다. 이 단계에서는 시도 해소를 지원하지 않아 기존 불명 시도는 유지된다.
기존 시도 각각은 같은 거래에서 현재 활성 주문의 로봇·task·상품·예약과
reserved 선반, inflight pickup의 task·action·AGV·세대·revision을 다시
대조한다. 대응 시도 없이 남은 inflight 또는 unknown pickup은 고아 상태로
진행을 막는다. unknown 상태의 자동 해소는 제공하지 않는다. 아직 발송하지
않은 일반 reserved pickup은 그 자체로 고아가 아니다.
다만 reserved는 아직 발송되지 않은 상태이므로 어떤 reserved 행이든 이전
task·action·AGV·session·dispatch revision 중 하나라도 남아 있으면 전체
진행을 차단한다. 기존 값을 새 발송 정보로 덮어쓰지 않는다.
서로 다른 task와 AGV는 각각 하나씩 기록할 수 있다. 원래 재고 수량과 선반
점유는 변경하지 않는다.

DB 오류나 트랜잭션 안의 충돌·검증 실패 후에는 세션을 차단한다. 지원하지
않는 action 등 거래 시작 전 입력 오류는 거래를 만들지 않는다. 트랜잭션 도중 실패는
SQLite rollback을 시도한다. commit이 이미 끝났거나 결과가 불명인 뒤
확인에 실패하면 기록된 attempt와 예약을 되돌리거나 가용 재고로 보지
않는다. 이 기능은 불명 시도를 자동 재시도하거나 결과를 확정하지 않는다.
프로세스 종료 후 같은 DB는 세션으로 다시 열 수 없으며, 기존 pair의
읽기 전용 점검만 가능하다. 읽기 점검은 결과를 해소하지 않는다.

시험에서 실제 파일 두 개에 대해 BEGIN 후 두 writer의 경쟁 차단과, 두 DB
쓰기 사이 프로세스 강제 종료 시 rollback을 확인한다. 이는 해당 환경의
시험이며 다른 journal·파일시스템·전원 장애에서 전부 원자적으로 복구된다는
보장은 아니다. allocation DB, 외부 메시지와 합성 세계 변경도 이 커밋의
원자 범위 밖이다. OS 잠금에 협조하지 않는 파일 교체의 모든 경합, 유효한
내용의 외부 변조, 백업 롤백은 완전 차단하거나 인증하지 못한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest server.tests.test_synthetic_action_transaction -v
```
