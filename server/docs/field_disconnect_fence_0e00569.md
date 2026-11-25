# 팀 로봇 MQTT 단절 시 주문 처리 차단 제안

`feature/field-run`의 `0e00569`를 대상으로 한
[`field_disconnect_fence_0e00569.patch`](field_disconnect_fence_0e00569.patch)는
별도 사본에서 재현·시험한 **제안 패치**다. 팀 로봇 원본에는 적용하지 않았다.
기존 [`field_nav_sequence_limited.patch`](field_nav_sequence_limited.md)와
`agv/link/` 및 `agv/runner.py`를 함께 수정하므로 두 패치를 겹쳐 적용하지 않는다.
이 제안은 단절 시 주문 경합을 대상으로 하며 반복 노드 경로 문제를 해결하지 않는다.

## 동작과 제한

- MQTT 주문을 받은 연결 세대에 묶는다. 단절 콜백이 시작되면 신호를 남기고
  대기 중인 주문 큐를 지운다. 이미 큐에서 꺼낸 주문도 구세대면 처리하지 않는다.
- 단절 콜백 신호는 재연결되어도 남는다. 해당 링크는 새 주문을 받아 운행하지
  않으며 사람이 상태를 확인하기 전 자동 재개하지 않는다. 이 패치에는 기존
  작업·점유를 복구하고 단절 신호를 해제하는 프로토콜이 없다.
- 명령 처리 전과 경로 설정 후 연결 상태를 확인한다. 새 주행을 재개한 경우
  runner가 첫 모터 명령을 반환하기 전후에도 단절 신호를 확인한다. 단절 감지
  도중 주문 장부가 이미 바뀐 경우도 있으므로 재연결만으로 재개할 수 없다.
- 정지 이벤트 생성 시 이미 단절이 감지됐으면 이벤트를 발행하지 않는다. 그러나
  이벤트 발행 직후 단절되는 경우 Paho 내부에 큐된 QoS 1 이벤트의 재전송이나
  서버 수신 여부는 이 패치가 해결하지 않는다. 결과 불명 이벤트의 일회 처리와
  서버·로봇 간 재동기화 계약이 따로 필요하다.
- 단절 **콜백이 시작된 뒤**에 대한 처리다. 통신이 물리적으로 끊겼지만 아직
  콜백이 오지 않은 시점, 첫 출력 검사가 끝난 뒤 실제 모터에 적용하기 전의
  경합, 이미 진행 중인 회전·후진의 제동 거리나 물리적 정지를 증명하지 않는다.
  상태의 신뢰성, 장비별 인증·TLS·ACL, 위치·후단 이탈도 따로 검증해야 한다.
- 링크 교체나 로봇 재시작만으로 이전 명령과 구간 점유가 일치한다고 간주하지
  않는다. 서버의 실물 실행 차단을 이 패치만으로 해제하지 않는다.

## 적용과 격리 시험

팀 소스 `0e00569`의 **새 별도 사본**을 준비한 뒤 그 사본에서 적용한다. 아래
예시의 서버 저장소 경로와 팀 사본 경로는 실행 환경에 맞게 바꾼다.

```powershell
Set-Location 'D:\팀-로봇-별도-사본'
git apply --check 'D:\관통\AGV-analysis\server\docs\field_disconnect_fence_0e00569.patch'
git apply 'D:\관통\AGV-analysis\server\docs\field_disconnect_fence_0e00569.patch'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'D:\관통\AGV-analysis\.venv\field-mat-deps'
$env:TEMP = 'D:\관통\AGV-analysis\.venv\pr59-test-temp'
$env:TMP = $env:TEMP
& 'D:\관통\AGV-analysis\.venv\Scripts\python.exe' -m unittest agv.tests.test_link_disconnect_fence agv.tests.test_link
```

독립 사본에 적용해 실행한 결과 **18개 중 16개 통과, 2개 생략**이었다. 생략은
팀 테스트의 `AGV_SERVER_REPO` 미설정 때문이다. 신규 6개는 단절된 주문 큐,
재연결 전 큐에 들어온 주문, 이미 꺼낸 구세대 주문, 경로 설정 중 단절,
잠금을 기다리는 콜백, 첫 모터 출력 전 단절을 다룬다. 최초 세 경합과 첫 출력
사례는 수정 전 실패도 확인했다. 팀 가상 매트의 기존 MQTT 링크 시험도 포함한다.
Paho 2.x에서 callback API v1 사용 중단 예정 경고가 나왔다.

이 시험은 합성 영상·대역 통신을 이용하며 실물 JETANK나 팀 공용 브로커로
검증한 결과가 아니다. 팀 테스트 전체 실행은 완료하지 않았다.
