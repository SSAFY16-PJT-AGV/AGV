# 가상 주문 접수 응답 유실과 재조회

로컬 가상 주문 화면은 POST 전에 원본 멱등 키와 표시용 정보를 브라우저
`localStorage`에 먼저 기록한다. 응답이 유실되거나 화면을 새로고침하면 같은
키를 HTTP 헤더에 넣어 `GET /order-demo/api/submission-lookup`으로 조회한다.
조회는 읽기 전용이며 새 주문이나 MQTT 명령을 보내지 않는다. 키는 URL이
아닌 `Idempotency-Key` 헤더로 전달하고 응답은 `Cache-Control: no-store`다.

원래 요청이 서버에서 접수 또는 명확히 거절된 것으로 확인되면 해당 브라우저
기록을 지운다. `unknown`, 조회 실패, 404 또는 저장소 오류는 주문이 없다는
증거가 아니므로 표시를 유지하고 새 주문을 차단한다. 기록을 저장할 수 없는
브라우저에서는 POST 자체를 시작하지 않는다. 가상 구간 점유·예약을 임의로
해제하거나 불명 요청을 다시 발행하는 기능은 없다.

영속 주문 모드는 기존 SQLite 요청 키를 읽어 서버 재시작 후에도 원래 요청을
찾는다. 이미 시작한 가상 작업의 결과가 불명인 경우 기존 `unknown` 및
복구 차단을 유지한다. 비영속 모드는 서버 재시작 뒤 키 기록이 사라질 수
있고, 이때 404는 결과 불명으로 남는다. 같은 출처의 다른 탭이 기록한 요청도
자동 조회하지만 명확한 서버 결과가 없으면 안전한 미수신으로 추정하지 않는다.

브라우저 저장은 같은 브라우저·같은 출처에 한정된다. 사용자가 저장 데이터를
삭제하거나 다른 기기를 쓰면 식별자가 복원되지 않는다. 서버가 기록하기 전
응답이 유실된 경우에도 이 화면만으로 미수신을 증명할 수 없다. 운영자는 기존
주문 및 점유 진단을 확인해야 하며, 이 기능은 실물 로봇의 현재 위치·정지·
후단 이탈이나 물리 주행 안전의 증거가 아니다.

```powershell
.\.venv\Scripts\python.exe -m unittest server.tests.test_submission_key_recovery `
  server.tests.test_order_submission_refresh server.tests.test_order_demo_ui `
  server.tests.test_order_dashboard_status -v
```
