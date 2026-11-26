# 격리 Mosquitto의 로봇별 TLS·토픽 권한 시험

이 시험은 팀과 협의할 로봇 계정별 권한 제안을 **별도로 생성한 로컬
Mosquitto**에 적용해 확인한다. 실제 브로커·계정·인증서나 로봇에는
접속하지 않는다. 임시 사용자 암호·1일짜리 자체 서명 TLS 인증서·ACL과
새 임시 폴더를 만들고 시험 소유 브로커만 종료한다. 테스트를 완료하면
임시 폴더는 삭제된다.

두 가상 로봇 각각에 다른 계정을 부여하고, 자기 로봇의 `state`, `event`,
`conn`만 발행하고 자기 `order`만 구독하도록 설정한다. 시험 전용 관찰자는
양쪽 토픽을 구독하고 주문을 발행한다. 관찰자 권한은 실서비스 서버의
권한 제안이나 실제 브로커 설정을 의미하지 않는다.

테스트는 다음을 확인한다.

- 올바른 계정·암호와 CA로 연결하고 각자의 상태·이벤트·연결 보고를
  실제 관찰자에게 전달한다. 다른 로봇의 토픽 및 주문 토픽에 대한 발행은
  허가된 후속 메시지까지 관찰하면서 전달되지 않는지 확인한다.
- 두 로봇이 양쪽 주문 토픽을 구독하려고 시도해도 실제로는 자기 주문만
  받는다. 자기 연결 종료 LWT는 전달되고 다른 로봇을 가장한 LWT는
  전달되지 않는다.
- 틀린 암호·익명 계정·다른 CA·다른 서버 이름을 거부하며 평문 MQTT
  연결이 성공하지 않는지 확인한다.

MQTT 3.1.1의 PUBACK이나 SUBACK은 ACL에 따른 실제 전달의 증거가 아니다.
따라서 이 시험은 해당 토픽의 수신 여부와 각 송신자의 후속 허가 메시지를
함께 검사한다. 네트워크 지연이 있는 모든 운영 환경에서의 거부를 증명하지는
않는다. MQTT 장치 인증서(mTLS), 실제 장치별 키 배포·폐기, 클라이언트 ID
정책, 실물 장비와 브로커 권한은 별도 검증이 필요하다. 테스트의 Paho
클라이언트 결과는 팀 로봇 코드나 실물 주행 안전의 증거가 아니다.

## 실행

Mosquitto 2와 같은 폴더의 `mosquitto_passwd`, OpenSSL, 서버 Python 의존성이
필요하다. `MOSQUITTO_EXE`를 명시하지 않거나 OpenSSL이 없으면 시험을 생략한다.
Windows용 Mosquitto가 한글 경로의 인증·ACL 파일을 열지 못하는 경우가 있어
`AGV_MQTT_ASCII_TEMP`에 쓰기 가능한 **영문 경로의 기존 폴더**를 지정한다.
해당 폴더에는 무작위 이름의 테스트 전용 임시 폴더만 만든다.

```powershell
$env:MOSQUITTO_EXE = 'C:\Program Files\mosquitto\mosquitto.exe'
$env:AGV_MQTT_ASCII_TEMP = 'C:\path\to\writable\ascii-temp'
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest server.tests.test_mosquitto_tls_robot_acl -v
```

이 시험에서 사용하는 `127.0.0.1`과 임시 인증서는 테스트 소유 브로커에만
적용되며 운영 환경 연결을 위한 값이 아니다. 시스템 MQTT 프록시·전역 SOCKS
프록시가 격리 연결을 우회할 수 있으므로 기존 로컬 전용 가드를 통과해야 한다.
