# AGV 프로젝트 세션 안내

이 문서는 새 세션이 프로젝트와 자료 위치를 빠르게 파악하기 위한 안내다.
작업 전 현재 파일·브랜치·원격 상태와 사용자의 최신 요청을 다시 확인한다.
저장소의 [AGENTS.md](AGENTS.md)에는 프로젝트 작업 기준이 있다.

## 1. 프로젝트 설명

- 스마트 마트의 창고·매장 물류를 자동화하는 팀 프로젝트다. 재고 부족을
  확인하고 AGV 두 대가 물품을 운반하며, 서버가 작업과 경로·구간 점유를
  관리하고 Unreal 디지털 트윈으로 상태를 보여주는 것이 목표다.
- 서버는 Python과 FastAPI를 사용한다. 로봇과 서버는 MQTT/JSON으로
  통신하며, 웹 주문은 HTTP API를 사용한다. 현재 가상 시연에서 Unreal에는
  WebSocket으로 상태와 집기·적재 요청을 전달한다.
- 저장소: [GitHub AGV](https://github.com/SSAFY16-PJT-AGV/AGV). 서버 작업
  브랜치는 [`feature/server-communication`](https://github.com/SSAFY16-PJT-AGV/AGV/tree/feature/server-communication)이다.
  팀 로봇과 Unreal 코드는 각자 별도 브랜치에서 관리하므로 작업 전 확인한다.

## 2. 사용자의 역할

사용자는 **서버·통신 담당자**다. 웹·모바일 주문을 서버 작업으로 연결하고,
로봇 배정·경로 계산·구간 예약·MQTT 명령과 상태·이벤트 처리, Unreal과의
상태·동작 연동, 재고·주문 저장과 복구, 통합 시험이 주요 담당 범위다.

- 팀 로봇과 Unreal의 기존 구현 및 최신 브랜치를 확인해 중복 구현을 피한다.
- 가상 시험, 실제 Unreal 실행, 실물 로봇 시험을 구분해 결과를 설명한다.
- 사용자는 한국어의 구체적인 설명과 작업 완료 후 기능별 커밋·서버·통신
  브랜치 즉시 푸시를 선호한다. 작업 전 Git 상태를 확인하고 사용자 변경을
  보존한다.

## 3. 작업 전 참고 자료

| 자료 | 확인할 내용 |
| --- | --- |
| [팀 설계 문서 v0.3](https://github.com/SSAFY16-PJT-AGV/AGV/blob/docs/project-plan/docs/design.md) | 전체 구성, MQTT 토픽·메시지, 맵 좌표와 구간 예약 설계. 현재 구현과 차이가 있을 수 있으므로 코드와 함께 확인한다. |
| [저장소 작업 기준](AGENTS.md) | 코드 구조, 설계 우선순위, 실물 장비와 테스트 규칙. |
| [서버 README](server/README.md) | 서버 실행 방법, MQTT 보안 설정, 웹·Unreal 시연과 테스트 조건. |
| [팀 맵 웹·Unreal 시연](server/docs/field_web_unreal_demo.md) | 맵 대응 예제, 실행 명령과 해당 예제의 가정. |
| [팀 로봇 연동 대조](server/docs/field_robot_interop.md) | 팀 로봇의 MQTT 코드·맵과 서버의 차이 및 연동 조건. |
| [실제 AGV–서버 MQTT 통신 이슈 #42](https://github.com/SSAFY16-PJT-AGV/AGV/issues/42) | 로봇 측 통신 규격 제안. 설계 문서·현재 코드와 단위 및 필드를 대조한다. |
| [로봇 반복 경로 패치](server/docs/field_nav_sequence_limited.md) | 팀 로봇 코드에 대한 별도 패치의 적용·시험 방법과 제한. |
| [팀 로봇 맵 원본](https://github.com/SSAFY16-PJT-AGV/AGV/blob/feature/field-run/maps/mat_ssafy_v2.json) | 팀 로봇이 사용하는 맵 데이터. |
| [디지털 트윈 브랜치](https://github.com/SSAFY16-PJT-AGV/AGV/tree/feature/digital-twin) · [로봇 브랜치](https://github.com/SSAFY16-PJT-AGV/AGV/tree/feature/field-run) | 팀의 Unreal 및 로봇 최신 코드 확인. |

이 PC의 Unreal 코드와 맵은 각각 `D:\관통\AGV-digital-twin\README.md`,
`D:\관통\AGV-digital-twin\Content\Data\warehouse_map.json`에서도 확인할 수 있다.

설계 문서는 로컬 저장소에서도 다음 명령으로 읽을 수 있다.

```powershell
git show origin/docs/project-plan:docs/design.md
```

확인한 작업 폴더와 연결된 Google Drive 검색에서는 이 프로젝트의 PDF·Word
원본을 찾지 못했다. 필요한 경우 팀의 원본 문서 위치를 확인해 이 항목을 갱신한다.

## 4. 지난 작업 기록을 볼 위치

지난 작업 내용은 이 파일에 옮기지 않는다. 필요한 기록은 다음 위치에서 찾는다.

| 위치 | 볼 수 있는 자료 |
| --- | --- |
| `D:\관통\AGENTS.md` | 이 PC의 세션별 상세 인계 기록. 최신 날짜부터 확인한다. |
| `D:\관통\AGV-analysis` | 저장소. `git log`와 `server/docs/`에서 커밋 이력·관련 안내를 확인한다. |
| [서버·통신 브랜치 커밋](https://github.com/SSAFY16-PJT-AGV/AGV/commits/feature/server-communication) · [팀 공유 이슈 #43](https://github.com/SSAFY16-PJT-AGV/AGV/issues/43) · [이전 이슈 #38](https://github.com/SSAFY16-PJT-AGV/AGV/issues/38) | GitHub에 남은 커밋과 팀 공유 기록. |
| `D:\관통\AGV-analysis\.venv\` | 이 PC에 남은 격리 시험 결과·로그. Git 추적 대상이 아니므로 다른 PC에는 자동으로 제공되지 않는다. |

`D:\관통` 경로는 이 PC 기준이다. 다른 PC에서는 저장소를 클론한 위치를
사용한다. 로컬 시험 로그·데이터베이스·Unreal 빌드와 이 PC의 상위 인계
문서는 Git으로 자동 전달되지 않는다. 인증정보와 토큰을 문서에 기록하거나
커밋하지 않는다.
