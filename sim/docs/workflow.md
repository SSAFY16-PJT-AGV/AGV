# 격리 시뮬레이션 후속 주문 연결

`SimProgressWorkflow`는 기존 수신기의 `advanced` 결과를 한정된 큐에 넣고
별도 작업자에서 후속 `ProgressPublisher`를 호출한다. 수신기의 결과 observer가
비어 있을 때만 `start()`로 연결하며 MQTT 수신 콜백은 발행 완료를 기다리지
않는다. 이미 설정된 observer는 교체하지 않는다. 수신기가 연결·구독 준비를
마치지 않았으면 제출하지 않고, 중복 및 최종 완료 이벤트는 자동 발행하지 않는다.

생성할 때 수신기와 같은 progress/gate, 고정된 로봇별 작업과 부팅 세션 매핑,
`simulation_enabled=True`와 새 전용 `OrderPublisher`를 제공하는 factory가
필요하다. `factory(robot_id, task_id, seq)`는 context manager를 반환하며 매번
새로운 전용 연결을 준비하고 종료 시 클라이언트를 정리해야 한다. 자동 작업은
발행 전에 저장된 이벤트 및 진행 journal의 robot, task, seq, session, digest와
주문 payload를 대조한다. 최종 허가는 기존 recovery gate가 확인한다.

재시작 시 대기 주문을 자동 탐색하거나 재전송하지 않는다. 운영자가 새 challenge를
완료하고 수신기를 준비한 후 `submit_pending(task_id, seq, session)`으로만
기존 pending 주문을 명시적으로 제출할 수 있다. uncertain 주문은 다시 전송하지
않는다. 큐가 가득 찼거나 중단되거나 발행이 실패해도 점유를 해제하지 않으며,
결과가 불확실하면 그대로 보존하고 복구해야 한다. `stop()`은 새 제출을 중단하고
대기 항목을 버린 뒤 실행 중 작업의 종료를 기다린다. 오류 observer에는 고정된
분류만 전달하며 주문 payload나 예외 원문을 기록하지 않는다.
발행 진입과 stop은 별도 락으로 순서가 정해진다. 이미 발행에 진입한 시도는
종료를 기다리며, stop이 먼저 진행된 경우 새 발행에 진입하지 않는다. 이 락을
기다리는 동안 MQTT 수신 콜백의 락은 잡지 않는다.

이는 실제 로봇에 대한 인증이나 정지·후단 이탈 증명이 아니다. factory가 외부
네트워크나 물리 로봇에 연결하지 않도록 호출자가 별도로 격리해야 한다. 브로커
연결이 물리적으로 끊긴 즉시 서버가 이를 감지하거나 기계를 멈추는 것도 보장하지
않는다. 실제 주행에는 사용할 수 없다.
