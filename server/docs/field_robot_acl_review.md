# 로봇 MQTT 토픽 권한 제안의 오프라인 대조

`server.field_robot_acl_review`는 팀과 논의할 **로봇 계정별 토픽 권한 제안**을
로컬 JSON으로 대조한다. `feature/field-run`의 `ef9e600`에 있는 로봇 MQTT 링크의
현재 토픽에 근거하며, 확정된 팀 정책이 아니다. 브로커에 접속하거나 설정 파일을
읽고 쓰지 않으며, 명령을 발행하지 않는다.

각 로봇에 서로 다른 계정을 사용한다고 가정하고, 해당 로봇의 아래 토픽만
선언되어 있는지 확인한다.

| 방향 | 토픽 |
| --- | --- |
| 로봇 발행 | `agv/{id}/state`, `agv/{id}/event`, `agv/{id}/conn` |
| 로봇 구독 | `agv/{id}/order` |

`conn` 발행에는 온라인과 정상 종료의 오프라인 메시지가 포함된다. LWT 허용
여부는 브로커의 인증·인가 방식에서 별도로 확인한다. 로봇 패치의 일회 이벤트
클라이언트가 임시 client ID를 쓸 때도 같은 계정으로 해당 토픽을 발행할 수
있는지와 client ID 정책을 별도 시험해야 한다.

## 입력 예시

다음과 같은 **가상의** 파일을 로컬에 준비한다. 실제 비밀번호·토큰·인증서와
접속 정보는 넣지 않는다.

```json
{
  "robots": [
    {
      "robot_id": "r1",
      "principal": "robot-1",
      "publish": ["agv/r1/state", "agv/r1/event", "agv/r1/conn"],
      "subscribe": ["agv/r1/order"]
    },
    {
      "robot_id": "r2",
      "principal": "robot-2",
      "publish": ["agv/r2/state", "agv/r2/event", "agv/r2/conn"],
      "subscribe": ["agv/r2/order"]
    }
  ]
}
```

```powershell
.\.venv\Scripts\python -m server.field_robot_acl_review C:\경로\robot-acl-proposal.json
```

`matches_proposal: true`는 **입력에 적힌 로봇 계정의 선언**이 위 제안과
일치한다는 의미다. 누락이나 예상 밖 권한(다른 로봇의 토픽, 와일드카드,
`obs` 등)이 있으면 `issues`에 표시하고 종료 코드 1을 반환한다. 식별자나
JSON 구조가 잘못되었거나 로봇 ID·계정이 중복되면 종료 코드 2다. 오류 메시지에
입력 본문을 출력하지 않는다. 코드 0은 선언과 제안의 일치만 뜻한다.

## 확인할 수 없는 것

- 입력이 브로커 설정의 전체 권한을 빠짐없이 표현하는지, 다른 계정·역할·상속
  규칙이 추가 권한을 부여하는지 확인하지 않는다. 설정된 인증 방식, 계정과 실제
  장치의 대응, client ID, TLS 인증서와 호스트 검증도 확인하지 않는다.
- 서버 계정, 관리자, 관측·매핑 구성요소 및 그 권한은 범위 밖이다. 실제 서버
  관측 서비스는 와일드카드를 구독하므로 이 로봇 전용 제안을 적용할 수 없다.
- 팀이 추후 `obs` 등 추가 토픽을 사용한다면 제안을 재검토해야 한다. 이 도구는
  선언되지 않은 권한을 추정하거나 누락된 브로커 권한을 자동으로 고치지 않는다.
- MQTT 3.1.1 PUBACK은 발행 ACL 통과나 수신 측의 처리를 증명하지 않는다. 실제
  브로커에서 계정별 허용·거부 및 토픽 전달을 별도 격리 시험해야 한다.
- 이 비교는 실제 로봇의 주행, 정지, 후단 이탈, 실물 운행 승인과 무관하다.

격리 단위 시험:

```powershell
$env:TEMP = (Resolve-Path .venv).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python -m unittest server.tests.test_field_robot_acl_review -v
```
