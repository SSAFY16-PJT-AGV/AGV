# 격리 재동기화 제안 v1

이 문서는 서버·로봇 격리 실험을 위한 제안이다. 기본은 비활성화이며 실물
주행 허가, 물리적 위치·정지·후단 이탈 또는 운영 브로커 보안을 증명하지 않는다.
같은 로봇 프로세스·세션에서 `hold/nav_hold`와 해당 해제 노드가 확인되고,
끝 동작이 `none`인 작업의 원본 `rear_clear`가 보존된 경우만 대상으로 한다.
프로세스 재시작, 중간 정지, 이벤트 원본 유실, 물품 집기·적재 결과 불명,
세션·명령·방문 순번 불일치는 계속 보류한다.

## 전송 및 인증

- `agv/{robot_id}/reconcile/request`, `/response`, `/commit`: 모두 QoS 1,
  non-retained. 기존 `event` 토픽에는 새로운 종류를 보내지 않는다.
- 로봇별로 서로 다른 32바이트 이상 비밀 키를 메모리로 명시 주입한다.
  키가 없으면 비활성화된다. 키를 코드·로그·문서에 쓰지 않는다. HMAC은 장비가
  올바르게 물리 정지했다는 의미가 아니며 운용에는 인증·TLS·ACL이 추가로 필요하다.
- `mac`을 뺀 나머지 정확한 필드를 `json.dumps(sort_keys=True,
  ensure_ascii=True,separators=(',',':'),allow_nan=False).encode('ascii')`로
  직렬화하고 `HMAC-SHA256(key, b'AGV-RECONCILE-V1\0' + canonical)`를
  소문자 64자리 hex로 보낸다. 중복 키·초과 크기·NaN·예상 밖 필드·서명 불일치,
  retained 또는 QoS 1이 아닌 메시지는 거부한다.

## 메시지

모든 메시지에 `v=1`, `kind`, `nonce`, `server_epoch`, `robot_id`,
`robot_session`, `order_id`, `seq`, `order_digest`, `map_id`, `release_index`,
`stopped_at`, `event_id`, `event_digest`, `mac`이 있다. `nonce`와 `server_epoch`,
두 digest는 소문자 64자리 hex다. seq는 양의 정수, release_index는 1 이상의
정수이며 불리언은 허용하지 않는다.

1. `request`의 `kind=probe`: 서버가 이벤트를 못 보았으면 `event_id`와
   `event_digest` 둘 다 null, 알고 있으면 둘 다 제공한다. 서버가 못 보았다는
   사실은 이미 전송된 메시지가 도착하지 않는다는 증거가 아니다.
2. `response`의 `kind=snapshot`: 공통 필드에 실제 이벤트 ID와 canonical
   SHA-256 digest를 채우고, 해당 연결의 `connection_generation`, 로봇이 생성한
   64자리 `robot_nonce`, 원본 `rear_clear` JSON `event`를 추가한다.
3. `commit`의 `kind=commit`: 공통 필드, snapshot의 두 nonce·연결 세대·이벤트
   ID/digest와 `decision=hold_confirmed`를 포함한다. 원본 event는 포함하지 않는다.
4. `response`의 `kind=commit_ack`: commit과 동일한 내용 및 `kind=commit_ack`로
   다시 서명한다.

서버 nonce는 서버 epoch에서 단일 사용하며 서버의 단조 시계로 만료한다. 로봇은
첫 유효 probe를 수락할 때만 nonce를 만들고 자체 단조 시계 10초 기한을 고정한다.
같은 probe 재전송은 nonce나 기한을 갱신하지 않는다. 다른 내용의 동일 nonce,
연결 세대 변경, 상태 변동, 시간 만료는 거부한다. 사용 nonce 저장 한도에 도달하면
오래된 것을 지워 재사용하지 않고 새 probe를 차단한다. 동일 commit의 응답 유실 시
같은 연결·기한에 한해 동일 ACK를 재전송할 수 있으며 다른 commit은 거부한다.

commit은 서버 장부 확인 사실을 알릴 뿐이다. **disconnect latch와 일반 주문 및
모터 차단은 유지한다.** 현재 6필드 주문에는 서버 dispatch 세대가 없어 새 연결에
늦게 도착한 옛 주문을 구별할 수 없기 때문이다. 추가 재동기화나 수동 확인 없이
후속 주행하지 않는다.
