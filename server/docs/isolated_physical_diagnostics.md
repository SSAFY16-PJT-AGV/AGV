# 격리 실물 재동기화 관찰 진단

`GET /robots/physical-recovery`는 명시적으로 연결한 격리 관찰기와 같은
`RecoveryGate`·예약 DB·서명 장부의 **읽기 전용 상태**를 반환한다. 기존
관찰기·재동기화 과정을 실행하거나 이벤트를 처리하지 않는다. 실물 장비,
실제 정지·위치·후단 이탈 또는 운행 허가를 확인하는 API가 아니다.

기본 서버 실행에는 관찰기가 없어 503이다. 격리 실험 코드는 자신이 만든
동일 gate와 활성 watcher를 직접 넘겨 아래처럼 일시적으로 결합한다.

```python
from server.physical_recovery_diagnostics import PhysicalRecoveryDiagnostics
app.state.physical_recovery_diagnostics = PhysicalRecoveryDiagnostics(
    gate, watcher, isolated=True)
```

사용이 끝나면 바인딩을 제거하고 실험이 소유한 watcher를 종료한다. watcher
교체·중지, 다른 gate epoch, 잘못된 DB·점유나 차단 해제는 503이며 빈 결과로
추정하지 않는다. 이 API는 physical 런타임이나 로봇 명령 발행을 켜지 않는다.
관찰기와 장부의 생성 조건 및 제한은 [격리 재동기화](isolated_physical_reconciliation.md)를
따른다.

`tasks`의 `gate=blocked`는 기존 서버 차단 상태다. `report`는 연결이 없거나
오프라인이면 `unavailable`, 상태가 없으면 `not_observed`, 수신 시각이
기준보다 오래되거나 역행하면 `stale`, 현재 연결에서 최근 상태가 도착했다면
`recent_report`다. 이는 보고서의 도착 신선도이지 물리 정지의 증거가 아니다.
`signed_record=recorded`는 현재 서버 epoch에 서명 교환이 기록됐음을 뜻할
뿐 그때의 보고가 지금도 유효하다는 의미가 아니다. `event_state=not_observed`는
수신함에서 확인하지 못했다는 뜻이며, 수신되지 않았다는 증거가 아니다.
모든 항목에서 `motion_authorized`와 `physical_stop_proven`은 `false`다.
세션·nonce·서명·원본 메시지·인증 값은 출력하지 않는다.

기존 `/robots` 관측과 같은 직접 loopback 및 선택적
`AGV_OBSERVATION_API_TOKEN` 보호를 사용하며 프록시를 지원하지 않는다.
토큰이 설정되지 않은 경우 로컬 사용자에게 인증 없이 보이므로 운영 서비스의
접근 제어로 취급하지 않는다. 모든 응답에 `Cache-Control: no-store`를 붙인다.
