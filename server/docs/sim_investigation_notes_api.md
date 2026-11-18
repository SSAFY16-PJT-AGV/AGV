# 가상 조사 참고 기록 API

`AGV_SIM_CONFIG` 설정에 `"investigation_notes": true`를 명시하면 가상
런타임이 동일 SQLite에 조사 참고 기록을 초기화한다. 기본값은 `false`이며
불리언 이외의 값은 거부한다. 기존 가상 로봇 ID·작업 대응과 복구 게이트를
사용하며 설정 파일의 기존 루프백 MQTT·`sim-` 로봇·로컬 API 토큰 제약이
그대로 적용된다. 팀 맵 가상 실행과 일반 가상 실행 모두 선택 가능하다.

기존 `/simulation` API와 같이 로컬 클라이언트와 공유 Bearer 토큰이 필요하다.
응답은 `Cache-Control: no-store`다. 다음 예시의 토큰은 안전하게 공급해야
하며 실제 토큰이나 개인정보를 로그, 기록 ID 또는 설정된 식별자에 넣지 않는다.

```http
POST /simulation/tasks/T1/investigation-notes
Authorization: Bearer <shared-local-token>
Content-Type: application/json

{"request_id":"19e8327bd02e4c2583f745a8b1bd9c60","category":"connection"}
```

입력은 이 두 필드만 허용한다. `request_id`는 32자리 소문자 hex이며 각
요청에 새 무작위 ID를 만들고 불확실한 동일 요청의 재시도에만 재사용한다.
분류는 `position`, `connection`, `action`, `other` 중 하나다. 같은 ID와
작업·분류의 재시도는 최초 기록을 반환하고 다른 작업 또는 분류는 충돌로
거부한다. 요청 본문은 청크를 읽는 동안 최대 512바이트로 제한한다.
`GET /simulation/tasks/T1/investigation-notes?after_id=0&limit=20`으로 해당
작업만 조회한다. `limit`은 1~20이고 `next_after`가 있으면 다음 페이지의
`after_id`에 넣는다. 비활성, 저장 실패와 손상은 상세 DB 내용을 응답하지 않는다.

응답에는 `scope: isolated_simulation`, `kind: unverified_reference`,
`control_effect: none`, `identity: shared_token_not_person`이 포함된다.
이 기록은 보류 명령이나 확인·복구 승인, 발행, 점유 해제가 아니며 공유
토큰은 개인 신원 인증이 아니다. 가상 DB 스냅샷과 서버 UTC 관측 시각은
실제 로봇 상태·물리적 정지·사건 시간의 증거가 아니다. 런타임이 실행 중이면
MQTT가 끊겨도 읽고 기록할 수 있지만 복구 게이트나 발행에는 영향을 주지
않는다. 재시작 시 기록을 읽을 수 있어도 실행 중 작업을 자동 재개하지 않는다.
종료 시 신규 workflow 발행을 먼저 막고 통신을 정리한 뒤 진행 중인 참고 기록
요청이 끝나면 DB 소유권을 놓는다. 이미 발행 단계에 들어간 동작을 회수한다는
뜻은 아니다. 내부 코드가 같은 스레드의 참고 기록 처리 중 종료를 재진입하면
교착을 막기 위해 거부한다. 정상 HTTP 기록 처리에는 이러한 콜백이 없다.

기록 삽입은 API 수준의 추가 전용이며 DB 파일 직접 수정이나 이전 백업으로의
되돌리기를 막지 못한다. 스냅샷은 기존 DB의 작업·로봇·자원 ID를 포함한다.
용량·스냅샷 한도, 손상 차단과 백업상 한계는
`server/docs/sim_investigation_notes.md`를 참고한다. 임의 텍스트는 받지 않으며
토큰·민감 정보가 기록에 포함되지 않도록 기존 식별자도 관리해야 한다.

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
$env:AMQTT_TEST_PATH = (Resolve-Path .venv\broker-deps).Path
.\.venv\Scripts\python.exe -m unittest server.tests.test_sim_investigation_notes_api server.tests.test_sim_investigation_notes_api_mqtt -v
```

격리 aMQTT 시험은 실제 장비, 실물 통신 인증 또는 물리적 안전 검증이 아니다.
