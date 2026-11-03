# MQTT 통신 연습

이 코드는 한 PC의 `127.0.0.1` 브로커를 사용하는 **가짜 로봇 연습**입니다.
실제 JETANK, 모터, 작업 배정 및 경로 예약에는 연결되지 않습니다.
`demo-robot1`은 실습 전용 ID이고 메시지의 맵·노드·주문 값은 임시 예시입니다.

## 준비

이 실습 PC에는 공식 Mosquitto 2.1.2 실행 파일을 저장소 바깥 `../tools/mosquitto-2.1.2`에 준비했습니다. 다른 PC에서는 [Mosquitto 공식 다운로드](https://mosquitto.org/download/)에서 설치할 수 있습니다.

PowerShell에서 저장소 루트로 이동한 뒤 별도의 첫 번째 창에서 브로커를 시작합니다:

```powershell
& "..\tools\mosquitto-2.1.2\mosquitto.exe" -p 18883 -v
```

설치 경로가 다르면 그 위치의 `mosquitto.exe`를 지정하세요. 설정 파일 없이 시작한 브로커는 이 PC의 로컬 연결만 허용합니다. 이 예제는 18883번 포트를 사용합니다.

다른 PowerShell 창에서 Python 가상환경을 준비합니다:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r server/demo/requirements.txt
```

## 실행: 서버와 가짜 로봇용 PowerShell 창 2개

1. 첫 번째 창에서 서버를 시작하고 `ready`가 나타나는지 확인합니다.

   ```powershell
   .\.venv\Scripts\python server/demo/demo.py server --port 18883
   ```

2. 저장소 루트에 있는 두 번째 창에서 가짜 로봇을 시작합니다.

   ```powershell
   .\.venv\Scripts\python server/demo/demo.py robot --port 18883
   ```

3. 로봇이 `agv/demo-robot1/state`에 가짜 대기 상태를 보내면 서버가 이를
   출력하고 `agv/demo-robot1/order`에 가짜 주문을 보냅니다. 로봇은 주문을
   출력하고 `order_received` 상태를 보냅니다. 서버에서 이 상태까지 확인하면
   양방향 통신이 완료된 것입니다. 각 창을 끝낼 때는 `Ctrl+C`를 누릅니다. 다시 연습할 때는 서버와 로봇을 모두 종료하고 서버부터 다시 시작하세요. 브로커도 사용을 마치면 해당 창에서 `Ctrl+C`로 종료합니다.

브로커가 다른 포트를 사용하면 두 명령 모두에 `--port 포트번호`를 추가합니다.
연결 오류가 나면 브로커가 **같은 PC의 127.0.0.1**에서 실행 중인지 확인하세요.

```powershell
python -m unittest discover -s server/demo -p 'test_*.py'
```

## 설계 문서와의 차이

주문은 문서의 필드 구조를 본뜬 예시이며 QoS 1, retain 없음으로 보냅니다.
상태는 연습을 위해 QoS 0, retain 없음으로 보내므로 문서가 제안하는 retained
상태 및 전체 필드와 다릅니다. 실물과 연동하려면 팀의 메시지 계약, 인증,
중복·재연결 처리, 위치 확인, 예약 및 로봇 측 정지 인터록을 별도로 구현하고
검증해야 합니다. 이 데모의 주문 수신은 실제 수행이나 도착을 의미하지 않습니다.
